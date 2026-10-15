#!/usr/bin/env bash
# ===========================================================================
#  SEBI deployment — APP VM  (run AFTER the database VM)
#
#  Usage:   sudo DB_VM_IP=10.x.x.x THIS_VM_IP=10.y.y.y bash deploy-app.sh
#
#  DB_VM_IP    the database VM's address, printed at the end of deploy-db.sh
#  THIS_VM_IP  this VM's address, EXACTLY as analysts will type it in a browser.
#              Evidence-image links are signed against it; get it wrong and
#              everything works except images, which return 403.
#
#  Idempotent. Full log at /tmp/modus-deploy-app.log — send that if it fails.
# ===========================================================================
set -uo pipefail

IMAGE_TAR="${IMAGE_TAR:-modus-app-2.5.tar.gz}"
IMAGE="${IMAGE:-docker.io/olamzar/modus-app:2.5}"
NAME="modus-app"
PORT="${PORT:-3000}"                       # what SEBI opened; matches the WhatsApp thread
DB_VM_IP="${DB_VM_IP:-}"
THIS_VM_IP="${THIS_VM_IP:-}"
INGEST_SECRET="${INGEST_SECRET:-}"         # must match the processing tier exactly
LOG=/tmp/modus-deploy-app.log

exec > >(tee -a "$LOG") 2>&1
echo "=== modus app deploy — $(date) ==="

step(){ printf '\n>>> %s\n' "$1"; }
ok(){   printf '    OK   %s\n' "$1"; }
bad(){  printf '    FAIL %s\n' "$1"; }
die(){  printf '\n*** STOPPED: %s\n*** full log: %s\n' "$1" "$LOG"
        printf '*** SEND ANANYA JUST THE CODE IN BRACKETS, e.g. "%s" — that is enough.\n' \
               "$(printf '%s' "$1" | sed -n 's/^\[\([A-Z][0-9]*\)\].*/\1/p')"; exit 1; }

step "0/8  checking what you passed in"
[ -n "$DB_VM_IP" ]   || die "[A01] DB_VM_IP is not set. Run: sudo DB_VM_IP=<db vm ip> THIS_VM_IP=<this vm ip> bash deploy-app.sh"
[ -n "$THIS_VM_IP" ] || die "[A02] THIS_VM_IP is not set — it is what analysts type in the browser"
ok "database at $DB_VM_IP, this VM is $THIS_VM_IP, serving on :$PORT"
[ -n "$INGEST_SECRET" ] || bad "[A03] INGEST_SECRET empty — the processing tier cannot push until it is set"

step "1/8  checking podman"
command -v podman >/dev/null || die "[A04] podman not installed"
ok "$(podman --version)"

step "2/8  loading the image (this one is large, be patient)"
if podman image exists "$IMAGE"; then
  ok "image already present, skipping load"
elif [ -f "$IMAGE_TAR" ]; then
  LOADED=$(podman load -i "$IMAGE_TAR" 2>&1 | tee /dev/stderr \
            | sed -n 's/.*[Ll]oaded image[(s)]*: *//p' | tail -1 | tr -d ' ')
  [ -n "$LOADED" ] || die "[A05] podman load failed — the file may be truncated; check its sha256"
  if [ "$LOADED" != "$IMAGE" ]; then
    bad "tar contained '$LOADED', not '$IMAGE' — using what was in the tar"
    IMAGE="$LOADED"
  fi
  ok "loaded $IMAGE"
else
  die "[A06] $IMAGE_TAR not found in $(pwd) — SFTP it here first"
fi
podman image exists "$IMAGE" \
  || die "[A07] image '$IMAGE' is not present locally and this VM cannot reach the internet.
    Check what you have:  podman images
    Then re-run with:     IMAGE=<the name listed> DB_VM_IP=$DB_VM_IP THIS_VM_IP=$THIS_VM_IP bash $(basename "$0")"

step "3/8  can this VM reach the database?"
if timeout 8 bash -c "</dev/tcp/${DB_VM_IP}/5432" 2>/dev/null; then
  ok "${DB_VM_IP}:5432 reachable"
else
  bad "[A08] cannot reach ${DB_VM_IP}:5432 — check the firewall on the DB VM, or that it is running"
  echo "    continuing anyway; the app retries, but it will show no data until this works"
fi

step "4/8  persistent storage for evidence files"
# Blobs pushed after deployment live here. Without the volume they are inside the
# container and are destroyed by `podman rm` — which is what changing any setting does.
podman volume exists modus_blobs 2>/dev/null || podman volume create modus_blobs >/dev/null
ok "volume modus_blobs"

