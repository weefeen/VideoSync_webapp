#!/usr/bin/env bash
#
# Prometheus and Grafana, on the always-on host.
#
#   sudo deploy/install-monitoring.sh
#
# Safe to run more than once.
#
# WHY HERE AND NOT ON THE COMPUTE HOST. The renderer runs on a machine that
# is created for a job and destroyed afterwards. Prometheus cannot scrape a
# machine that no longer exists, and anything stored on it dies with it —
# so the worker reports its own costs in the events it already sends, the
# ledger writes them into jobs.sqlite here, and /metrics reads them back.
# A destroyed instance's history survives because it was never on the
# instance. No exporter, no service discovery, no pushgateway.
#
# WHAT THIS COSTS. Prometheus and Grafana together want roughly 300-400 MB.
# On this box a render has already been measured peaking at 2.6 GB of 3.8 —
# so both are given hard memory limits below rather than being left to
# compete with the thing they exist to watch. If a limit is hit, systemd
# restarts that service; a dashboard that dies is better than a render
# that does.

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
METRICS="${VSW_METRICS_URL:-127.0.0.1:5000}"

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends prometheus prometheus-node-exporter adduser libfontconfig1 musl

if ! command -v grafana-server >/dev/null 2>&1; then
    say "Grafana"
    # Grafana is not in Debian/Ubuntu's own archive.
    install -d -m 0755 /etc/apt/keyrings
    curl -fsSL https://apt.grafana.com/gpg.key |
        gpg --dearmor -o /etc/apt/keyrings/grafana.gpg
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
        > /etc/apt/sources.list.d/grafana.list
    apt-get update -qq
    apt-get install -y -qq grafana
fi

say "Point Prometheus at the app"
# Only our own job is touched: this box may be somebody else's too.
cat > /etc/prometheus/prometheus.yml <<EOF
global:
  scrape_interval: 30s
  evaluation_interval: 30s

scrape_configs:
  - job_name: videosync
    static_configs:
      - targets: ['${METRICS}']

  - job_name: prometheus
    static_configs:
      - targets: ['127.0.0.1:9090']

  # The machine itself. Our own /metrics reports what a RENDER used; this
  # reports what the BOX has, which is the other half of the only question
  # that matters here — whether the next job fits. A render has already been
  # measured at 2457 MB of 3915 on a 7-minute recording, and the upload cap
  # is 8 minutes — lowered from 25 because 14.1 minutes measured 4.62 GB
  # against 3.9 GB of RAM and OOM-killed the box.
  - job_name: node
    static_configs:
      - targets: ['127.0.0.1:9100']
EOF

say "Loopback only"
# node_exporter binds to every interface by default and reports the machine's
# memory, disks, filesystems and network to anyone who asks. Same mistake as
# Prometheus, same fix, and it is checked at the end.
if [ -f /etc/default/prometheus-node-exporter ]; then
    if grep -q '^ARGS=' /etc/default/prometheus-node-exporter; then
        sed -i 's|^ARGS=.*|ARGS="--web.listen-address=127.0.0.1:9100"|'             /etc/default/prometheus-node-exporter
    else
        echo 'ARGS="--web.listen-address=127.0.0.1:9100"'             >> /etc/default/prometheus-node-exporter
    fi
fi

# Prometheus binds to every interface by default, and this box has a public
# address — which puts queue depth, failure counts and throughput on the
# open internet with no password. The same mistake the RabbitMQ package
# makes, and worth checking on every service this host gains.
if [ -f /etc/default/prometheus ]; then
    if grep -q '^ARGS=' /etc/default/prometheus; then
        sed -i 's|^ARGS=.*|ARGS="--web.listen-address=127.0.0.1:9090"|'             /etc/default/prometheus
    else
        echo 'ARGS="--web.listen-address=127.0.0.1:9090"' >> /etc/default/prometheus
    fi
fi

say "Keep both out of the renderer's way"
# 15 days rather than the default 15 GB of history: this is a handful of
# series scraped twice a minute, and the disk is shared with the videos.
install -d /etc/systemd/system/prometheus.service.d
cat > /etc/systemd/system/prometheus.service.d/override.conf <<'EOF'
[Service]
MemoryMax=350M
Nice=10
CPUWeight=20
EOF

install -d /etc/systemd/system/prometheus-node-exporter.service.d
cat > /etc/systemd/system/prometheus-node-exporter.service.d/override.conf <<'EOF'
[Service]
MemoryMax=64M
Nice=10
CPUWeight=20
EOF

install -d /etc/systemd/system/grafana-server.service.d
cat > /etc/systemd/system/grafana-server.service.d/override.conf <<'EOF'
[Service]
MemoryMax=300M
Nice=10
CPUWeight=20
EOF

say "The Infinity plugin, for the failure table"
# Counts come from Prometheus; the REASON a job failed cannot. An error
# message or an ffmpeg command line as a Prometheus label is unbounded
# cardinality — one new series per distinct failure — so the detail is read
# straight from the app as JSON, and this is what reads JSON.
# `grafana cli`, with a homepath. The old `grafana-cli` is deprecated in
# Grafana 13 and fails with "Could not find config defaults" — which the
# first version of this hid behind >/dev/null, so it reported a clean
# install of nothing.
if [ -d /var/lib/grafana/plugins/yesoreyeram-infinity-datasource ]; then
    echo "  already installed"
elif grafana cli --homepath /usr/share/grafana         --pluginsDir /var/lib/grafana/plugins         plugins install yesoreyeram-infinity-datasource 2>&1 | tail -2; then
    chown -R grafana:grafana /var/lib/grafana/plugins
