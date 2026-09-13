#!/usr/bin/env bash
#
# Update the alignment engine (VideoScoreSync) on this server.
#
#     ssh root@<web box> 'bash /srv/vsw/current/deploy/update-vss.sh'
#
# WHEN YOU DECIDE, not on a schedule. Nothing on this box pulls by itself —
# there is no cron entry and no timer for it, and the deploy key is
# read-only. The server stays on whatever commit it has until this is run.
#
# WHY IT IS NOT ENOUGH TO PULL. The alignment does not run here. `app/sync.py`
# hands `--vss-root` to a child process and that child runs on a COMPUTE
# NODE, so the VideoScoreSync a node carries is the one that decides where
# every bar lands. A node gets it by rsyncing this directory from this box
# at boot (see app/compute/cloudinit.py), which is why updating here is the
# whole job — for nodes created afterwards.
#
# A node ALREADY RUNNING keeps the copy it booted with, deliberately: you do
# not want the engine swapped underneath a render that is half finished. The
# next node to be created picks this up.
#
# This repository is READ-ONLY for us. This script only ever pulls.

set -euo pipefail

ROOT="${VSS_ROOT:-/srv/vsw/VideoScoreSync}"

say() { printf '\n=== %s ===\n' "$1"; }

[ -d "$ROOT/.git" ] || {
    echo "$ROOT is not a git checkout, so there is nothing to pull." >&2
    echo "It was copied here by hand; replace it the same way." >&2
    exit 1
}

say "before"
git -C "$ROOT" log --oneline -1 | sed 's/^/  /'
echo "  dated $(git -C "$ROOT" log -1 --format=%ci)"
before=$(git -C "$ROOT" rev-parse HEAD)

say "pulling"
# A failure here is almost always the deploy key: GitHub answers
# "Permission denied (publickey)" when the key has been removed from the
# repository's Deploy keys. That is a click in GitHub, not a problem on
# this box, so the message says so rather than printing a bare git error.
if ! git -C "$ROOT" pull --ff-only 2>&1 | sed 's/^/  /'; then
    cat >&2 <<'WHY'

  The pull failed. If it said "Permission denied (publickey)", the deploy
  key is no longer on the repository. It is /root/.ssh/id_vss, named in
  /root/.ssh/config under `Host github-vss`. Add its PUBLIC half
  (id_vss.pub) to

      github.com/weefeen/VideoScoreSync -> Settings -> Deploy keys -> Add

  Leave "Allow write access" UNTICKED: this box only ever reads.
WHY
    exit 1
fi

after=$(git -C "$ROOT" rev-parse HEAD)

say "after"
git -C "$ROOT" log --oneline -1 | sed 's/^/  /'
if [ "$before" = "$after" ]; then
    echo "  already up to date — nothing changed"
    exit 0
fi
echo
echo "  what changed:"
git -C "$ROOT" diff --stat "$before" "$after" | tail -12 | sed 's/^/    /'

# The one thing that makes the engine unusable, checked the way the app
# checks it (settings.why_cannot_sync looks for services/). Better to find
# out now than when a visitor is waiting for a render.
say "is it still usable"
if [ -d "$ROOT/services" ]; then
    echo "  services/ is present — the app will accept this engine"
else
    echo "  services/ IS MISSING — app/settings.py refuses an engine without" >&2
    echo "  it, so alignment would be switched off. Check what was pulled." >&2
    exit 1
fi

say "what happens next"
cat <<'NEXT'
  Nothing restarts here: this box does not align, it only hands the engine
  to the machines that do.

  A compute node created from now on rsyncs this directory at boot and will
  use the version you just pulled. A node running right now keeps the copy
  it booted with until it is destroyed, which is deliberate — the engine is
  not swapped underneath a render in progress.
NEXT
