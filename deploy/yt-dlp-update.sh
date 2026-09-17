#!/usr/bin/env bash
#
# Keep yt-dlp current. Run daily by vsw-ytdlp-update.timer.
#
# WHY THIS EXISTS AND WHY IT IS DAILY. yt-dlp is not a normal dependency.
# YouTube changes how a stream is addressed every few weeks, and when it
# does, the installed yt-dlp stops working -- not loudly, but by failing
# every download with a message about formats. A pinned version is
# therefore a guaranteed outage on an unknown date. The project ships
# releases constantly for exactly this reason, so the safe state is the
# newest one, checked often enough that the gap is hours rather than weeks.
#
# It updates IN THE APPLICATION'S VIRTUALENV, not system-wide: that is the
# interpreter app/fetchaudio.py runs `-m yt_dlp` with, and a system copy
# would be updated while the one actually used stayed stale.
#
# NOTHING IS RESTARTED. fetchaudio spawns yt-dlp per download, so the next
# fetch picks up the new version on its own. Restarting gunicorn to change
# a subprocess would drop live requests for no reason.
#
#   bash deploy/yt-dlp-update.sh            # check, update if behind
#   bash deploy/yt-dlp-update.sh --check    # say only, change nothing
set -uo pipefail

VENV="${VSW_VENV:-/srv/vsw/venv}"
PY="$VENV/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
dry=""
[ "${1:-}" = "--check" ] && dry=1

say() { echo "[yt-dlp-update] $*"; }

before="$("$PY" -m yt_dlp --version 2>/dev/null | tr -d '[:space:]')"
if [ -z "$before" ]; then
  say "yt-dlp is not installed in $VENV -- installing it"
  before="(absent)"
fi

# --dry-run still contacts PyPI and reports what it WOULD do, which is the
# whole of the check. Grepping its output is how we learn there is a newer
# one without a second dependency to ask the index directly.
plan="$("$PY" -m pip install --upgrade --dry-run yt-dlp 2>&1)"
if echo "$plan" | grep -qi "Would install yt-dlp"; then
  target="$(echo "$plan" | grep -oiE 'Would install yt-dlp-[0-9.]+' | head -1 | sed 's/.*yt-dlp-//')"
  say "installed $before, available ${target:-newer}"
  if [ -n "$dry" ]; then
    say "--check: stopping here"
    exit 0
  fi
else
  say "already current at $before"
  exit 0
fi

if ! "$PY" -m pip install --upgrade --quiet yt-dlp; then
  # A failed update is NOT a failed deploy: the previous version is still
  # installed and still works until YouTube changes again. Loud, non-zero,
  # and the timer will try again tomorrow.
  say "the update FAILED; staying on $before"
  exit 1
fi

after="$("$PY" -m yt_dlp --version 2>/dev/null | tr -d '[:space:]')"
say "updated $before -> ${after:-unknown}"

# Proof it can still start. A package that installed but cannot run is the
# one failure mode an update introduces, and it is worth ten seconds to
# catch here rather than on the next visitor's link.
if [ -z "$after" ]; then
  say "WARNING: the new yt-dlp will not report a version"
  exit 1
fi
exit 0
