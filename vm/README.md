# The two VMs (RHEL + podman)

Two machines. **Database first** — the application needs its address.

Everything here is idempotent: re-running is safe.

No credentials in this folder. Sign-in details are issued separately.

---

## 0. Before anything, on BOTH VMs

```bash
sed -i 's/\r$//' *.sh
```

These files crossed a Windows machine. If any carries Windows line endings, bash
fails with `$'\r': command not found`, which tells you nothing. One second, every
time, no downside.

Then confirm the image arrived intact:

```bash
sha256sum modus-db-2.1.tar.gz
```

Must match `checksums.txt` from the workstation. **Do not skip this.** A truncated
file fails at load with a confusing error.

---

## 1. Database VM

```bash
sudo APP_VM_IP=<app-vm-ip> bash deploy-db.sh
```

`APP_VM_IP` restricts port 5432 to the application VM. Leave it out and 5432 opens
to everything, which the script will warn you about.

Expected, in order:

| Step | Expect |
|---|---|
| `1/7 checking podman` | `OK  podman version 4.x` |
| `2/7 loading the image` | `OK  loaded docker.io/olamzar/modus-db:2.1` |
| `3/7 persistent storage` | `OK  volume modus_pgdata created` |
| `4/7 starting the container` | `OK  container started, published: 0.0.0.0:5432->5432/tcp` |
| `5/7 waiting for init` | `OK  database ready` — **up to 30 minutes on a slow disk** |
| `6/7 verifying the data` | `tables=36  companies=20  users_with_passwords=5` |
| `7/7 firewall and auto-start` | `OK  5432 opened to <app-vm-ip> only` |

It finishes by printing **this VM's IP address**. Write it down — the application VM
needs it.

### If step 6 reports the wrong numbers

The script stops and tells you what to do. It is almost always an interrupted first
load: PostgreSQL only populates an **empty** data directory, so a load that died
halfway leaves a volume that will never fill in, and every later run looks fine with
wrong data.

```bash
podman rm -f modus-db
podman volume rm modus_pgdata
sudo APP_VM_IP=<app-vm-ip> bash deploy-db.sh
```

---

## 2. Application VM

```bash
sudo DB_VM_IP=<db-vm-ip> THIS_VM_IP=<app-vm-ip> PORT=8091 INGEST_SECRET=<secret> bash deploy-app.sh
```

**`THIS_VM_IP` must be the address analysts type in their browser.** Evidence-image
links are cryptographically signed against it. Get it wrong and every page works
except the images, which return 403 — and nothing in any log will mention it. If
analysts will reach this box by hostname, use the hostname.

`INGEST_SECRET` must match the processing tier exactly. Without it the application
runs fine but rejects incoming data with `503 ingest is not configured`.

Expected, in order:

| Step | Expect |
|---|---|
| `0/8 checking what you passed in` | `OK  database at <ip>, this VM is <ip>, serving on :8091` |
| `1/8 checking podman` | `OK` |
| `2/8 loading the image` | `OK  loaded docker.io/olamzar/modus-app:3.3` (slow, it is large) |
| `3/8 can this VM reach the database?` | `OK  <db-ip>:5432 reachable` |
| `4/8 persistent storage` | `OK  volume modus_blobs` |
| `5/8 starting the container` | `OK  container started, published: 0.0.0.0:8091->8080/tcp` |
| `6/8 waiting for the application` | `OK  application up` |
| `7/8 checking it serves` | `OK  UI responds` / `OK  API responds` |
| `8/8 firewall and auto-start` | `OK  8091/tcp opened` / `OK  will restart after a reboot` |

Then open `http://<app-vm-ip>:8091` and sign in.

**Check that evidence screenshots render.** That is the single check that proves
`THIS_VM_IP` was right. Everything else can pass while this is broken.

---

## 2a. Is it working?

```bash
sudo bash check.sh
```

Four lines and a verdict, on either VM. **Always with `sudo`** — it says so itself if you
forget.

If you only want one command, this is the one that matters:

