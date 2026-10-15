#!/usr/bin/env bash
# ===========================================================================
#  SEBI deployment — DATABASE VM  (run this one FIRST)
#
#  Usage:   sudo bash deploy-db.sh
#
#  Everything is idempotent: re-running is safe and will not lose data.
#  Writes a full log to /tmp/modus-deploy-db.log — send that if anything fails.
# ===========================================================================
set -uo pipefail

IMAGE_TAR="${IMAGE_TAR:-modus-db-1.3.tar.gz}"   # the file you SFTP'd across
IMAGE="${IMAGE:-docker.io/olamzar/modus-db:1.3}"
NAME="modus-db"
PORT="${PORT:-5432}"
APP_VM_IP="${APP_VM_IP:-}"                       # restricts 5432 to the app VM; blank = any
LOG=/tmp/modus-deploy-db.log

exec > >(tee -a "$LOG") 2>&1
echo "=== modus db deploy — $(date) ==="

step(){ printf '\n>>> %s\n' "$1"; }
ok(){   printf '    OK   %s\n' "$1"; }
bad(){  printf '    FAIL %s\n' "$1"; }
die(){  printf '\n*** STOPPED: %s\n*** full log: %s\n' "$1" "$LOG"
        printf '*** SEND ANANYA JUST THE CODE IN BRACKETS, e.g. "%s" — that is enough.\n' \
               "$(printf '%s' "$1" | sed -n 's/^\[\([A-Z][0-9]*\)\].*/\1/p')"; exit 1; }

step "1/7  checking podman"
command -v podman >/dev/null || die "[D01] podman not installed"
ok "$(podman --version)"

step "2/7  loading the image"
if podman image exists "$IMAGE"; then
  ok "image already present, skipping load"
elif [ -f "$IMAGE_TAR" ]; then
  # Take the name podman actually registered rather than assuming it matches $IMAGE.
  # A tar saved under a different tag loads under that tag, and `podman run` would
  # then try to PULL from the internet — which does not exist on this VM, so it
  # hangs for minutes and fails with a DNS error that never mentions tag names.
  LOADED=$(podman load -i "$IMAGE_TAR" 2>&1 | tee /dev/stderr \
            | sed -n 's/.*[Ll]oaded image[(s)]*: *//p' | tail -1 | tr -d ' ')
  [ -n "$LOADED" ] || die "[D02] podman load failed — the file may be truncated; check its sha256"
  if [ "$LOADED" != "$IMAGE" ]; then
    bad "tar contained '$LOADED', not '$IMAGE' — using what was in the tar"
    IMAGE="$LOADED"
  fi
  ok "loaded $IMAGE"
else
  die "[D03] $IMAGE_TAR not found in $(pwd) — SFTP it here first"
fi
podman image exists "$IMAGE" \
  || die "[D04] image '$IMAGE' is not present locally and this VM cannot reach the internet.
    Check what you have:  podman images
    Then re-run with:     IMAGE=<the name listed> bash $(basename "$0")"

step "3/7  persistent storage"
# Without a volume the data lives in the container's writable layer and is destroyed by
# `podman rm` — which is what changing any setting below does.
PREEXISTING=no
if podman volume exists modus_pgdata 2>/dev/null; then
  PREEXISTING=yes
  ok "volume modus_pgdata already exists — the data load will be SKIPPED"
else
  podman volume create modus_pgdata >/dev/null
  ok "volume modus_pgdata created"
fi

step "4/7  starting the container"
podman rm -f "$NAME" >/dev/null 2>&1
# :Z relabels for SELinux. Without it RHEL blocks the mount and Postgres cannot start.
podman run -d --name "$NAME" --pull=never \
  -p "${PORT}:5432" \
  -v modus_pgdata:/var/lib/postgresql/data:Z \
  "$IMAGE" >/dev/null || die "[D05] podman run failed — see the log"
PORTMAP=$(podman port "$NAME" 2>/dev/null)
[ -n "$PORTMAP" ] || die "[D06] the container is running but NO ports were published, so the
    app VM will never reach this database. Usually a port conflict.
        podman rm -f $NAME
        ss -tlnp | grep :${PORT}
    Free the port, then run this script again."
