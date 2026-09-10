#!/usr/bin/env bash
#
# Make the alerts reach a person.
#
#   sudo deploy/install-alert-mail.sh
#
# Safe to run more than once.
#
# WHERE THE CREDENTIALS COME FROM. /srv/vsw/shared/.env, which is not in the
# repository and never will be. The app already has SMTP_* there for the
# "your video is ready" mail, so an alert that emails somebody needs no
# second set of credentials and no second place to rotate them. This script
# copies them into grafana.ini; nothing secret passes through git.
#
# WHY GRAFANA NEEDS ITS OWN COPY. Grafana is a separate process that has
# never heard of our .env, and it sends its own mail. The alternative was a
# webhook back into the app so the app sends it — one more endpoint, on the
# box whose health is in question, which is exactly the wrong place for the
# thing that tells you the box is unhealthy.
#
# grafana.ini is 0640 root:grafana, so the password is readable by Grafana
# and by root and nobody else. Checked below rather than assumed.

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

ENVFILE="${VSW_ENV:-/srv/vsw/shared/.env}"
INI=/etc/grafana/grafana.ini
PROV=/etc/grafana/provisioning/alerting/contact-points.yaml

[ -r "$ENVFILE" ] || { echo "no $ENVFILE to read" >&2; exit 1; }

# Read one value without sourcing the file: it is configuration, not script,
# and sourcing it would execute whatever a stray backtick happened to be.
get() {
    sed -n "s/^$1=//p" "$ENVFILE" | tail -1 | sed 's/^"//; s/"$//'
}

SMTP_HOST="$(get SMTP_HOST)"
SMTP_PORT="$(get SMTP_PORT)"
SMTP_USER="$(get SMTP_USER)"
SMTP_PASSWORD="$(get SMTP_PASSWORD)"
SMTP_FROM="$(get SMTP_FROM)"
SMTP_STARTTLS="$(get SMTP_STARTTLS)"
SMTP_SSL="$(get SMTP_SSL)"
ALERT_EMAIL="$(get ALERT_EMAIL)"

missing=""
[ -n "$SMTP_HOST" ]   || missing="$missing SMTP_HOST"
[ -n "$ALERT_EMAIL" ] || missing="$missing ALERT_EMAIL"
if [ -n "$missing" ]; then
    cat >&2 <<NOTE

  Nothing to do yet:$missing is not set in $ENVFILE.

  Add these lines to it and run this again. SMTP_HOST is the only one the
  app also needs — filling it in makes the "your video is ready" mail work
  as well, which does not work today either.

      SMTP_HOST=smtp.example.com
      SMTP_PORT=587
      SMTP_USER=info@weefeen.com
      SMTP_PASSWORD=...
      SMTP_FROM=Weefeen <info@weefeen.com>
      SMTP_STARTTLS=1

      # Where an alert goes. Not a visitor-facing address.
      ALERT_EMAIL=you@example.com

NOTE
    exit 1
fi

# Grafana wants host:port in one field.
HOSTPORT="$SMTP_HOST"
case "$SMTP_HOST" in
    *:*) ;;
    *)   HOSTPORT="$SMTP_HOST:${SMTP_PORT:-587}" ;;
esac

# A password containing # or ; ends the value early in an ini file and the
# rest is read as a comment — Grafana's own documentation says to wrap those
# in triple quotes. Done conditionally, because wrapping one that does not
# need it is a different way to get the wrong password.
QUOTED="$SMTP_PASSWORD"
case "$SMTP_PASSWORD" in
    *"#"*|*";"*) QUOTED='"""'"$SMTP_PASSWORD"'"""' ;;
esac