```bash
sudo podman exec modus-db psql -U modus -d surveillance -tAc \
     "SELECT count(*) FROM surveillance_sebi.companies"
```

A number means the database is fine. An error or `0` means it is not.

---

## 2b. Two things that look like failure and are not

**`podman ps` shows nothing.** Containers belong to the user that started them. The
scripts run under `sudo`, so the containers are root's:

```bash
sudo podman ps -a
```

Plain `podman ps` lists *your* containers and will be empty. This catches everyone once.
To stop it catching you twice:

```bash
echo "alias podman='sudo podman'" >> ~/.bashrc && source ~/.bashrc
```

**A script times out but the container carries on.** Giving up on waiting does not stop
anything. Postgres keeps loading; the application keeps retrying the database. If a script
reports a timeout, check the real state before redoing anything:

```bash
sudo podman exec modus-db psql -U modus -d surveillance -tAc \
     "SELECT count(*) FROM surveillance_sebi.companies"     # want 20
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8091/api/companies   # 401 = healthy
```

Both good means the deployment succeeded and the script simply lost patience.

---

## 3. When something goes wrong

On either VM:

```bash
sudo bash collect.sh
```

Writes a full report to `/tmp/modus-report-HHMM.txt` and prints a short summary
sized to photograph. Passwords are redacted.

Send the file if you can move it; photograph the summary if you cannot.

---

## Notes that save time

**Only the one published port (8091) should ever be exposed** on the application VM. The object store
is deliberately bound to `127.0.0.1` inside the container and proxied — publishing
9000 would expose it directly.

**podman has no daemon**, so `--restart` does not survive a reboot. The scripts
install systemd units instead. If that step reports FAIL, the containers still run
until the next reboot.

**Volumes hold all the data.** `podman rm` is safe; removing a volume is not.
`modus_pgdata` and `modus_blobs` must be wiped together or not at all — the database
tracks which evidence files exist, so removing one and keeping the other leaves
records pointing at missing files, and re-pushing cannot repair it.

**If the image name does not match**, the scripts use whatever was in the tar and
tell you. They never fall back to pulling from the internet, which would hang.

---

## Failure codes — send me just the code

Every stopping point prints a code in brackets. **Text me the code and nothing else.**
`D11` tells me as much as a full log would.

### Database VM (`D..`)

| Code | Meaning | First thing to try |
|---|---|---|
| `D01` | podman not installed | wrong VM, or podman was never installed |
| `D02` | tar is corrupt or truncated | re-transfer; check `sha256sum` |
| `D03` | tar file not in this directory | `ls` — move it here |
| `D04` | image not loaded and no internet | `podman images`, then `IMAGE=<name> bash deploy-db.sh` |
| `D05` | `podman run` refused | send the log |
| `D06` | running but no ports published | port conflict — `ss -tlnp \| grep :5432` |
| `D07` | container died during load | usually disk space — `df -h` |
| `D08` | load took over 30 minutes | probably STILL loading — check the count before re-running |
| `D09` | fewer than 36 tables | schema did not load |
| `D10` | fewer than 20 companies | data did not load; see `D11` |
| `D11` | **volume was left half-loaded** | `podman volume rm modus_pgdata` then re-run |
| `D12` | data did not load, fresh volume | send the log |
| `D13` | 5432 opened to everything | harmless; re-run with `APP_VM_IP=` to restrict |
| `D14` | firewalld not running | open 5432 by hand |
| `D15` | systemd unit not installed | runs now, will not survive a reboot |

### Application VM (`A..`)

