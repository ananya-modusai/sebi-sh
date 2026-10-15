# Workstation (Windows + Docker Desktop)

This machine has internet. It does two jobs:

1. **Test** the containers locally before touching the VMs
2. **Package** the images as files, because the VMs have no internet

No credentials in this folder. Sign-in details are issued separately.

**Docker Desktop must be running** — the whale icon in the system tray steady, not
animating — before anything below.

---

## Job 1 — test it here

Double-click:

```
windows-start.cmd
```

First run takes 10–15 minutes (about 4 GB of images). Re-runs take seconds because
the data is kept in volumes.

It prints progress as it goes. What you should see:

| Step | Expect |
|---|---|
| `[1/7] Checking Docker` | `OK` |
| `[2/7] Downloading images` | `OK` (or "already here") |
| `[3/7] Network, storage...` | `ports 3000 and 5432 are free` |
| `[4/7] Starting the database` | `started` |
| `[5/7] Loading data` | **`loaded - 247 companies`** |
| `[6/7] Starting the application` | `ports published: 8080/tcp -> 0.0.0.0:3000` |
| `[7/7] Waiting for it to answer` | `READY` |

Then the summary:

```
companies in database : 247
UI                    : 200     (want 200)
API docs              : 200     (want 200)
data endpoint         : 401     (401 is CORRECT - auth is working)
```

**`401` on the data endpoint is a pass.** It means the API is alive and authentication
is switched on. A `200` there would be the bug.

### Then check it by hand

Open **http://localhost:3000** and sign in.

1. A company list appears → the app reached the database
2. Open a company → posts load
3. **Evidence screenshots render** ← the one that matters most
4. Network graph tab opens
5. Admin → Users loads

Step 3 is the highest-value check in the whole runbook. If text loads but images are
broken, `BLOB_PUBLIC_URL` is wrong — and nothing else will look wrong. Everything
else can pass while the product is useless to an analyst.

### If a step fails

```
windows-collect.cmd
```

Writes `modus-report.txt` to your Desktop and opens it. Select all, copy, send it.

### When you are done

```
windows-stop.cmd
```

Stops both containers, keeps the data.

---

## Job 2 — package the images for the VMs

Double-click:

```
windows-save.cmd
```

Takes 15–25 minutes, writes about 1.9 GB to `Desktop\modus-transfer\`:

```
modus-db-1.3.tar.gz
modus-app-2.5.tar.gz
checksums.txt
```

Then:

1. Move both files to the VMs with **ARCON PAM**
   - `modus-db-1.3.tar.gz` → the **database** VM
   - `modus-app-2.5.tar.gz` → the **application** VM
2. On each VM, confirm it arrived intact:
   ```
   sha256sum modus-db-1.3.tar.gz
   ```
   It must match `checksums.txt`. **Check this every time** — a truncated transfer
   fails later with an error that says nothing about truncation, and you will spend
   an hour on the wrong problem.
3. Copy the `vm/` scripts to the same directory and follow `vm/README.md`.

---

## Opening a shell on the VMs

Through **ARCON PAM**, not plain SSH:

1. Open the ARCON portal in the browser on this machine
2. Authenticate as yourself
3. Pick the target VM from your entitled assets
4. PAM opens the terminal, injecting the credentials itself

You never type an IP or a root password. File transfer uses PAM's own transfer
feature, not `scp`.

**Sessions are recorded, and they time out when idle.** `podman load` on the
application image takes a while — if the session drops mid-load, see the recovery
note in `vm/README.md`, because an interrupted database load needs cleaning up
before you retry.

---

## Failure codes — send me just the code

If `windows-start.cmd` stops, it prints a code in brackets. **Text me the code only.**

| Code | Meaning | First thing to try |
|---|---|---|
| `W01` | Docker Desktop not running | start it, wait for the steady whale |
| `W02` | could not pull the database image | network or proxy |
| `W03` | could not pull the application image | network or proxy; retry, pulls resume |
| `W04` | port 3000 or 5432 already in use | `netstat -ano \| findstr ":3000 "` |
| `W05` | database would not start | `docker logs modus-db` |
| `W06` | database never finished loading | usually disk space |
| `W07` | application would not start | `docker logs modus-app` |
| `W08` | running but no ports published | port conflict — run `windows-stop.cmd`, retry |
| `W09` | application never answered | `docker logs modus-app` |

### What "it worked" looks like

One line is enough:

```
companies 247 / UI 200 / API 200 / data 401
```

If all four match, everything is fine. **`401` is correct** — it means authentication
is on.