ok "container started, published: $(echo "$PORTMAP" | tr '\n' ' ')"

step "5/7  waiting for first-time initialisation (loads ~175MB, up to 5 min)"
for i in $(seq 1 100); do
  if podman logs "$NAME" 2>&1 | grep -q "database system is ready to accept connections"; then
    if podman logs "$NAME" 2>&1 | grep -q "PostgreSQL init process complete" \
       || podman exec "$NAME" pg_isready -U modus -d surveillance >/dev/null 2>&1; then
      ok "database ready"; break
    fi
  fi
  podman ps -a --filter "name=$NAME" --format '{{.Status}}' | grep -q Exited && {
    podman logs --tail 30 "$NAME"; die "[D07] container exited during init"; }
  sleep 6
  [ "$i" = 100 ] && die "[D08] timed out waiting for init — see $LOG"
done

step "6/7  verifying the data"
TABLES=$(podman exec "$NAME" psql -U modus -d surveillance -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='surveillance_sebi'" 2>/dev/null | tr -d ' ')
COMPANIES=$(podman exec "$NAME" psql -U modus -d surveillance -tAc \
  "SELECT count(*) FROM surveillance_sebi.companies" 2>/dev/null | tr -d ' ')
USERS=$(podman exec "$NAME" psql -U modus -d surveillance -tAc \
  "SELECT count(*) FROM public.users WHERE hashed_password IS NOT NULL" 2>/dev/null | tr -d ' ')
echo "    tables=$TABLES  companies=$COMPANIES  users_with_passwords=$USERS"
[ "${TABLES:-0}" -ge 36 ] && ok "schema loaded" || bad "[D09] expected 36 tables, found ${TABLES:-0}"
if [ "${COMPANIES:-0}" -ge 240 ]; then
  ok "company data loaded"
else
  bad "[D10] expected ~247 companies, found ${COMPANIES:-0}"
  # Postgres only runs its init scripts when the data directory is EMPTY. A volume
  # left behind by a run that died mid-load will never be filled in, and every
  # later run looks like it succeeded while the data stays wrong.
  if [ "$PREEXISTING" = yes ]; then
    die "[D11] this volume already had data, so the load was skipped and it is incomplete.
    Wipe it and run this script again:
        podman rm -f $NAME
        podman volume rm modus_pgdata
        sudo APP_VM_IP=\${APP_VM_IP:-<app-vm-ip>} bash $(basename "$0")"
  fi
  die "[D12] the data did not load. Send $LOG"
fi

step "7/7  firewall and auto-start"
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  if [ -n "$APP_VM_IP" ]; then
    # Only the app VM should ever reach the database.
    firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${APP_VM_IP}/32 port port=${PORT} protocol=tcp accept" >/dev/null 2>&1
    ok "5432 opened to ${APP_VM_IP} only"
  else
    firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1
    bad "[D13] 5432 opened to ALL — rerun with APP_VM_IP=<app vm ip> to restrict it"
  fi
  firewall-cmd --reload >/dev/null 2>&1
else
  bad "[D14] firewalld not running — open ${PORT}/tcp yourself if needed"
fi

# podman has no daemon, so --restart does not survive a reboot. systemd does.
if command -v systemctl >/dev/null; then
  podman generate systemd --new --name "$NAME" --files --restart-policy=always >/dev/null 2>&1 \
    && mv -f "container-${NAME}.service" /etc/systemd/system/ 2>/dev/null \
    && systemctl daemon-reload \
    && systemctl enable "container-${NAME}.service" >/dev/null 2>&1 \
    && ok "will restart automatically after a reboot" \
    || bad "[D15] could not install the systemd unit (container still runs until reboot)"
fi

echo
echo "==========================================================="
echo " DATABASE VM DONE"
echo "   container : $NAME"
echo "   port      : $PORT"
echo "   db/user   : surveillance / modus"
echo "   THIS VM'S IP — you need it for the app VM:"
ip -4 addr show scope global 2>/dev/null | awk '/inet /{print "     "$2}' | head -3
echo "   log       : $LOG"
echo "==========================================================="
