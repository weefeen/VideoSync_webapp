#!/usr/bin/env bash
#
# Put one secret into the server's .env without it passing through a chat,
# a shell history, a process list or a log.
#
#   sudo bash deploy/set-secret.sh FACEBOOK_PAGE_TOKEN
#   (paste the value, press Enter; nothing is echoed)
#
# The value is read from the terminal with echo off, so it does not appear
# on screen, in `history`, or in `ps`. The file is backed up first, the old
# line for that name is dropped, the new one appended, permissions kept at
# 0600, and the web process restarted so it reads the change.
set -euo pipefail

NAME="${1:?usage: set-secret.sh VARIABLE_NAME   (value is read from the terminal)}"
ENV_FILE="${VSW_ENV:-/srv/vsw/shared/.env}"

case "$NAME" in
    *[!A-Z0-9_]*) echo "refusing: '$NAME' is not a plain VARIABLE_NAME" >&2; exit 2;;
esac

read -r -s -p "value for $NAME (hidden): " VALUE
echo
if [ -z "$VALUE" ]; then
    echo "empty value; nothing changed" >&2
    exit 1
fi

cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"
grep -v "^${NAME}=" "$ENV_FILE" > "$ENV_FILE.tmp" || true
printf '%s=%s\n' "$NAME" "$VALUE" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chown vsw:vsw "$ENV_FILE"
chmod 600 "$ENV_FILE"
unset VALUE

systemctl restart vsw-web
echo "$NAME set (${#NAME} chars of name, value not shown); vsw-web restarted"
