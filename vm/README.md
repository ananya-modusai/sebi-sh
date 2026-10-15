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
sha256sum modus-db-1.3.tar.gz
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
| `2/7 loading the image` | `OK  loaded docker.io/olamzar/modus-db:1.3` |
| `3/7 persistent storage` | `OK  volume modus_pgdata created` |
| `4/7 starting the container` | `OK  container started, published: 0.0.0.0:5432->5432/tcp` |
| `5/7 waiting for init` | `OK  database ready` (up to 5 minutes) |
| `6/7 verifying the data` | `tables=36  companies=247  users_with_passwords=5` |
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
sudo DB_VM_IP=<db-vm-ip> THIS_VM_IP=<app-vm-ip> INGEST_SECRET=<secret> bash deploy-app.sh
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
| `0/8 checking what you passed in` | `OK  database at <ip>, this VM is <ip>, serving on :3000` |
| `1/8 checking podman` | `OK` |
| `2/8 loading the image` | `OK  loaded docker.io/olamzar/modus-app:2.4` (slow, it is large) |
| `3/8 can this VM reach the database?` | `OK  <db-ip>:5432 reachable` |
| `4/8 persistent storage` | `OK  volume modus_blobs` |
| `5/8 starting the container` | `OK  container started, published: 0.0.0.0:3000->8080/tcp` |
| `6/8 waiting for the application` | `OK  application up` |
| `7/8 checking it serves` | `OK  UI responds` / `OK  API responds` |
| `8/8 firewall and auto-start` | `OK  3000/tcp opened` / `OK  will restart after a reboot` |

Then open `http://<app-vm-ip>:3000` and sign in.

**Check that evidence screenshots render.** That is the single check that proves
`THIS_VM_IP` was right. Everything else can pass while this is broken.

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

**Only port 3000 should ever be published** on the application VM. The object store
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
| `D08` | load took over 10 minutes | slow disk; re-run, or send the log |
| `D09` | fewer than 36 tables | schema did not load |
| `D10` | fewer than 247 companies | data did not load; see `D11` |
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
| `A09` | port 3000 already in use | `ss -tlnp \| grep :3000` |
| `A10` | running but no ports published | port conflict |
| `A11` | `podman run` refused | send the log |
| `A12` | container died at startup | usually cannot reach the database |
| `A13` | app took over 4 minutes | send the log |
| `A14` | UI not answering 200 | send the log |
| `A15` | API not answering 200 | send the log |
| `A16` | could not open 3000 | open it by hand |
| `A17` | systemd unit not installed | runs now, not after a reboot |

### What "it worked" looks like

Two numbers are enough to tell me everything is fine:

```
DB VM  : tables=36  companies=247
APP VM : UI responds / API responds
```
