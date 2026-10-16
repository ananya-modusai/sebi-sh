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
# While something is executing, five minutes between passes turns a progress bar into three
# jumps. Idle cost is unchanged because this only applies when a run is actually in flight.
ACTIVE_INTERVAL = int(os.environ.get("POLL_ACTIVE_INTERVAL_SECONDS", "10"))
PAGE = int(os.environ.get("POLL_PAGE_ROWS", "500"))
STATE_FILE = os.environ.get("POLL_STATE_FILE", "/var/lib/modus-poller/state.json")
TIMEOUT = int(os.environ.get("POLL_TIMEOUT_SECONDS", "120"))

log = logging.getLogger("poller")
_last_relay = 0.0
_last_users = 0.0


def _relay_due(min_gap: float = 20.0) -> bool:
    """True at most once every min_gap seconds.

    Collecting one company can spend ten minutes inside a single table uploading evidence,
    and a company that starts running during that window was invisible for all of it. This
    lets the blob loop check for new runs without issuing a request per file.
    """
    global _last_relay
    now = time.time()
    if now - _last_relay < min_gap:
        return False
    _last_relay = now
    return True


def _users_due(min_gap: float = 60.0) -> bool:
    """The same idea for the access list, on a slower clock.

    Accounts change far less often than runs do, so a minute is plenty — but it has to be
    its own clock rather than once per pass. A pass is as long as the collection backlog
    it is working through, and a twelve-company backlog kept an account granted in the
    control panel out of the local list for twenty minutes.
    """
    global _last_users
    now = time.time()
    if now - _last_users < min_gap:
        return False
    _last_users = now
    return True
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
def _send_blobs(rows: list[dict], column: str, seen: set[str],
                on_progress=None) -> int:
    """Make every blob a row points at exist in the container BEFORE the row does.

    Reports after each file because this is where the minutes go: a company with two hundred
    screenshots spends far longer here than in all the table pages combined, and counting
    only tables leaves the number motionless for the whole of it.
    """
    urls = sorted({r[column] for r in rows
                   if r.get(column) and str(r[column]).startswith("s3://")} - seen)
    total = len(urls)
    for i, url in enumerate(urls, 1):
        if on_progress is not None and (i == 1 or i % 5 == 0 or i == total):
            on_progress(i, total)
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

    Stamps the shared clock so a sync from here and one from `_users_mid` cannot both
    fire back to back — whichever happens first satisfies the other.
    """
    global _last_users
    users = _get_json("/api/sync/users")["users"]
    if dry_run:
        log.info("access list: %d user(s) — would apply", len(users))
        return 0
    _post_container("/users", {"users": users})
    _last_users = time.time()
    log.info("access list: %d user(s) applied", len(users))
    return len(users)


def sync_live(*, dry_run: bool) -> int:
    """Bring the local monitoring set in line with the processing tier.

    Rides with the access list rather than with collection, because putting a company live
    starts no run — there is nothing to collect, so the flag would otherwise never arrive.
    """
    live = _get_json("/api/sync/live")["live"]
    if dry_run:
        log.info("live set: %d company(ies) — would apply", len(live))
        return 0
    _post_container("/live", {"live": live})
    log.info("live set: %d company(ies) applied", len(live))
    return len(live)


def relay_active(*, dry_run: bool) -> int:
    """Mirror runs that are still executing, so their progress is visible here.

    The watchlist already polls its own API every ten seconds for any company whose run is
    queued or running, and already renders the step count as a percentage. None of that ever
    fired, because only finished runs were delivered and so no company was ever in a running
    state. Relaying the run and its steps while they are in flight is the only missing piece.

    Returns how many runs are active, which decides how soon the next pass happens.
    """
    data = _get_json("/api/sync/active")
    runs = data.get("runs") or []
    if not runs or dry_run:
        if runs:
            log.info("%d run(s) executing — would relay", len(runs))
        return len(runs)

    # A company that has never been collected has no row in the watchlist, so there is
    # nowhere for its progress to appear and a freshly-started run looks like nothing is
    # happening. Send a minimal company row first; the real one arrives with the data.
    # Only ever seed a real name. Falling back to the isin overwrote the names of companies
    # that were already here, leaving unrecognisable rows in their watchlist — the row this
    # was meant to create is worth far less than the ones it damaged.
    seed = [{"isin": r["isin"], "company_name": r["company_name"]}
            for r in runs
            if r.get("isin") and r.get("company_name")
            and r["company_name"] != r["isin"]]
    if seed:
        try:
            _post_container("/batches", {
                "schema_version": data["schema_version"],
                "batch_id": f"seed-{runs[0]['run_id']}",
                "run_id": runs[0]["run_id"], "finished_at": runs[0].get("started_at") or "",
                "isin": runs[0].get("isin") or "", "table": "companies", "mode": "upsert",
                "key": ["isin"], "seq": 1, "rows": seed})
        except Exception:                       # noqa: BLE001 - the real row follows anyway
            log.debug("could not seed the company row", exc_info=True)

    for table, rows in (("pipeline_runs", data.get("run_rows") or []),
                        ("pipeline_run_steps", data.get("step_rows") or [])):
        if not rows:
            continue
        # The id has to change as progress does, or the far side treats the update as a
        # duplicate of the batch before it and the bar never moves.
        stamp = hashlib.sha256(
            json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]
        _post_container("/batches", {
            "schema_version": data["schema_version"],
            "batch_id": f"live-{table}-{stamp}",
            "run_id": runs[0]["run_id"],
            "finished_at": runs[0].get("started_at") or "",
            "isin": runs[0].get("isin") or "",
            "table": table,
            "mode": "upsert",
            "key": ["run_id"] if table == "pipeline_runs" else ["run_id", "step_name"],
            "seq": 1,
            "rows": rows,
        })
    log.info("relayed %d run(s) in flight: %s", len(runs),
             ", ".join(f"{r['isin']} {r.get('current_step') or r['state']}" for r in runs[:3]))
    return len(runs)


def _report(isin: str, name: str, run_id: str, *, phase: str = "collecting",
            tables_done: int = 0, tables_total: int = 0,
            blobs_done: int = 0, blobs_total: int = 0, detail: str = "") -> None:
    """Say where we are with one company, into its own table on the far side.

    Fire and forget: progress is a courtesy and must never be able to interrupt or slow the
    data it describes. Blob counts are reported because fetching evidence files is most of
    the wait — counting only tables leaves the number frozen for minutes.
    """
    try:
        _post_container_path("/api/ingest/v1/collection", {
            "isin": isin, "company_name": name, "run_id": run_id, "phase": phase,
            "tables_done": tables_done, "tables_total": tables_total,
            "blobs_done": blobs_done, "blobs_total": blobs_total, "detail": detail})
    except Exception:                           # noqa: BLE001
        log.debug("could not report progress", exc_info=True)


def _post_container_path(path: str, payload: dict) -> None:
    body = json.dumps(payload, default=str).encode()
    st, raw, _ = _request("POST", f"{CONTAINER_URL}{path}", body=body,
                          headers={"Content-Type": "application/json"})
    if st not in (200, 201, 202):
        raise RuntimeError(f"POST {path} -> {st} {raw[:200]!r}")


def _relay_mid() -> None:
    """Pick up runs that started while we were busy, without stalling the upload."""
    if not _relay_due():
        return
    try:
        relay_active(dry_run=False)
    except Exception:                           # noqa: BLE001
        log.debug("mid-collection relay failed", exc_info=True)


def _users_mid() -> None:
    """Keep the access list and the live set current while we are busy collecting."""
    if not _users_due():
        return
    try:
        sync_users(dry_run=False)
    except Exception:                           # noqa: BLE001
        log.debug("mid-collection access-list sync failed", exc_info=True)
    try:
        sync_live(dry_run=False)
    except Exception:                           # noqa: BLE001
        log.debug("mid-collection live sync failed", exc_info=True)


def _mid_pass() -> None:
    """Everything that must keep happening while a long collection is under way.

    Both calls are self-gating, so this is cheap to invoke from a tight loop.
    """
    _relay_mid()
    _users_mid()


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

    # Collection takes minutes and nothing on screen said so: the company appeared with
    # whatever had arrived and its badge read COMPLETED, because that reflects the run, not
    # whether the data finished landing. Marking the run as still running here makes the
    # watchlist poll it and show progress, and the real state overwrites this at commit.
    name = plan.get("company_name") or plan["isin"]
    _report(plan["isin"], name, run_id,
            tables_done=len(plan["tables"]) - len(pending), tables_total=len(plan["tables"]),
            detail="starting")

    blobs_seen: set[str] = set()
    for table in pending:
        name_of_table, seq, after, rows_sent = table["name"], 0, None, 0
        name = plan.get("company_name") or plan["isin"]
        while True:
            seq += 1
            query = f"?seq={seq}&limit={PAGE}"
            if after:
                query += "&after=" + urllib.parse.quote("\t".join(after), safe="")
            page = _get_json(f"/api/sync/rows/{run_id}/{name_of_table}{query}")
            batch = page["rows"]
            if batch:
                if table["blob_column"]:
                    _send_blobs(batch, table["blob_column"], blobs_seen,
                                on_progress=lambda d, t: _report(
                                    plan["isin"], name, run_id,
                                    tables_done=len(progress["done_tables"]),
                                    tables_total=len(plan["tables"]),
                                    blobs_done=d, blobs_total=t,
                                    detail=f"evidence files {d}/{t}") or _mid_pass())
                _post_container("/batches", {
                    "schema_version": plan["schema_version"],
                    "batch_id": page["batch_id"], "run_id": run_id,
                    "finished_at": plan["finished_at"], "isin": plan["isin"],
                    "table": name_of_table, "mode": page["mode"], "key": page["key"],
                    "seq": seq, "rows": batch})
                rows_sent += len(batch)
                progress["batches"] += 1
                after = page["last_key"]
            # Checkpoint after every page, so a crash resumes here and not at the start.
            progress["counts"][name] = rows_sent
            _save_state(state)
            if page["exhausted"]:
                break
        # the TABLE, not the company — recording the company name here meant no table was
        # ever marked done, so every pass re-collected all of them and the count ran past
        # the total
        progress["done_tables"].append(name_of_table)
        _save_state(state)
        _report(plan["isin"], name, run_id,
                tables_done=len(progress["done_tables"]), tables_total=len(plan["tables"]),
                detail=name_of_table)
        # Collecting one company takes minutes, and the loop only came back here between
        # runs — so a company starting while another was being collected stayed invisible
        # for the whole of it. Checking between tables keeps the watchlist current.
        try:
            relay_active(dry_run=False)
        except Exception:                       # noqa: BLE001 - a hint must never stall data
            log.debug("could not refresh in-flight runs mid-collection", exc_info=True)
        log.info("  %-26s %d rows", name_of_table, rows_sent)

    # Commit swaps the replace-mode tables in one transaction; until this lands the run is
    # staged but invisible, which is what stops a half-pulled company from being shown.
    _post_container(f"/runs/{run_id}/commit", {
        "schema_version": plan["schema_version"],
        "run_id": run_id, "isin": plan["isin"], "run_type": plan["run_type"],
        "finished_at": plan["finished_at"], "expected_batches": progress["batches"],
        "counts": progress["counts"], "replace_tables": plan["replace_tables"]})

    # The progress marker and the real run row are the same row, and the marker was written
    # last — so a finished company sat on "collecting" indefinitely. Re-fetch the run row now
    # that everything else has landed, so the true state is what remains.
    try:
        final = _get_json(f"/api/sync/rows/{run_id}/pipeline_runs?seq=1&limit=5")
        if final.get("rows"):
            _post_container("/batches", {
                "schema_version": plan["schema_version"],
                "batch_id": f"final-{run_id}", "run_id": run_id,
                "finished_at": plan["finished_at"], "isin": plan["isin"],
                "table": "pipeline_runs", "mode": "upsert", "key": ["run_id"],
                "seq": 1, "rows": final["rows"]})
    except Exception:                           # noqa: BLE001 - the data is already delivered
        log.warning("could not write the final run state; the row may still read as collecting")

    status, raw, _ = _request("POST", f"{PROCESSING_URL}/api/sync/done/{run_id}")
    if status != 200:
        raise RuntimeError(f"could not confirm delivery: {status} {raw[:200]!r}")
    _report(plan["isin"], name, run_id, phase="done")
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
    try:
        sync_live(dry_run=dry_run)
    except Exception:                           # noqa: BLE001 - never block data on this
        log.exception("could not sync the live set; continuing with data")
    # Mirror anything executing before collecting finished work: an analyst watching a
    # company wants to see it move, and that signal is worthless if it arrives late.
    try:
        active = relay_active(dry_run=dry_run)
    except Exception:                           # noqa: BLE001 - never block collection on this
        log.exception("could not relay in-flight runs; continuing")
        active = 0
    waiting = _get_json("/api/sync/pending")["runs"]
    if not waiting:
        if not active:
            log.info("nothing waiting")
        return active
    log.info("%d run(s) waiting", len(waiting))
    collected = 0
    for run in waiting:
        if _stop:
            log.info("stopping — current run will resume from its last page")
            break
        _mid_pass()                             # between companies, not only at the top
        try:
            if collect(run, state, dry_run=dry_run):
                collected += 1
        except Exception:                       # noqa: BLE001 - one bad run must not stall the rest
            log.exception("run %s failed; will retry next pass", run["run_id"][:8])
    return max(collected, active)


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
    # Never exit because the far side is unreachable. Under a supervisor, exiting is fatal:
    # three quick failures and it stops trying, so a few minutes of downstream downtime ends
    # collection permanently and nothing says so until someone notices stale data. A link
    # that is down now is routinely up later, so the only correct response is to wait.
    try:
        _get_json("/api/sync/pending")
    except Exception as exc:                    # noqa: BLE001
        log.warning("cannot reach the processing tier yet: %s", exc)
        log.warning("check PROCESSING_URL, INGEST_SECRET and HTTPS_PROXY if this persists")
        if args.once or args.dry_run:
            return 2

    if args.once or args.dry_run:
        one_pass(dry_run=args.dry_run)
        return 0

    while not _stop:
        busy = one_pass(dry_run=False)
        wait = ACTIVE_INTERVAL if busy else args.interval
        for _ in range(wait):
            if _stop:
                break
            time.sleep(1)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
