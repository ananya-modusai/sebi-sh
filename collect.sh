#!/usr/bin/env bash
# ===========================================================================
#  When something breaks:   sudo bash collect.sh
#
#  Gathers everything needed to diagnose into ONE small text file, then prints
#  a short summary. Send the file — or if you can only retype a few lines, the
#  summary at the bottom is usually enough to identify the problem.
#
#  Deliberately small: no image layers, no data, nothing sensitive beyond
#  hostnames and ports. Passwords are redacted.
# ===========================================================================
set -uo pipefail
OUT="/tmp/modus-report-$(date +%H%M).txt"

{
echo "=========== MODUS DEPLOYMENT REPORT  $(date) ==========="
echo
echo "--- host ---"
hostname; cat /etc/redhat-release 2>/dev/null; uname -r
echo "selinux: $(getenforce 2>/dev/null || echo n/a)"
echo "podman : $(podman --version 2>/dev/null || echo MISSING)"
echo "rootless: $(podman info 2>/dev/null | grep -i rootless | head -1)"
echo "disk   : $(df -h / | tail -1)"
echo "memory : $(free -h 2>/dev/null | awk '/Mem:/{print $2" total, "$7" available"}')"

echo
echo "--- containers ---"
podman ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null

echo
echo "--- images ---"
podman images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' 2>/dev/null | head -10

echo
echo "--- volumes ---"
podman volume ls 2>/dev/null

echo
echo "--- listening ports ---"
ss -tlnp 2>/dev/null | head -15

echo
echo "--- firewall ---"
firewall-cmd --list-all 2>/dev/null | head -20

for C in modus-app modus-db; do
  podman container exists "$C" 2>/dev/null || continue
  echo
  echo "--- $C : environment (passwords redacted) ---"
  podman inspect "$C" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | sed -E 's/(PASSWORD|SECRET|KEY)=.*/\1=***REDACTED***/I'
  echo
  echo "--- $C : last 40 log lines ---"
  podman logs --tail 40 "$C" 2>&1
  echo
  echo "--- $C : errors anywhere in the log ---"
  podman logs "$C" 2>&1 | grep -iE "error|exception|fatal|refused|denied|timeout" | tail -25
done

echo
echo "--- connectivity ---"
for t in "localhost:3000" "localhost:5432"; do
  h=${t%%:*}; p=${t##*:}
  timeout 5 bash -c "</dev/tcp/$h/$p" 2>/dev/null && echo "  $t reachable" || echo "  $t NOT reachable"
done
if podman container exists modus-app 2>/dev/null; then
  DBH=$(podman inspect modus-app --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | awk -F= '/^DB_HOST=/{print $2}')
  [ -n "${DBH:-}" ] && { timeout 5 bash -c "</dev/tcp/$DBH/5432" 2>/dev/null \
      && echo "  app -> db ($DBH:5432) reachable" || echo "  app -> db ($DBH:5432) NOT reachable"; }
fi

echo
echo "--- http checks ---"
for u in "http://localhost:3000/" "http://localhost:3000/docs"; do
  echo "  $u -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$u" 2>/dev/null || echo unreachable)"
done

echo
echo "--- database ---"
if podman container exists modus-db 2>/dev/null; then
  podman exec modus-db psql -U modus -d surveillance -tAc \
    "SELECT 'tables='||count(*) FROM information_schema.tables WHERE table_schema='surveillance_sebi'" 2>&1 | head -2
  podman exec modus-db psql -U modus -d surveillance -tAc \
    "SELECT 'companies='||count(*) FROM surveillance_sebi.companies" 2>&1 | head -2
  podman exec modus-db psql -U modus -d surveillance -tAc \
    "SELECT 'users='||count(*) FROM public.users WHERE hashed_password IS NOT NULL" 2>&1 | head -2
fi
echo
echo "=========== END ==========="
} > "$OUT" 2>&1

# ---- short summary on screen, for when you can only retype a few lines ----
echo
echo "================ SUMMARY ================"
printf "  containers : "; podman ps -a --format "{{.Names}} [{{.Status}}]" 2>/dev/null | paste -sd'  ' -; echo
printf "  UI         : "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 http://localhost:3000/ 2>/dev/null || echo unreachable
printf "  API        : "; curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 http://localhost:3000/docs 2>/dev/null || echo unreachable
if podman container exists modus-db 2>/dev/null; then
  printf "  companies  : "; podman exec modus-db psql -U modus -d surveillance -tAc \
    "SELECT count(*) FROM surveillance_sebi.companies" 2>/dev/null | tr -d ' '
fi
echo "  top errors :"
for C in modus-app modus-db; do
  podman container exists "$C" 2>/dev/null || continue
  podman logs "$C" 2>&1 | grep -iE "error|exception|fatal|refused|denied" | tail -3 | sed "s/^/    [$C] /" | cut -c1-150
done
echo
echo "  FULL REPORT: $OUT"
echo "  ($(wc -l < "$OUT") lines — send this file)"
echo "========================================="
