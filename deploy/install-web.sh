#!/usr/bin/env bash
#
# Put the site on the internet: Apache in front, TLS from Let's Encrypt.
#
#   sudo deploy/install-web.sh chopin.weefeen.com
#
# Safe to run more than once.
#
# THE ONE THING THIS MUST GET RIGHT. Four endpoints are loopback-only by
# design and a naive `ProxyPass /` publishes all of them:
#
#     /metrics          queue depth, failure counts, what the machine costs
#     /api/failures     ffmpeg command lines assembled from visitors' choices
#     /api/compute      what the scaler would do, and why
#     /api/visitors     VISITORS' IP ADDRESSES, with the city each resolves to
#
# The last one is the most personal thing this application holds. They are
# blocked here, before the proxy, and the blocks are verified at the end from
# the outside as well as from loopback — because "I wrote a Location block"
# and "the endpoint is unreachable" are different claims.
#
# ON A SHARED BOX. Production will have weefeen's Symfony app beside this.
# Nothing here disables another vhost, touches the default site, or edits
# anything outside our own conf files.

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

HOST="${1:-chopin.weefeen.com}"
APP="${VSW_APP:-127.0.0.1:5000}"
EMAIL="${CERT_EMAIL:-info@weefeen.com}"
ACME=/var/www/acme

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends apache2 certbot

a2enmod -q proxy proxy_http headers rewrite ssl remoteip
install -d "$ACME/.well-known/acme-challenge"
chown -R www-data:www-data "$ACME"

say "Port 80: the ACME challenge, and a redirect for everything else"
cat > /etc/apache2/sites-available/vsw-http.conf <<APACHE
<VirtualHost *:80>
    ServerName ${HOST}

    # The challenge must stay on plain HTTP: it is how the certificate is
    # renewed, and a redirect here breaks renewal three months from now,
    # silently, at which point the site stops working.
    Alias /.well-known/acme-challenge/ ${ACME}/.well-known/acme-challenge/
    <Directory "${ACME}/.well-known/acme-challenge">
        Require all granted
        Options None
    </Directory>

    RewriteEngine On
    RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/
    RewriteRule ^(.*)\$ https://${HOST}\$1 [R=301,L]

    ErrorLog \${APACHE_LOG_DIR}/vsw-error.log
    CustomLog \${APACHE_LOG_DIR}/vsw-access.log combined
</VirtualHost>
APACHE

a2ensite -q vsw-http
apache2ctl configtest
systemctl reload apache2

say "The certificate"
if [ -f "/etc/letsencrypt/live/${HOST}/fullchain.pem" ]; then
    echo "  already have one for ${HOST}"
else
    certbot certonly --webroot -w "$ACME" -d "$HOST" \
        --email "$EMAIL" --agree-tos --no-eff-email --non-interactive
fi