step "5/8  starting the container"
# Containers from an EARLIER image tag keep their published ports. podman can then
# start this one while publishing nothing, and `podman ps` still looks healthy while
# port 3000 refuses every connection — the analyst sees a dead product.
for C in $(podman ps -a --format '{{.ID}} {{.Image}}' 2>/dev/null | grep -i olamzar | awk '{print $1}'); do
  podman rm -f "$C" >/dev/null 2>&1 && bad "removed a leftover container ($C)"
done
if command -v ss >/dev/null && ss -tln 2>/dev/null | grep -q ":${PORT} "; then
  die "[A09] something else is already listening on ${PORT}.
    Find it with:  ss -tlnp | grep :${PORT}
    Stop that, then run this script again."
fi
podman rm -f "$NAME" >/dev/null 2>&1
podman run -d --name "$NAME" --pull=never \
  -p "${PORT}:8080" \
  -v modus_blobs:/data/seaweed:Z \
  -e DB_HOST="$DB_VM_IP" \
  -e DB_PORT=5432 \
  -e DB_NAME=surveillance \
  -e DB_USER=modus \
  -e DB_PASSWORD=modus \
  -e SSH_TUNNEL_ENABLED=false \
  -e DEPLOY_ENV=onprem \
  -e SURVEILLANCE_SCHEMA=surveillance_sebi \
  -e BLOB_PUBLIC_URL="http://${THIS_VM_IP}:${PORT}" \
  -e REPORT_APP_URL="http://localhost:8080/" \
  -e SEBI_ADMIN_EMAIL=ananya@modussecure.com \
  -e INGEST_KEY_ID=modus-processing \
  -e INGEST_SECRET="$INGEST_SECRET" \
  "$IMAGE" >/dev/null || die "[A11] podman run failed — see the log"
PORTMAP=$(podman port "$NAME" 2>/dev/null)
[ -n "$PORTMAP" ] || die "[A10] the container is running but NO ports were published, so
    nothing can reach it. Usually a port conflict.
        podman rm -f $NAME
        ss -tlnp | grep :${PORT}
    Free the port, then run this script again."
ok "container started, published: $(echo "$PORTMAP" | tr '\n' ' ')"

step "6/8  waiting for the application"
for i in $(seq 1 60); do
  podman logs "$NAME" 2>&1 | grep -q "Application startup complete" && { ok "application up"; break; }
  podman ps -a --filter "name=$NAME" --format '{{.Status}}' | grep -q Exited && {
    podman logs --tail 30 "$NAME"; die "[A12] container exited at startup"; }
  sleep 4
  [ "$i" = 60 ] && { podman logs --tail 20 "$NAME"; die "[A13] timed out waiting for startup"; }
done

step "7/8  checking it serves"
sleep 5
code(){ curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$1" 2>/dev/null; }
[ "$(code http://localhost:${PORT}/)" = 200 ]      && ok "UI responds"       || bad "[A14] UI did not return 200"
[ "$(code http://localhost:${PORT}/docs)" = 200 ]  && ok "API responds"      || bad "[A15] API did not return 200"
[ "$(code http://localhost:${PORT}/api/v1/auth/send-otp)" = 405 ] \
  || true   # 405 just means GET on a POST route; presence is what matters

step "8/8  firewall and auto-start"
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="${PORT}/tcp" >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 \
    && ok "${PORT}/tcp opened" || bad "[A16] could not open ${PORT}/tcp"
else
  bad "firewalld not running — open ${PORT}/tcp yourself if needed"
fi

if command -v systemctl >/dev/null; then
  podman generate systemd --new --name "$NAME" --files --restart-policy=always >/dev/null 2>&1 \
    && mv -f "container-${NAME}.service" /etc/systemd/system/ 2>/dev/null \
    && systemctl daemon-reload \
    && systemctl enable "container-${NAME}.service" >/dev/null 2>&1 \
    && ok "will restart automatically after a reboot" \
    || bad "[A17] could not install the systemd unit (container still runs until reboot)"
fi

echo
echo "==========================================================="
echo " APP VM DONE"
echo "   open      : http://${THIS_VM_IP}:${PORT}"
echo "   sign in   : the account and password issued to you separately"
echo "   log       : $LOG"
echo
echo " If pages load but images are missing, THIS_VM_IP did not match what"
echo " you typed in the browser. Re-run with the address analysts actually use."
echo "==========================================================="
