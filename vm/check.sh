#!/usr/bin/env bash
# ===========================================================================
#  Is it working?   sudo bash check.sh
#
#  Four lines, one verdict. Run it on either VM — it works out which one it is
#  from what is installed.
#
#  Always run with sudo. Containers started by the deploy scripts belong to
#  root, so a plain `podman ps` shows an empty list and looks like a failure.
#  This script says so explicitly rather than letting you guess.
# ===========================================================================
set -uo pipefail

PORT="${PORT:-8091}"
ENGINE=$(command -v podman || command -v docker) || { echo "neither podman nor docker found"; exit 1; }
E=$(basename "$ENGINE")

if [ "$(id -u)" -ne 0 ]; then
  echo
  echo "  !! Not running as root."
  echo "     The containers belong to root, so this will probably report nothing."
  echo "     Re-run as:  sudo bash $(basename "$0")"
  echo
fi

echo
echo "=============== MODUS CHECK ==============="

# --- what is running
printf "  containers : "
RUNNING=$($E ps --format '{{.Names}}' 2>/dev/null | paste -sd' ' -)
echo "${RUNNING:-NONE RUNNING}"
STOPPED=$($E ps -a --filter status=exited --format '{{.Names}} ({{.Status}})' 2>/dev/null | paste -sd', ' -)
[ -n "$STOPPED" ] && echo "  stopped    : $STOPPED"

# --- database, if this is the DB VM
if $E container exists modus-db 2>/dev/null || $E ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^modus-db$'; then
  N=$($E exec modus-db psql -U modus -d surveillance -tAc \
      "SELECT count(*) FROM surveillance_sebi.companies" 2>/dev/null | tr -d ' ')
  if [ -n "${N:-}" ] && [ "${N:-0}" -gt 0 ]; then
    echo "  database   : OK — $N companies"
  else
    echo "  database   : NOT ANSWERING"
    echo "               still loading on first start? check with:"
    echo "               sudo $E logs --tail 20 modus-db"
  fi
fi

# --- application, if this is the app VM
if $E ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^modus-app$'; then
  MAP=$($E port modus-app 2>/dev/null | paste -sd' ' -)
  echo "  published  : ${MAP:-NOTHING — nothing can reach it, usually a port clash}"
  UI=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://localhost:${PORT}/" 2>/dev/null)
  API=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://localhost:${PORT}/docs" 2>/dev/null)
  DATA=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "http://localhost:${PORT}/api/companies" 2>/dev/null)
  echo "  UI / API   : $UI / $API      (want 200 / 200)"
  echo "  data       : $DATA           (401 is CORRECT — it means auth is on)"
fi

# --- poller, if installed
if $E ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^modus-poller$'; then
  LAST=$($E logs --tail 3 modus-poller 2>&1 | tail -1 | cut -c1-90)
  echo "  poller     : ${LAST:-no output yet}"
fi

echo "==========================================="
echo
echo "  Everything above green? You are deployed."
echo "  Something red? Run:  sudo bash collect.sh   and send the summary."
echo