say "Port 443: the site, with the private endpoints shut"
cat > /etc/apache2/sites-available/vsw-https.conf <<APACHE
<VirtualHost *:443>
    ServerName ${HOST}

    SSLEngine on
    SSLCertificateFile      /etc/letsencrypt/live/${HOST}/fullchain.pem
    SSLCertificateKeyFile   /etc/letsencrypt/live/${HOST}/privkey.pem
    SSLProtocol             -all +TLSv1.2 +TLSv1.3
    SSLHonorCipherOrder     off

    # ── the four that never leave this machine ──────────────────────────
    # 404 rather than 403: a refusal confirms the endpoint exists, and
    # /api/visitors existing is itself worth knowing to somebody looking.
    # Matched before any ProxyPass, and ProxyPass ! keeps each one from
    # reaching the application at all.
    <LocationMatch "^/(metrics|api/(failures|compute|visitors))">
        Require all denied
        ErrorDocument 403 "Not found"
    </LocationMatch>
    ProxyPass /metrics !
    ProxyPass /api/failures !
    ProxyPass /api/compute !
    ProxyPass /api/visitors !

    # ── everything else goes to the app ─────────────────────────────────
    ProxyPreserveHost On
    ProxyPass        / http://${APP}/
    ProxyPassReverse / http://${APP}/

    # 4 GB at 20 Mbit/s is about 27 minutes. The default is 60 SECONDS, so
    # without this every large upload dies mid-transfer and the visitor is
    # told nothing useful. gunicorn's own --timeout has to match, which
    # this script also sets.
    ProxyTimeout 3600

    # Who the visitor actually is. Apache sets X-Forwarded-For and the app
    # reads it ONLY when TRUST_PROXY is on — which this script turns on,
    # because without it every visitor looks like 127.0.0.1 and they all
    # share one rate-limit bucket.
    RemoteIPHeader X-Forwarded-For
    RemoteIPInternalProxy 127.0.0.1

    # Defence in depth for the rate-limit key. mod_remoteip resolves the true
    # client into REMOTE_ADDR above; this OVERWRITES the forwarded header the
    # backend sees with that resolved address, so a client cannot seed the
    # list at all. The app keys on the LAST entry (see app/limits.client_key)
    # and would resist a forged leftmost value anyway — this makes the whole
    # header trustworthy rather than just the last element. Without it, a
    # client sending `X-Forwarded-For: 1.2.3.4` could pick which rate-limit
    # bucket it counted against; with it, it cannot. `mod_headers` is enabled
    # at the top of this script.
    RequestHeader set X-Forwarded-For "%{REMOTE_ADDR}s"

    Header always set Strict-Transport-Security "max-age=31536000"
    Header always set X-Content-Type-Options "nosniff"
    Header always set Referrer-Policy "strict-origin-when-cross-origin"
    # The page loads its typefaces from Google and nothing else from
    # anywhere; the privacy page says exactly that, so the policy should
    # say it too.
    Header always set X-Frame-Options "SAMEORIGIN"

    ErrorLog \${APACHE_LOG_DIR}/vsw-error.log
    CustomLog \${APACHE_LOG_DIR}/vsw-access.log combined
</VirtualHost>
APACHE

a2ensite -q vsw-https
apache2ctl configtest
systemctl reload apache2

say "Tell the app there is a proxy in front of it"
# These two changes belong together and must happen at the same moment. With
# a proxy and TRUST_PROXY off, remote_addr is 127.0.0.1 for EVERY visitor:
# one shared rate-limit bucket, and a visitor list with a single row in it.
ENVFILE=/srv/vsw/shared/.env
if grep -q '^TRUST_PROXY=' "$ENVFILE"; then
    sed -i 's|^TRUST_PROXY=.*|TRUST_PROXY=true|' "$ENVFILE"
else
    echo 'TRUST_PROXY=true' >> "$ENVFILE"
fi
echo "  TRUST_PROXY=true"

UNIT=/etc/systemd/system/vsw-web.service
if grep -q -- '--timeout 900' "$UNIT"; then
    sed -i 's|--timeout 900|--timeout 3600|' "$UNIT"
    systemctl daemon-reload
    echo "  gunicorn --timeout raised to 3600 to match ProxyTimeout"
fi
systemctl restart vsw-web
sleep 5

say "Renewal"
# certbot's packaged timer does the renewing; Apache has to be told to pick
# the new certificate up, or it serves the old one until something restarts
# it by chance — usually three months later, in the form of a browser
# warning.
install -d /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-apache <<'HOOK'
#!/bin/sh
systemctl reload apache2
HOOK
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-apache
systemctl is-enabled certbot.timer >/dev/null 2>&1 && echo "  certbot.timer is enabled" \
    || { systemctl enable --now certbot.timer; echo "  certbot.timer enabled"; }

say "Does it work, and is what should be shut actually shut?"
printf '  %-34s %s\n' "https://${HOST}/app/" \
    "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "https://${HOST}/app/" || echo failed)"
printf '  %-34s %s\n' "http:// redirects" \
    "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "http://${HOST}/app/" || echo failed)"

echo
echo "  these four must be 403/404 from outside and 200 on loopback:"
for path in /metrics /api/failures /api/compute /api/visitors; do
    out=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "https://${HOST}${path}" || echo failed)
    inside=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "http://${APP}${path}" || echo failed)
    verdict="ok"
    case "$out" in 200) verdict="EXPOSED — FIX THIS" ;; esac
    [ "$inside" = "200" ] || verdict="$verdict (but loopback gives $inside, so Prometheus is broken)"
    printf '    %-16s outside %-4s loopback %-4s %s\n' "$path" "$out" "$inside" "$verdict"
done

echo
echo "  nothing of ours should be listening publicly except 80 and 443:"
ss -ltn | awk '$4 !~ /127\.0\.0\.1|::1/ && NR > 1 {print "    " $4}'
