# Modus surveillance — deployment scripts

Scripts to bring the two containers up. **Download the ZIP** (green *Code* button →
*Download ZIP*) rather than cloning — `git clone` is blocked on some networks, plain
HTTPS downloads are not.

No credentials are in this repository. Sign-in details are issued separately.

---

## Windows (Docker Desktop) — testing

Unzip, then **double-click**:

```
windows-start.cmd
```

It pulls both images, starts them, waits for the database to load, checks that
everything answers, and prints the URL to open. First run takes 10–15 minutes
because the images are about 4 GB. Running it again takes seconds — it reuses
the data.

| File | What it does |
|---|---|
| `windows-start.cmd` | brings everything up and verifies it |
| `windows-collect.cmd` | when something breaks — writes a report to your Desktop and opens it |
| `windows-stop.cmd` | stops both containers, keeps the data |

**Requires Docker Desktop to be running** — the whale icon in the system tray
must be steady, not animating.

---

## RHEL VMs (podman) — the real deployment

Two VMs. **Database first**, because the app needs its address.

```bash
# fix line endings first if these files passed through Windows
sed -i 's/\r$//' *.sh

# --- DB VM ---
sudo APP_VM_IP=<app-vm-ip> bash deploy-db.sh
#   prints this VM's IP at the end — you need it next

# --- APP VM ---
sudo DB_VM_IP=<db-vm-ip> THIS_VM_IP=<app-vm-ip> INGEST_SECRET=<secret> bash deploy-app.sh
```

`THIS_VM_IP` **must be the address analysts type in the browser.** Evidence-image
links are cryptographically signed against it. Get it wrong and every page works
except the images, which return 403 — the most misleading failure in the system.

When something breaks:

```bash
sudo bash collect.sh
```

Writes a full report to `/tmp/` and prints a short summary sized to photograph.

---

## The images

```
olamzar/modus-app:2.4     application (UI + API + web server + object store)
olamzar/modus-db:1.3      database (PostgreSQL 17)
```

Both public, both `linux/amd64`. Use these exact tags — earlier ones have known
faults, including `2.3`, which rejects every administrator request.

Air-gapped machines have no registry access, so pull on a machine that does, then:

```
docker save olamzar/modus-app:2.4 | gzip > modus-app-2.4.tar.gz
docker save olamzar/modus-db:1.3  | gzip > modus-db-1.3.tar.gz
```

Transfer those, put them beside the scripts, and the scripts load them instead of
pulling. Keep the tag spelling exactly as above — the scripts look the images up by
that name, and a mismatch makes them try to reach the internet and hang.