| Code | Meaning | First thing to try |
|---|---|---|
| `A01` | `DB_VM_IP` not given | re-run with it |
| `A02` | `THIS_VM_IP` not given | re-run with the browser-facing address |
| `A03` | `INGEST_SECRET` empty | app works, but incoming data gets 503 |
| `A04` | podman not installed | wrong VM |
| `A05` | tar corrupt or truncated | re-transfer; check `sha256sum` |
| `A06` | tar not in this directory | move it here |
| `A07` | image not loaded, no internet | `podman images`, then pass `IMAGE=` |
| `A08` | cannot reach the database VM | DB VM firewall, or it is not running |
| `A09` | the published port is already in use | `ss -tlnp \| grep :8091` |
| `A10` | running but no ports published | port conflict |
| `A11` | `podman run` refused | send the log |
| `A12` | container died at startup | usually cannot reach the database |
| `A13` | app took over 15 minutes | usually still waiting on the DB; it retries forever |
| `A14` | UI not answering 200 | send the log |
| `A15` | API not answering 200 | send the log |
| `A16` | could not open the port | open it by hand |
| `A17` | systemd unit not installed | runs now, not after a reboot |

### What "it worked" looks like

Two numbers are enough to tell me everything is fine:

```
DB VM  : tables=36  companies=20
APP VM : UI responds / API responds
```

---

## 4. Data delivery — the poller

This VM cannot be reached from outside, so nothing is pushed to it. The poller asks the
processing tier what is waiting, fetches it, and feeds it into the application container
over localhost.

It ships **inside `modus-app:3.3`** and runs as a second container from that same image,
so there is nothing extra to install and everything stays containerised.

### Start it

```bash
podman run -d --name modus-poller --restart=always \
  --network host --user 0 \
  -v modus_poller_state:/state:Z \
  -e PROCESSING_URL=https://sebi-processing-api.modusai.net \
  -e INGEST_KEY_ID=k1 \
  -e INGEST_SECRET=CHANGE-ME \
  -e CONTAINER_URL=http://localhost:8091 \
  -e HTTPS_PROXY=http://CHANGE-ME:PORT \
  -e HTTP_PROXY=http://CHANGE-ME:PORT \
  -e NO_PROXY=localhost,127.0.0.1 \
  -e POLL_INTERVAL_SECONDS=300 \
  -e POLL_STATE_FILE=/state/state.json \
  --entrypoint python3 \
  docker.io/olamzar/modus-app:3.3 /opt/modus/poller.py
```

Five values to change: `PROCESSING_URL`, `INGEST_SECRET`, the two proxy lines, and
`CONTAINER_URL` if the published port is not 8091.

Three flags that are not optional:

| Flag | Why |
|---|---|
| `--user 0` | the image runs as uid 999 and cannot write to the state volume otherwise |
| `--network host` | lets it reach the application on `localhost:8091` |
| `NO_PROXY=localhost,127.0.0.1` | without it, it tries to reach its own container through SEBI's proxy, which refuses |

If host networking is not permitted, drop `--network host`, put both containers on one
podman network, and use `-e CONTAINER_URL=http://modus-app:8080`.

### Test before leaving it running

```bash
podman run --rm --network host --user 0 \
  -e PROCESSING_URL=... -e INGEST_SECRET=... -e HTTPS_PROXY=... \
  -e CONTAINER_URL=http://localhost:8091 -e NO_PROXY=localhost,127.0.0.1 \
  --entrypoint python3 docker.io/olamzar/modus-app:3.3 /opt/modus/poller.py --dry-run
```

| Output | Meaning |
|---|---|
| `nothing waiting` | **working** — connected, authenticated, queue empty |
| `N run(s) waiting` + a table list | **working**, and there is data to collect |
| `cannot reach the processing tier` | proxy or URL wrong |
| `-> 401` | `INGEST_SECRET` does not match ours |
| `-> 404` | our side has not been deployed yet — tell Ananya |
| `-> 503` | our side has no secret configured — tell Ananya |

Then one real pass with `--once` instead of `--dry-run`. Expect a line per table and
`run <id> complete — N batches`.

### Watching it

```bash
podman logs -f modus-poller
```

### Notes

Interrupting it is safe. Progress is checkpointed after **every page**, and re-sending is
free — the ingest API deduplicates batches by id and blobs by sha256. A dropped link costs
one repeated page, never a half-loaded company.

A company only becomes visible when its run **commits**, so analysts never see a partial
pull.

`poller.py` and `modus-poller.service` are also in this folder if you would rather run it
with systemd on the host than as a container.
