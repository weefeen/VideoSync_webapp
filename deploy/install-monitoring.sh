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
apt-get install -y -qq --no-install-recommends prometheus adduser libfontconfig1 musl

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
EOF

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

install -d /etc/systemd/system/grafana-server.service.d
cat > /etc/systemd/system/grafana-server.service.d/override.conf <<'EOF'
[Service]
MemoryMax=300M
Nice=10
CPUWeight=20
EOF

say "Wire Grafana to Prometheus, and load the dashboard"
install -d /etc/grafana/provisioning/datasources /etc/grafana/provisioning/dashboards \
           /var/lib/grafana/dashboards

cat > /etc/grafana/provisioning/datasources/prometheus.yml <<'EOF'
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://127.0.0.1:9090
    isDefault: true
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

# Loopback only. Grafana is reached over an SSH tunnel until there is a
# reverse proxy in front of it with a password on the door; it ships with a
# default admin login and must not be left facing the internet.
install -d /etc/grafana
if ! grep -q '^http_addr = 127.0.0.1' /etc/grafana/grafana.ini 2>/dev/null; then
    printf '\n[server]\nhttp_addr = 127.0.0.1\n' >> /etc/grafana/grafana.ini
fi

systemctl daemon-reload
systemctl enable --now prometheus grafana-server
sleep 8

say "State"
for unit in prometheus grafana-server; do
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
