#!/usr/bin/env bash
#
# Close what should not be open, and keep a copy of what cannot be replaced.
#
#   sudo deploy/harden.sh
#
# Safe to run more than once.
#
# Three things, all found by auditing the box after it went public:
#
#   epmd      RabbitMQ's port mapper was answering the internet on 4369,
#             handing out Erlang node names to anyone who asked.
#   firewall  ufw was installed and inactive, so anything that ever starts
#             listening is reachable by default rather than by decision.
#   backups   jobs.sqlite held every job, every visitor address and every
#             measurement, with no copy anywhere.

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

ROOT="${VSW_ROOT:-/srv/vsw}"

say "epmd: answer only this machine"
# epmd is SOCKET-ACTIVATED by systemd on Ubuntu: systemd binds the port and
# hands epmd the file descriptor, so the daemon never opens a socket itself.
# ERL_EPMD_ADDRESS in rabbitmq-env.conf therefore does nothing whatsoever —
# which is what the first version of this script set, and the port stayed on
# every interface. The binding belongs in the socket unit.
install -d /etc/systemd/system/epmd.socket.d
cat > /etc/systemd/system/epmd.socket.d/loopback.conf <<'CONF'
[Socket]
# The empty assignment first is REQUIRED: without it this ListenStream is
# added to the packaged ListenStream=4369 rather than replacing it, and the
# port stays open on every interface while looking configured.
ListenStream=
ListenStream=127.0.0.1:4369
CONF

systemctl daemon-reload
systemctl stop epmd.socket epmd.service 2>/dev/null || true
systemctl start epmd.socket
sleep 3
# The broker registers with epmd at start, so it has to come back after the
# socket moved or it will be advertising itself to a daemon that is gone.
systemctl restart rabbitmq-server
sleep 15
printf '  rabbitmq: %s\n' "$(systemctl is-active rabbitmq-server)"
ss -ltn | awk '$4 ~ /:4369$/ {print "  epmd on " $4}'

say "Firewall"
# SSH FIRST, and verified present before enabling. Enabling ufw with no
# allow rule for 22 locks everybody out of a machine reachable only by SSH,
# and the only way back is the provider's console.
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp   comment 'ssh'
ufw allow 80/tcp   comment 'http, and the ACME challenge'
ufw allow 443/tcp  comment 'https'

if ! ufw show added | grep -q '22/tcp'; then
    echo "  REFUSING to enable: no rule for 22 was added" >&2
    exit 1
fi
ufw --force enable
ufw status verbose | sed 's|^|  |'

say "Back up the one file that cannot be rebuilt"
install -d "$ROOT/backups"
cat > /usr/local/bin/vsw-backup <<'SCRIPT'
#!/usr/bin/env bash
# A consistent copy of jobs.sqlite, kept locally and in the bucket.
set -euo pipefail
ROOT="${VSW_ROOT:-/srv/vsw}"
DB="$ROOT/shared/var/jobs.sqlite"
OUT="$ROOT/backups/jobs-$(date -u +%Y%m%d-%H%M%S).sqlite"
[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }

# `.backup`, NOT cp. The database is in WAL mode and written to while this
# runs; copying the file alone gives a torn snapshot missing whatever is in
# the write-ahead log, which restores as a corrupt database at the worst
# possible moment.
"$ROOT/venv/bin/python" - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
b = sqlite3.connect(dst)
with b:
    a.backup(b)
a.close(); b.close()
PY
gzip -f "$OUT"

# Off the machine as well. A backup on the same disk as the original
# survives a mistake and not a dead disk, and the bucket is already here.
"$ROOT/venv/bin/python" - "$OUT.gz" <<'PY'
import pathlib, sys
sys.path.insert(0, "/srv/vsw/current")
from app import storage
local = pathlib.Path(sys.argv[1])
if storage.available():
    key = f"backups/{local.name}"
    size = storage.put(local, key)
    print(f"  {key}  {size} bytes, confirmed in the bucket")
else:
    print(f"  bucket unavailable ({storage.status()['problem']}); local only")
PY

# Three weeks locally. The bucket keeps its own for as long as its lifecycle
# rules say, which is where the long history belongs.
find "$ROOT/backups" -name 'jobs-*.sqlite.gz' -mtime +21 -delete
SCRIPT
chmod +x /usr/local/bin/vsw-backup

cat > /etc/systemd/system/vsw-backup.service <<'UNIT'
[Unit]
Description=Back up the VideoSync job database
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vsw-backup
UNIT

cat > /etc/systemd/system/vsw-backup.timer <<'UNIT'
[Unit]
Description=Nightly VideoSync database backup

[Timer]
# Late enough that a long render started in the evening has finished, and
# randomised so it never lands on the same second as anything else.
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now vsw-backup.timer

say "Prove it works, now, rather than trusting it at 03:30"
/usr/local/bin/vsw-backup

say "And prove the copy is readable — an unverified backup is a hope"
LATEST=$(ls -1t "$ROOT/backups"/jobs-*.sqlite.gz | head -1)
gunzip -c "$LATEST" > /tmp/verify.sqlite
"$ROOT/venv/bin/python" - <<'PY'
import sqlite3
c = sqlite3.connect("/tmp/verify.sqlite")
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
n = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
r = c.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0]
print(f"  integrity_check: {ok}")
print(f"  {n} jobs and {r} recognitions are in the copy")
if ok != "ok":
    raise SystemExit("  THE BACKUP IS NOT READABLE")
PY
rm -f /tmp/verify.sqlite
ls -lh "$ROOT/backups" | tail -3 | sed 's|^|  |'

say "What faces the internet now"
ss -ltn | awk 'NR > 1 && $4 !~ /127\.0\.0\.|::1/ {print "  " $4}'
echo
systemctl list-timers vsw-backup.timer --no-pager | sed -n 2p | sed 's|^|  next backup: |'
