#!/usr/bin/env python3
"""Pulls finished company runs from the processing tier into the local container.

This VM can open outbound connections but cannot be reached, so nothing is pushed to it.
Every few minutes this asks the processing tier what is waiting, fetches it, and feeds it
into the container's ingest API on localhost — the same endpoints that were always used,
reached from inside this machine instead of from the internet.

Standard library only: this VM has no package index, so there is nothing to install.

    PROCESSING_URL=https://... INGEST_SECRET=... python3 poller.py
    python3 poller.py --once        one pass, then exit (use this to test)
    python3 poller.py --dry-run     report what is waiting, change nothing

Progress is kept in a local file, not on the far side: this is the only consumer, so it is
the only thing that needs to remember what it already has. Re-pulling is harmless — the
ingest API deduplicates by batch_id and blobs by sha256 — so an interrupted run costs one
repeated page, never a half-applied company.

Exit codes: 0 fine, 1 configuration wrong, 2 the far side could not be reached.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROCESSING_URL = os.environ.get("PROCESSING_URL", "").rstrip("/")
CONTAINER_URL = os.environ.get("CONTAINER_URL", "http://localhost:3000").rstrip("/")
KEY_ID = os.environ.get("INGEST_KEY_ID", "modus-processing")
SECRET = os.environ.get("INGEST_SECRET", "")
INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
PAGE = int(os.environ.get("POLL_PAGE_ROWS", "500"))
STATE_FILE = os.environ.get("POLL_STATE_FILE", "/var/lib/modus-poller/state.json")
TIMEOUT = int(os.environ.get("POLL_TIMEOUT_SECONDS", "120"))

log = logging.getLogger("poller")
_stop = False


# --------------------------------------------------------------------------- http
def _signed(body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    bh = hashlib.sha256(body).hexdigest()
    return {
        "X-Key-Id": KEY_ID,
        "X-Timestamp": ts,
        "X-Body-Sha256": bh,
        "X-Signature": hmac.new(SECRET.encode(), f"{ts}.{bh}".encode(), hashlib.sha256).hexdigest(),
    }


def _lower(headers) -> dict[str, str]:
    return {k.lower(): v for k, v in (headers or {}).items()}


def _request(method: str, url: str, *, body: bytes | None = None,
             headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    payload = body if body is not None else b""
    hdrs = dict(_signed(payload))
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        # urlopen honours HTTPS_PROXY / HTTP_PROXY from the environment, which is how this
        # VM reaches anything at all.
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(), _lower(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), _lower(exc.headers)


def _get_json(path: str) -> dict:
    status, raw, _ = _request("GET", f"{PROCESSING_URL}{path}")
    if status != 200:
        raise RuntimeError(f"GET {path} -> {status} {raw[:200]!r}")
    return json.loads(raw)


def _post_container(path: str, payload: dict) -> None:
    body = json.dumps(payload, default=str).encode()
    status, raw, _ = _request(
        "POST", f"{CONTAINER_URL}/api/ingest/v1{path}", body=body,
        headers={"Content-Type": "application/json"})
    if status not in (200, 201, 202):
        raise RuntimeError(f"POST {path} -> {status} {raw[:300]!r}")


# --------------------------------------------------------------------------- state
def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"runs": {}}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = f"{STATE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, STATE_FILE)      # atomic, so a crash mid-write cannot corrupt it


# --------------------------------------------------------------------------- work
def _send_blobs(rows: list[dict], column: str, seen: set[str]) -> int:
    """Make every blob a row points at exist in the container BEFORE the row does."""
    urls = sorted({r[column] for r in rows
                   if r.get(column) and str(r[column]).startswith("s3://")} - seen)
    for url in urls:
        status, data, headers = _request(
            "GET", f"{PROCESSING_URL}/api/sync/blob?url={urllib.parse.quote(url, safe='')}")
        if status != 200:
            log.warning("  blob %s unavailable (%s) — rows will still load, image will not render",
                        url, status)
            seen.add(url)        # do not retry forever on a blob that is simply gone
            continue
        sha = headers.get("x-blob-sha256") or hashlib.sha256(data).hexdigest()
        put, raw, _ = _request(
            "PUT", f"{CONTAINER_URL}/api/ingest/v1/blobs/{sha}", body=data,
            headers={"Content-Type": headers.get("content-type", "application/octet-stream"),
                     "X-Blob-Key": headers.get("x-blob-key", "")})
        if put not in (200, 201, 409):
            raise RuntimeError(f"PUT blob {sha} -> {put} {raw[:200]!r}")
        seen.add(url)
    return len(urls)


def sync_users(*, dry_run: bool) -> int:
    """Bring the local access list in line with the processing tier.

    Runs every pass, independently of whether any company data is waiting: an account
    granted or revoked in the control panel must take effect here without waiting for
    the next pipeline run. The far side converges on the list it is given, so sending
    the same list twice changes nothing.
    """
    users = _get_json("/api/sync/users")["users"]
    if dry_run:
        log.info("access list: %d user(s) — would apply", len(users))
        return 0
    _post_container("/users", {"users": users})
    log.info("access list: %d user(s) applied", len(users))
    return len(users)


def collect(run: dict, state: dict, *, dry_run: bool) -> bool:
    """Pull one run. Returns True if it completed."""
    run_id = run["run_id"]
    plan = _get_json(f"/api/sync/plan/{run_id}")
    progress = state["runs"].setdefault(run_id, {"done_tables": [], "counts": {}, "batches": 0})
    pending = [t for t in plan["tables"] if t["name"] not in progress["done_tables"]]

    log.info("run %s (%s) — %d of %d tables left",
             run_id[:8], plan.get("isin"), len(pending), len(plan["tables"]))
    if dry_run:
        for t in pending:
            log.info("  would pull %s", t["name"])
        return False

    blobs_seen: set[str] = set()
    for table in pending:
        name, seq, after, rows_sent = table["name"], 0, None, 0
        while True:
            seq += 1
            query = f"?seq={seq}&limit={PAGE}"
            if after:
                query += "&after=" + urllib.parse.quote("\t".join(after), safe="")
            page = _get_json(f"/api/sync/rows/{run_id}/{name}{query}")
            batch = page["rows"]
            if batch:
                if table["blob_column"]:
                    _send_blobs(batch, table["blob_column"], blobs_seen)
                _post_container("/batches", {
                    "schema_version": plan["schema_version"],
                    "batch_id": page["batch_id"], "run_id": run_id,
                    "finished_at": plan["finished_at"], "isin": plan["isin"],
                    "table": name, "mode": page["mode"], "key": page["key"],
                    "seq": seq, "rows": batch})
                rows_sent += len(batch)
                progress["batches"] += 1
                after = page["last_key"]
            # Checkpoint after every page, so a crash resumes here and not at the start.
            progress["counts"][name] = rows_sent
            _save_state(state)
            if page["exhausted"]:
                break
        progress["done_tables"].append(name)
        _save_state(state)
        log.info("  %-26s %d rows", name, rows_sent)

    # Commit swaps the replace-mode tables in one transaction; until this lands the run is
    # staged but invisible, which is what stops a half-pulled company from being shown.
    _post_container(f"/runs/{run_id}/commit", {
        "schema_version": plan["schema_version"],
        "run_id": run_id, "isin": plan["isin"], "run_type": plan["run_type"],
        "finished_at": plan["finished_at"], "expected_batches": progress["batches"],
        "counts": progress["counts"], "replace_tables": plan["replace_tables"]})

    status, raw, _ = _request("POST", f"{PROCESSING_URL}/api/sync/done/{run_id}")
    if status != 200:
        raise RuntimeError(f"could not confirm delivery: {status} {raw[:200]!r}")
    state["runs"].pop(run_id, None)
    _save_state(state)
    log.info("run %s complete — %d batches", run_id[:8], progress["batches"])
    return True


def one_pass(*, dry_run: bool) -> int:
    state = _load_state()
    # Users first: an analyst who cannot log in does not care that the data arrived.
    try:
        sync_users(dry_run=dry_run)
    except Exception:                           # noqa: BLE001 - never block data on this
        log.exception("could not sync the access list; continuing with data")
    waiting = _get_json("/api/sync/pending")["runs"]
    if not waiting:
        log.info("nothing waiting")
        return 0
    log.info("%d run(s) waiting", len(waiting))
    collected = 0
    for run in waiting:
        if _stop:
            log.info("stopping — current run will resume from its last page")
            break
        try:
            if collect(run, state, dry_run=dry_run):
                collected += 1
        except Exception:                       # noqa: BLE001 - one bad run must not stall the rest
            log.exception("run %s failed; will retry next pass", run["run_id"][:8])
    return collected


def main() -> int:
    ap = argparse.ArgumentParser(description="pull finished runs into the local container")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--interval", type=int, default=INTERVAL, help="seconds between passes")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if not (PROCESSING_URL and SECRET):
        missing = " and ".join(n for n, v in
                               (("PROCESSING_URL", PROCESSING_URL), ("INGEST_SECRET", SECRET)) if not v)
        log.warning("%s not set — collection is off; the preloaded data is unaffected", missing)
        if args.once or args.dry_run:
            return 1
        # Under a supervisor, exiting here would restart forever. Idle instead: this is a
        # legitimate configuration for a deployment that only serves what it shipped with.
        while not _stop:
            time.sleep(60)
        return 0

    def _handle(signum, _frame):
        global _stop
        _stop = True
        log.info("signal %s received — finishing the current page", signum)
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    log.info("pulling from %s into %s every %ds", PROCESSING_URL, CONTAINER_URL, args.interval)

    # Running alongside the application, this starts before the API finishes booting, and the
    # web server answers 502 until it does. Waiting turns an alarming traceback on every boot
    # into one quiet line.
    for _ in range(60):
        if _stop:
            return 0
        try:
            urllib.request.urlopen(f"{CONTAINER_URL}/docs", timeout=5)
            break
        except urllib.error.HTTPError as exc:
            # 502/503 is the web server reporting that the application behind it is not up
            # yet — exactly what we are waiting for. Any other status means it answered.
            if exc.code not in (502, 503):
                break
            time.sleep(5)
        except Exception:
            time.sleep(5)
    else:
        log.warning("the local API never answered; collecting anyway and relying on retries")
    try:
        _get_json("/api/sync/pending")
    except Exception as exc:                    # noqa: BLE001
        log.error("cannot reach the processing tier: %s", exc)
        log.error("check PROCESSING_URL, INGEST_SECRET, and HTTPS_PROXY")
        return 2

    if args.once or args.dry_run:
        one_pass(dry_run=args.dry_run)
        return 0

    while not _stop:
        one_pass(dry_run=False)
        for _ in range(args.interval):
            if _stop:
                break
            time.sleep(1)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
