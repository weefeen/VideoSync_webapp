#!/usr/bin/env bash
#
# Make a provisioned compute node safe and small enough to capture as an
# image. Run ON THE NODE, immediately before capturing, and never on the web
# box.
#
#   ssh root@<node> 'bash -s' < deploy/compute-image-prep.sh
#
# Then power the instance off and capture the disk in Cloud Manager.
#
# WHAT AN IMAGE IS. A byte copy of the disk, deployed unchanged onto every
# machine made from it. So anything on that disk is on every future compute
# node, and anyone who can read the image can read it. That makes two
# categories of thing unacceptable in a capture:
#
#   SECRETS      .env carries the SMTP password and the bucket keys. Baked
#                in, they are in every instance and in the image itself, and
#                rotating them means rebuilding the image. They are passed at
#                create time through user_data instead, so a leaked image
#                gives an attacker software, not access.
#
#   IDENTITY     machine-id and the SSH host keys are meant to be unique per
#                machine. Captured, every compute node is indistinguishable
#                from every other, which breaks host-key checking in exactly
#                the way that trains people to ignore the warning.
#
# The size limit is 6 GB, and Linode requires a 10% buffer, so the disk must
# actually be using UNDER 5.4 GB. Measured on a provisioned node: 4.2 GB with
# this cleanup, 6.6 GB without.

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

if [ -f /srv/vsw/shared/.env ] && grep -q '^LINODE_TOKEN=' /srv/vsw/shared/.env; then
    echo "REFUSING: this looks like the web box — it holds the Linode token." >&2
    echo "Running here would delete the credentials the site needs." >&2
    exit 1
fi

say "Working data — never belongs in an image"
rm -rf /srv/vsw/shared/var /srv/vsw/current/var /srv/vsw/backups
rm -f /tmp/*.mp4 /srv/vsw/*.mp4
install -d /srv/vsw/shared/var
echo "  renders, uploads and staging removed"

say "Credentials — passed at boot, not baked in"
# The file is replaced by a marker rather than deleted, so an instance that
# somehow boots without user_data fails with something legible instead of a
# confusing KeyError deep in settings.
if [ -f /srv/vsw/shared/.env ]; then
    rm -f /srv/vsw/shared/.env
fi
cat > /srv/vsw/shared/.env.missing <<'NOTE'
# There is no .env in this image, on purpose.
#
# The broker password and the bucket keys are written here by cloud-init
# from the user_data the scaler supplies at create time. If you are reading
# this on a running instance, that did not happen: check the user_data on
# the create call.
NOTE
echo "  .env removed; a note left in its place"

say "Caches and logs"
export DEBIAN_FRONTEND=noninteractive
apt-get clean
rm -rf /var/lib/apt/lists/*
find /srv/vsw -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
rm -rf /var/log/*.gz /var/log/*.[0-9] /root/.cache /home/*/.cache 2>/dev/null || true
: > /root/.bash_history 2>/dev/null || true
echo "  apt, journal, pycache and shell history cleared"

say "Identity — regenerated per instance, not inherited"
# machine-id must be empty (not absent): systemd writes a fresh one at first
# boot if the file exists and is empty, and fails oddly if it is missing.
: > /etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -sf /etc/machine-id /var/lib/dbus/machine-id
# Host keys are regenerated on first boot by the packaged service; leaving
# them in means every compute node presents the SAME host key, so a real
# man-in-the-middle looks identical to the ordinary case.
rm -f /etc/ssh/ssh_host_*
systemctl enable ssh.service >/dev/null 2>&1 || true
# Authorized keys come from the create call too, so the image grants nobody
# access on its own.
rm -f /root/.ssh/authorized_keys
echo "  machine-id, host keys and authorized_keys cleared"

say "Is it small enough to capture?"
used=$(df --output=used -BM / | tail -1 | tr -dc '0-9')
printf '  %s MB used, against a 5400 MB limit\n' "$used"
du -sh /usr /srv /var 2>/dev/null | sort -rh | sed 's|^|  |'
if [ "$used" -lt 5400 ]; then
    echo
    echo "  FITS. Power the instance off, then capture the disk:"
    echo "    Cloud Manager -> the instance -> Storage -> the disk -> Create Image"
else
    echo
    echo "  TOO BIG by $((used - 5400)) MB. Capture will be refused."
    exit 1
fi

cat <<'NOTE'

  After capturing, grant the scaler user read access to the new image:
    Administration -> Identity & Access -> vsw-scaler
      -> Assign New Roles -> image_viewer, on THAT image only

  And remember this instance is still billable until it is DELETED.
  Powering it off does not stop the meter.
NOTE
