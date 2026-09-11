#!/usr/bin/env bash
#
# Turn a bare Ubuntu instance into a compute node ready to be captured.
#
#   bash deploy/compute-image-build.sh <node-ip>     # run ON THE WEB BOX
#
# Then:
#   ssh root@<node> 'bash -s' < deploy/compute-image-prep.sh
#   power off, capture the disk in Cloud Manager.
#
# WHY IT COPIES FROM THE WEB BOX rather than installing from
# requirements.txt. The web box's venv is the one every measurement in
# docs/ was taken against, and a fresh `pip install` resolves to whatever
# versions exist today — which is how a compute node quietly renders
# differently from the machine the numbers came from. The trade is
# reproducibility of *this* build against reproducibility from scratch, and
# for a render pipeline the first matters more.
#
# WHAT IT DELIBERATELY DOES NOT COPY:
#   .env                secrets reach a node in user_data, never in an image
#   var/                renders and uploads — the first attempt at this
#                       shipped 1.8 GB of visitor videos into the image,
#                       because rsync -L follows the `var` symlink
#   __pycache__         bytecode compiled for a different path

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
NODE="${1:?usage: compute-image-build.sh <node-ip>}"
ROOT="${VSW_ROOT:-/srv/vsw}"
SSH="ssh -o StrictHostKeyChecking=accept-new"

say "System packages the render needs"
$SSH "root@$NODE" '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
      ffmpeg python3 libcairo2 libgl1 libglib2.0-0 rsync >/dev/null
  printf "  ffmpeg %s\n" "$(ffmpeg -version | head -1 | cut -d" " -f3)"
'

say "The account the worker runs as"
# Created HERE rather than by cloud-init at every boot: it costs seconds on
# each create otherwise, during which a visitor is waiting.
$SSH "root@$NODE" '
  id -u vsw >/dev/null 2>&1 ||
    useradd --system --home /srv/vsw --shell /usr/sbin/nologin vsw
  install -d -o vsw -g vsw /srv/vsw/shared/var
  echo "  vsw exists"
'

say "The stack"
# NO -L. The `var` inside a release is a symlink to the shared working
# directory, and following it copies every rendered video onto the image.
for d in venv scores VideoScoreSync music_fingerprints; do
    [ -e "$ROOT/$d" ] || continue
    printf '  %-20s' "$d"
    rsync -a --delete "$ROOT/$d/" "root@$NODE:/srv/vsw/$d/"
    echo "ok"
done
printf '  %-20s' "app code"
rsync -a --delete --copy-unsafe-links \
      --exclude '.git' --exclude 'var' --exclude '__pycache__' \
      --exclude '*.pyc' --exclude '.env' \
      "$ROOT/current/" "root@$NODE:/srv/vsw/current/"
echo "ok"
printf '  %-20s' "fonts"
rsync -a "$ROOT/shared/fonts/" "root@$NODE:/srv/vsw/shared/fonts/"
echo "ok"

say "The worker unit"
# NOT the one in deploy/: that has `Requires=rabbitmq-server.service`, which
# is right on the web box and wrong here — a compute node has no broker of
# its own and the unit would refuse to start.
$SSH "root@$NODE" 'cat > /etc/systemd/system/vsw-worker.service <<UNIT
[Unit]
Description=VideoSync render worker
After=network-online.target cloud-final.service
Wants=network-online.target

[Service]
Type=simple
User=vsw
Group=vsw
WorkingDirectory=/srv/vsw/current
Environment=HOME=/srv/vsw
Environment=PYTHONUNBUFFERED=1
ExecStart=/srv/vsw/venv/bin/python -m app.queue.worker
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
# Enabled but NOT started: there is no .env yet, and it arrives at first
# boot from user_data. systemd will start it then.
systemctl daemon-reload
systemctl enable vsw-worker >/dev/null 2>&1
echo "  vsw-worker installed and enabled, not started"'

say "Ownership"
$SSH "root@$NODE" 'chown -R vsw:vsw /srv/vsw/current /srv/vsw/shared; echo "  done"'

say "Does the stack import, and can it see the bucket?"
# Proved BEFORE capture, so a broken image fails in front of a person rather
# than at three in the morning under a visitor's job.
scp -q "$ROOT/shared/.env" "root@$NODE:/tmp/probe.env"
$SSH "root@$NODE" 'cd /srv/vsw/current && \
  env $(grep -v "^#" /tmp/probe.env | grep . | xargs -d "\n") \
  /srv/vsw/venv/bin/python -c "
import sys; sys.path.insert(0, \".\")
from app import render, pipeline, storage
import torch, librosa
print(\"  app imports, torch\", torch.__version__, \"librosa\", librosa.__version__)
print(\"  usable score packages:\", len(pipeline.usable_packages()))
print(\"  bucket reachable:\", storage.status()[\"available\"])
"; rm -f /tmp/probe.env'

say "Size, against the capture limit"
$SSH "root@$NODE" '
  used=$(df --output=used -BM / | tail -1 | tr -dc "0-9")
  echo "  $used MB used; the limit is 5400 MB after cleanup"
  du -sh /usr /srv 2>/dev/null | sed "s|^|  |"
'

cat <<'NOTE'

  Next:
    ssh root@<node> 'bash -s' < deploy/compute-image-prep.sh
    power off, then Storage -> the disk -> Create Disk Image
NOTE