say "SMTP into grafana.ini"
# Appended as a fresh section rather than editing the 100 KB packaged file
# in place: for duplicate keys the last wins, and the packaged [smtp] is
# entirely commented out anyway. The marker keeps this idempotent — without
# it, every run would append another section and the file would grow forever.
if grep -q '^; managed by install-alert-mail.sh' "$INI"; then
    # Drop the previous block, everything from the marker to end of file.
    sed -i '/^; managed by install-alert-mail.sh/,$d' "$INI"
    echo "  replacing the previous block"
fi

{
    echo '; managed by install-alert-mail.sh — edit .env, not this'
    echo '[smtp]'
    echo 'enabled = true'
    echo "host = $HOSTPORT"
    [ -n "$SMTP_USER" ] && echo "user = $SMTP_USER"
    [ -n "$SMTP_PASSWORD" ] && echo "password = $QUOTED"
    [ -n "$SMTP_FROM" ] && echo "from_address = ${SMTP_FROM##*<}" | tr -d '>'
    echo 'from_name = VideoSync'
    case "$SMTP_SSL" in
        1|true|yes|True) echo 'skip_verify = false' ;;
    esac
} >> "$INI"

chown root:grafana "$INI"
chmod 0640 "$INI"
printf '  %s\n' "$(ls -l "$INI" | awk '{print $1, $3, $4}')"

say "Where the alerts go"
install -d /etc/grafana/provisioning/alerting
cat > "$PROV" <<YAML
# Written by install-alert-mail.sh from .env. Not in the repository: it
# carries an address, and the repository is not where addresses live.
apiVersion: 1

contactPoints:
  - orgId: 1
    name: operator
    receivers:
      - uid: vsw-operator-email
        type: email
        settings:
          addresses: ${ALERT_EMAIL}
          singleEmail: true

policies:
  - orgId: 1
    receiver: operator
    # Group everything into one mail rather than one per rule: both memory
    # rules firing means one problem, not two.
    group_by: ['grafana_folder', 'alertname']
    group_wait: 30s
    group_interval: 5m
    # A firing memory alert that nobody has acted on is still true four
    # hours later, and worth saying again — but not every five minutes.
    repeat_interval: 4h
YAML
chmod 0644 "$PROV"

say "Restart, and check it came back"
systemctl restart grafana-server
for _ in $(seq 1 20); do
    code=$(curl -fsS -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:3000/api/health 2>/dev/null || echo 000)
    [ "$code" = "200" ] && break
    sleep 4
done
echo "  grafana: HTTP $code   restarts=$(systemctl show grafana-server -p NRestarts --value)"
if [ "$code" != "200" ]; then
    echo "  IT DID NOT COME BACK. The last provisioning error:" >&2
    journalctl -u grafana-server --since '2 min ago' --no-pager 2>/dev/null |
        grep -iE 'Error:' | grep -vi shutting | tail -2 >&2
    exit 1
fi

say "Send a test"
# Through Grafana's own API, so it exercises the same path an alert takes —
# the credentials, the relay and the from-address together. A configuration
# that loads cleanly and cannot send is the failure worth catching here.
if curl -fsS -u "${GRAFANA_ADMIN:-admin}:${GRAFANA_PASSWORD:-admin}" \
        -H 'Content-Type: application/json' \
        -d "{\"name\":\"test\",\"type\":\"email\",\"settings\":{\"addresses\":\"${ALERT_EMAIL}\"}}" \
        http://127.0.0.1:3000/api/alert-notifications/test 2>&1 | head -3; then
    echo
    echo "  Sent. If nothing arrives, the relay accepted it and dropped it —"
    echo "  check the sender is allowed to send as ${SMTP_FROM:-that address}."
else
    echo "  The test could not be sent. If this is an authentication error,"
    echo "  the admin password is not the default; pass it as:"
    echo "      GRAFANA_PASSWORD=... sudo -E deploy/install-alert-mail.sh"
fi

cat <<'NOTE'

  Both memory rules now route to the operator contact point. To see them:

      Alerting -> Alert rules      what is armed
      Alerting -> Contact points   where it goes

NOTE
