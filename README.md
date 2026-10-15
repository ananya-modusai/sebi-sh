# Modus surveillance — deployment

**Download the ZIP** (green *Code* button → *Download ZIP*). `git clone` is blocked on
some networks; plain HTTPS downloads are not.

No credentials anywhere in this repository. Sign-in details are issued separately.

### Which folder

| You are on | Use | For |
|---|---|---|
| the **Windows workstation** | [`workstation/`](workstation/) | testing locally, and packaging the images for the VMs |
| a **RHEL VM** | [`vm/`](vm/) | the real deployment |

### Order on the day

1. `workstation/` — test it works here
2. `workstation/windows-save.cmd` — turn the images into two `.tar.gz` files
3. Move them to the VMs with ARCON PAM, plus the `vm/` folder
4. `vm/` — database VM first, then the application VM
5. `vm/poller.py` — installs the data feed once both are up

### If something fails

Every stopping point prints a code in brackets — `D11`, `A08`, `W06`.
**Send the code, nothing else.** Each folder's README has the full table.

Working looks like:

```
workstation : companies 247 / UI 200 / API 200 / data 401
DB VM       : tables=36  companies=20
APP VM      : UI responds / API responds
```

`401` is correct — it means authentication is on.
