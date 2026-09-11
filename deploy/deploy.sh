#!/usr/bin/env bash
#
# Put a new release live, and put the old one back if it does not come up.
#
#     deploy.sh <release-directory-name>
#
# This runs ON THE SERVER. The workflow rsyncs a release directory here and
# then calls it. It is written to be safe on a SHARED box: production will
# have weefeen's Symfony app, its PHP RabbitMQ consumers and the broker
# living beside it, and nothing here may touch any of them.
#
# What it deliberately does NOT do, because the workflow it is adapted from
# does and it would break the neighbour:
#   * no `docker system prune` — that deletes every volume on the host
#   * no `docker compose down -v` — that deletes named volumes
#   * no killing whatever holds port 5672 — that is their broker
#   * no `supervisorctl restart all` — only the units named below are
#     touched, and they are named rather than matched by pattern
#
# Layout, the same shape Capistrano gives the Symfony app next door, so an
# operator reading both sees one pattern:
#
#     $ROOT/releases/<timestamp>/    the code
#     $ROOT/current -> releases/...  the symlink the units follow
#     $ROOT/shared/.env              secrets, never in the repo
#     $ROOT/shared/var/              jobs.sqlite, uploads, output
#     $ROOT/venv                     ONE environment, shared — see below
#
# $ROOT is /srv/vsw on every host, including production, where the storage
# actually lives on a mounted volume: there `/srv/vsw` is a SYMLINK to
# `/mnt/volume_1/vsw`. One path everywhere means one set of systemd units
# rather than two that drift apart.
#
# Rolling back is repointing `current` and restarting; previous releases
# are kept for exactly that reason.

set -euo pipefail

ROOT="${VSW_ROOT:-/srv/vsw}"
UNITS="${VSW_UNITS:-vsw-worker vsw-web}"
HEALTH_URL="${VSW_HEALTH_URL:-http://127.0.0.1:5000/api/library}"
KEEP="${VSW_KEEP_RELEASES:-5}"
RELEASE="${1:?usage: deploy.sh <release-directory-name>}"

NEW="$ROOT/releases/$RELEASE"
PREVIOUS="$(readlink -f "$ROOT/current" 2>/dev/null || true)"

say() { printf '\n=== %s\n' "$*"; }

[ -d "$NEW" ] || { echo "no such release: $NEW" >&2; exit 1; }

say "Linking shared state into $RELEASE"
# The database, the uploads and the finished videos outlive any one
# release, and .env is never in the repository.
mkdir -p "$ROOT/shared/var"
ln -sfn "$ROOT/shared/var"  "$NEW/var"
ln -sfn "$ROOT/shared/.env" "$NEW/.env"

say "Dependencies"
# ONE environment shared by every release, rather than one per release as
# the script this was adapted from does. Two reasons, both specific to this
# app: it is 1.5 GB because of torch, so keeping five releases would mean
# 7.5 GB of near-identical copies; and its heavy half is dictated by two
# READ-ONLY engine repositories, so it does not change when this app's code
# changes. What a rollback has to undo is the code.
#
# BOTH requirement files. requirements.txt alone serves pages and renders a
# video from an alignment that already exists — it cannot recognise or
# align, and that omission shows up on the first upload rather than here.
[ -x "$ROOT/venv/bin/python" ] || python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --quiet --upgrade pip
"$ROOT/venv/bin/pip" install --quiet \
    -r "$NEW/requirements.txt" -r "$NEW/requirements-engine.txt"

say "Checking the release can actually start"
# Better to fail here, with `current` still pointing at the old release,
# than to swap the symlink and find out afterwards.
#
# This imports the ENGINES as well as the app, and that is the point.
# Checking `create_app()` alone is exactly what would let a missing torch
# look healthy: the web process starts perfectly well without it, and only
# a real upload finds out.
( cd "$NEW" && "$ROOT/venv/bin/python" - <<'PYCHECK'
import importlib, sys
sys.path.insert(0, ".")

broken = []
for module in ("torch", "librosa", "numba", "soundfile",
               "piano_transcription_inference", "cairosvg", "pika"):
    try:
        importlib.import_module(module)
    except Exception as exc:                       # noqa: BLE001
        broken.append(f"  {module}: {type(exc).__name__}: {exc}")
if broken:
    print("THE ENGINES ARE NOT USABLE IN THIS RELEASE:")
    print("\n".join(broken))
    sys.exit(1)

from app.routes import create_app
from app.settings import settings
create_app()
problems = settings.problems()
for problem in problems:
    print("WARNING:", problem)
print(f"app builds, engines import; {len(problems)} configuration warning(s)")
PYCHECK
)

say "Switching current -> $RELEASE"
ln -sfn "$NEW" "$ROOT/current"

say "Restarting our units only ($UNITS)"
# Named explicitly, never matched by pattern: this box belongs to somebody
# else as well. Needs a narrow sudoers rule for the deploy user; see
# deploy/README.md.
#
# `KillMode=control-group`, systemd's default, is what makes a restart take
# a running render's ffmpeg with it. Without a supervisor that ffmpeg
# survives and holds a core — measured, in docs/deployment-log.md §15.
# shellcheck disable=SC2086
sudo systemctl restart $UNITS

say "Health check"
ok=""
for _ in $(seq 1 30); do
    if curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; then
        ok="yes"; break
    fi
    sleep 2
done

if [ -z "$ok" ]; then
    echo "!!! did not become healthy" >&2
    if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ] && [ "$PREVIOUS" != "$NEW" ]; then
        say "Rolling back to $(basename "$PREVIOUS")"
        ln -sfn "$PREVIOUS" "$ROOT/current"
        # shellcheck disable=SC2086
        sudo systemctl restart $UNITS
        echo "rolled back" >&2
    fi
    exit 1
fi

say "Healthy. Pruning old releases, keeping $KEEP"
# PAST THE POINT OF NO FAILURE. The release is live and answering; from here
# on this script is tidying up, and tidying up must not be able to report a
# working deploy as a broken one.
#
# It did exactly that once. Old releases had `__pycache__` directories owned
# by root, written when the app was run by hand as root during an earlier
# deploy. The prune runs as the deploy user, which cannot unlink a file
# inside a directory it does not own, so `rm -rf` returned "Permission
# denied", `set -e` fired, and the workflow went red — minutes after the new
# release had gone live and passed its health check. The site was fine and
# the deploy was reported as failed, which is the worst way round.
#
# So: every failure here is reported and none is fatal.
cd "$ROOT/releases"
# Never remove the one `current` points at, whatever the ordering says.
ls -1dt */ 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    old="${old%/}"
    [ "$ROOT/releases/$old" = "$(readlink -f "$ROOT/current")" ] && continue
    if rm -rf -- "$ROOT/releases/$old" 2>/dev/null; then
        echo "removed $old"
    else
        echo "could NOT remove $old — left in place. Usually a file owned by"
        echo "  another user; 'sudo chown -R \$(id -un):\$(id -gn) $ROOT/releases'"
        echo "  clears it. This has not affected the deploy."
    fi
done || true

say "Deployed $RELEASE"
