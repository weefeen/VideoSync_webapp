#!/usr/bin/env bash
#
# Deploy the committed `main` to the web box, from this machine.
#
# THE RECIPE WAS PROSE IN deploy/README.md AND WAS TYPED OUT EACH TIME.
# Five commands over SSH, one of which names the release directory that
# three later ones must agree on: a deploy that half-runs leaves a release
# tree owned by root that the next attempt cannot write. This is the same
# five commands, written once, so a deploy is a thing that is RUN and not
# a thing that is composed.
#
# It deliberately deploys what is on GitHub, never this working tree:
# files here carry CRLF, and a shell script with CRLF dies on Linux with
# `$'\r': command not found`. The committed blobs are LF.
#
#   bash deploy/release.sh              # deploy HEAD of main
#   bash deploy/release.sh --check      # say what would happen, do nothing
#
set -euo pipefail

HOST="${VSW_HOST:-root@172.104.237.127}"
REPO="${VSW_REPO:-https://github.com/weefeen/VideoSync_webapp.git}"
SITE="${VSW_SITE:-https://chopin.weefeen.com}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 "$HOST")

cd "$(dirname "$0")/.."
dry=""
[ "${1:-}" = "--check" ] && dry=1

# --- the release must already be on the branch the server clones --------
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "main" ] || { echo "on '$branch', not main"; exit 1; }
git diff --quiet && git diff --cached --quiet \
  || { echo "the working tree is dirty; commit or stash first"; exit 1; }
git fetch -q origin main
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse origin/main)"
[ "$local_head" = "$remote_head" ] \
  || { echo "HEAD is not what origin/main holds; push first"; exit 1; }

short="$(git rev-parse --short HEAD)"
REL="main-$short-$(date -u +%Y%m%d%H%M%S)"
echo "deploying $short to $HOST as $REL"
[ -n "$dry" ] && { echo "  --check: stopping here"; exit 0; }

# --- clone, hand it to vsw, let deploy.sh do the swap -------------------
# ONE remote command: a connection that drops between two of them leaves
# the release half-built, and the next run then fails on a directory it
# cannot write.
"${SSH[@]}" "
set -e
rm -rf /srv/vsw/releases/$REL
git clone -q --depth 1 --branch main $REPO /srv/vsw/releases/$REL
rm -rf /srv/vsw/releases/$REL/.git /srv/vsw/releases/$REL/.github
chown -R vsw:vsw /srv/vsw/releases/$REL
bash /srv/vsw/releases/$REL/deploy/deploy.sh $REL"

# --- and it is actually serving the code we just sent -------------------
# deploy.sh health-checks and rolls back on its own, so this is the second
# opinion: asked from OUTSIDE, through Apache, the way a visitor asks.
echo
live="$("${SSH[@]}" 'readlink -f /srv/vsw/current' 2>/dev/null || true)"
echo "  current -> ${live:-unknown}"
case "$live" in
  *"$REL") echo "  the new release is live" ;;
  *) echo "  WARNING: current is not $REL -- deploy.sh rolled back"; exit 1 ;;
esac
# `/api/library` and not `/api/health`, because there is no `/api/health`
# -- deploy.sh has always health-checked the library, and a check against
# a path that does not exist reports every good deploy as a failure.
#
# By EXIT CODE, not `-w '%{http_code}'`: the curl in Git Bash on Windows
# exits 43 on that format string and prints `000`, so every good deploy
# ended by announcing the site was down. `-f` already makes any status
# outside 2xx a non-zero exit, which is the whole question being asked.
if curl -fsS -o /dev/null --max-time 25 "$SITE/api/library"; then
    echo "  $SITE/api/library answers"
else
    echo "  the site is not answering"; exit 1
fi
