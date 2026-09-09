#!/usr/bin/env bash
#
# Put a new release live on the always-on host, and put the old one back if
# it does not come up.
#
# This runs ON THE SERVER. The workflow rsyncs a release directory here and
# then calls it. It is written to be safe on a SHARED box: chopin runs beside
# weefeen's Symfony app, its PHP RabbitMQ consumers and its broker, and
# nothing here may touch any of them.
#
# What it deliberately does NOT do, because the workflow it is adapted from
# does and it would break the neighbour:
#   * no `docker system prune` — that deletes every volume on the host
#   * no `docker compose down -v` — that deletes named volumes
#   * no killing whatever holds port 5672 — that is their broker
#   * no `supervisorctl restart all` — that restarts their consumers too;
#     only the programs in our own group are touched
#
# Layout, the same shape Capistrano gives the Symfony app next door, so an
# operator reading both sees one pattern:
#
#     /mnt/volume_1/vsw/releases/<timestamp>/    the code
#     /mnt/volume_1/vsw/current -> releases/...  the symlink gunicorn follows
#     /mnt/volume_1/vsw/shared/.env              secrets, never in the repo
#     /mnt/volume_1/vsw/shared/var/              jobs.sqlite, uploads, output
#
# Rolling back is repointing `current` and restarting; the previous releases
# are kept for exactly that reason.

set -euo pipefail

ROOT="${VSW_ROOT:-/mnt/volume_1/vsw}"
GROUP="${VSW_SUPERVISOR_GROUP:-vsw}"
HEALTH_URL="${VSW_HEALTH_URL:-http://127.0.0.1:5057/api/library}"
KEEP="${VSW_KEEP_RELEASES:-5}"
RELEASE="${1:?usage: deploy.sh <release-directory-name>}"

NEW="$ROOT/releases/$RELEASE"
PREVIOUS="$(readlink -f "$ROOT/current" 2>/dev/null || true)"

say() { printf '\n=== %s\n' "$*"; }

[ -d "$NEW" ] || { echo "no such release: $NEW" >&2; exit 1; }

say "Linking shared state into $RELEASE"
# The database, the uploads and the finished videos outlive any one release,
# and .env is never in the repository.
mkdir -p "$ROOT/shared/var"
ln -sfn "$ROOT/shared/var"  "$NEW/var"
ln -sfn "$ROOT/shared/.env" "$NEW/.env"

say "Building the virtualenv"
# Its own venv per release, so a rollback also rolls back dependencies.
python3 -m venv "$NEW/.venv"
"$NEW/.venv/bin/pip" install --quiet --upgrade pip
"$NEW/.venv/bin/pip" install --quiet -r "$NEW/requirements.txt"

say "Checking the release can actually start"
# Better to fail here, with `current` still pointing at the old release,
# than to swap the symlink and discover the import error afterwards.
( cd "$NEW" && "$NEW/.venv/bin/python" -c "
import sys; sys.path.insert(0, '.')
from app.routes import create_app
from app.settings import settings
app = create_app()
problems = settings.problems()
for p in problems:
    print('WARNING:', p)
print('app builds;', len(problems), 'configuration warning(s)')
" )

say "Switching current -> $RELEASE"
ln -sfn "$NEW" "$ROOT/current"

say "Restarting our supervisor programs (group '$GROUP' only)"
supervisorctl reread >/dev/null
supervisorctl update "$GROUP" >/dev/null || true
supervisorctl restart "$GROUP:*"

say "Health check"
ok=""
for attempt in $(seq 1 30); do
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
        supervisorctl restart "$GROUP:*"
        echo "rolled back" >&2
    fi
    exit 1
fi

say "Healthy. Pruning old releases, keeping $KEEP"
cd "$ROOT/releases"
# Never remove the one `current` points at, whatever the ordering says.
ls -1dt */ 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
    old="${old%/}"
    [ "$ROOT/releases/$old" = "$(readlink -f "$ROOT/current")" ] && continue
    rm -rf -- "$ROOT/releases/$old"
    echo "removed $old"
done

say "Deployed $RELEASE"
