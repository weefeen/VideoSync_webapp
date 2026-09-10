#!/usr/bin/env bash
#
# The geolocation database, so visitor addresses can be resolved to a country
# and a city WITHOUT asking anybody.
#
#   deploy/fetch-geoip.sh                 city-level, ~60 MB download
#   deploy/fetch-geoip.sh country         country only, ~4 MB
#
# Safe to run more than once; run it monthly, because addresses move.
#
# WHY A FILE AND NOT AN API. The alternative is sending every visitor's
# address to a geolocation service, which hands a third party a list of who
# used this site and when in exchange for two columns. A file on this disk
# costs a download a month and discloses nothing.
#
# WHICH DATABASE. DB-IP's Lite editions: free, no account, no licence key, and
# no rate limit — which matters because it means this script needs no secret
# and can run from anywhere. MaxMind's GeoLite2 is the better-known option and
# is also free, but requires an account and a key; the reader below takes
# either, so swapping is a matter of pointing GEOIP_DB at the other file.
#
# ATTRIBUTION. DB-IP Lite is CC-BY-4.0. The site does not display this data —
# it is operational, seen only by the operator — so no public credit is due,
# but if a figure derived from it is ever published, credit db-ip.com.

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

EDITION="${1:-city}"
case "$EDITION" in
    city|country) ;;
    *) echo "usage: $0 [city|country]" >&2; exit 1 ;;
esac

# Where the app looks. Matches WORK_DIR in the unit files; override for a
# different layout.
DEST="${GEOIP_DIR:-/srv/vsw/shared/geoip}"
MONTH="$(date -u +%Y-%m)"
NAME="dbip-${EDITION}-lite-${MONTH}.mmdb"
URL="https://download.db-ip.com/free/${NAME}.gz"

say "Fetching $NAME"
install -d "$DEST"
TMP="$(mktemp "${DEST}/.fetch.XXXXXX")"
trap 'rm -f "$TMP" "$TMP.gz"' EXIT

# The file is published on the first of the month. Early on the 1st, in some
# timezones, this month's is not up yet — so fall back to last month's rather
# than failing, because a database four weeks old is worth far more than none.
if ! curl -fSL --progress-bar -o "$TMP.gz" "$URL"; then
    MONTH="$(date -u -d 'last month' +%Y-%m 2>/dev/null || date -u -v-1m +%Y-%m)"
    NAME="dbip-${EDITION}-lite-${MONTH}.mmdb"
    URL="https://download.db-ip.com/free/${NAME}.gz"
    echo "  this month is not published yet; taking ${MONTH}"
    curl -fSL --progress-bar -o "$TMP.gz" "$URL"
fi

gunzip -c "$TMP.gz" > "$TMP"

# Readable by the app, which does not run as root. `mktemp` makes the file
# 0600 and `mv` keeps that, so without this the download succeeds, the file
# is visibly there, and every lookup fails with a permission error that looks
# exactly like having no visitors.
chmod 0644 "$TMP"

# Swapped into place in one step. The app memory-maps this file and reads it
# on every visitor row, so writing over it in place would have a live process
# reading a half-written database.
mv -f "$TMP" "$DEST/$NAME"
ln -sfn "$DEST/$NAME" "$DEST/current.mmdb"
trap - EXIT
rm -f "$TMP.gz"

# Keep one previous month, discard the rest. Each is a few hundred megabytes
# and the disk is shared with the videos.
ls -1t "$DEST"/dbip-*.mmdb 2>/dev/null | tail -n +3 | xargs -r rm -f

say "In place"
chmod 0755 "$DEST"
ls -lh "$DEST"/dbip-*.mmdb | sed 's|^|  |'
echo
echo "  Point the app at it:"
echo "      GEOIP_DB=$DEST/current.mmdb"
echo
echo "  Then check it reads:"
echo "      python tools/visitors.py --where"