else
    echo "  COULD NOT INSTALL — the failure table will have no datasource"
fi

say "Wire Grafana to Prometheus, and load the dashboard"
install -d /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards \
           /var/lib/grafana/dashboards

cat > /etc/grafana/provisioning/datasources/prometheus.yml <<'EOF'
apiVersion: 1

# Removed before being recreated, so the pinned uids below actually take.
# Grafana matches an existing datasource by NAME and REFUSES to change its
# uid: provisioning then fails outright with "data source not found", every
# module that depends on it fails with it, and the process exits and is
# restarted by systemd — forever. That is not a theory. Adding `uid:` to a
# datasource this box already had put Grafana into a crash loop, 50 restarts
# deep and serving nothing, until these three lines were added.
deleteDatasources:
  - name: Prometheus
    orgId: 1
  - name: VideoSync
    orgId: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    # Pinned, not generated. The alert rules name this uid, and a datasource
    # that gets a fresh random one on every rebuild would leave them pointing
    # at nothing — which shows up as an alert that never fires.
    uid: prometheus
    url: http://127.0.0.1:9090
    isDefault: true
EOF

cat >> /etc/grafana/provisioning/datasources/prometheus.yml <<EOF

  # Reads /api/failures from the app. Restricted to that one host: an
  # Infinity datasource left unrestricted will fetch any URL a dashboard
  # names, which is a request-forgery hole with a login on it.
  - name: VideoSync
    type: yesoreyeram-infinity-datasource
    uid: vsw-infinity
    access: proxy
    jsonData:
      allowedHosts:
        - http://${METRICS}
EOF

cat > /etc/grafana/provisioning/dashboards/videosync.yml <<'EOF'
apiVersion: 1
providers:
  - name: videosync
    folder: ''
    type: file
    disableDeletion: false
    # Edits in the browser are kept; the file wins again on restart. So
    # anything worth keeping goes back into deploy/grafana-dashboard.json.
    allowUiUpdates: true
    options:
      path: /var/lib/grafana/dashboards
EOF

install -m 0644 "$HERE/grafana-dashboard.json" /var/lib/grafana/dashboards/
chown -R grafana:grafana /var/lib/grafana/dashboards

say "The memory alert"
# Provisioned as a file, so a rebuilt Grafana still has it. Delivery is a
# separate question: without an SMTP server the rule still fires and is
# visible under Alerting, it just does not reach anybody who is not looking.
install -d /etc/grafana/provisioning/alerting
install -m 0644 "$HERE/grafana-alerts.yaml" /etc/grafana/provisioning/alerting/
# Inside the [smtp] section only. The first version of this grepped the whole
# file for "enabled = true", matched a line 550 lines further down in an
# unrelated section, and reported "SMTP is configured" about a section that
# was entirely commented out — a check that answers yes when the answer is no
# is worse than no check, because it is believed.
if awk '/^\[smtp\]/{s=1;next} /^\[/{s=0} s' /etc/grafana/grafana.ini |
       grep -qE '^[[:space:]]*enabled[[:space:]]*=[[:space:]]*true'; then
    echo "  SMTP is configured; alerts can be emailed"
else
    echo "  NO SMTP — the alert will fire and show in Grafana, but will not"
    echo "  email anyone. Configure [smtp] in /etc/grafana/grafana.ini and"
    echo "  add a contact point to send it somewhere."
fi

# Loopback only. Grafana is reached over an SSH tunnel until there is a
# reverse proxy in front of it with a password on the door; it ships with a
# default admin login and must not be left facing the internet.
install -d /etc/grafana
if ! grep -q '^http_addr = 127.0.0.1' /etc/grafana/grafana.ini 2>/dev/null; then
    printf '\n[server]\nhttp_addr = 127.0.0.1\n' >> /etc/grafana/grafana.ini
fi

systemctl daemon-reload
systemctl enable prometheus prometheus-node-exporter grafana-server
# RESTART, not `enable --now`. apt starts both at install time, before any
# of the configuration above exists — and `--now` leaves an already-running
# service alone, so it keeps the packaged defaults and scrapes a
# node_exporter that is not installed instead of this app. That is exactly
# what happened the first time this ran.
systemctl restart prometheus prometheus-node-exporter grafana-server
sleep 10

say "Nothing of ours is facing the internet"
ss -ltn | awk '$4 ~ /:(3000|9090|9100)$/ {print "  " $4}' | while read -r a; do
    case "$a" in
        *127.0.0.1:*) echo "  ok       $a" ;;
        *) echo "  EXPOSED  $a  <- this should be loopback only" ;;
    esac
done

say "State"
for unit in prometheus prometheus-node-exporter grafana-server; do
    printf '  %-18s %s\n' "$unit" "$(systemctl is-active "$unit")"
done
printf '  %-18s %s\n' "scraping" "$METRICS"

say "Is the app's endpoint actually being read?"
curl -fsS "http://127.0.0.1:9090/api/v1/targets?state=active" 2>/dev/null |
    grep -o '"health":"[a-z]*"' | sort | uniq -c | sed 's/^/  /' || echo "  (not answering yet)"

cat <<'NOTE'

  Grafana is on 127.0.0.1:3000 and nowhere else. To look at it:

      ssh -N -L 3000:127.0.0.1:3000 <this host>

  then open http://localhost:3000 — admin/admin on first login, and it
  will insist you change it.
NOTE
