#!/usr/bin/env bash
#
# Put the two services under systemd, so they survive a reboot and so that
# stopping one takes its children with it.
#
#   sudo deploy/install-units.sh
#
# Safe to run more than once. It touches only the two units named below and
# nothing else on the machine — this box may be shared.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNITS=(vsw-worker.service vsw-web.service)

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

say "Installing units"
for unit in "${UNITS[@]}"; do
    install -m 0644 "$HERE/$unit" "/etc/systemd/system/$unit"
    echo "  /etc/systemd/system/$unit"
done

systemctl daemon-reload

say "Enabling and starting"
for unit in "${UNITS[@]}"; do
    systemctl enable --now "$unit"
done

# Give them a moment to fail, if they are going to.
sleep 5

say "State"
for unit in "${UNITS[@]}"; do
    printf '  %-22s %s\n' "$unit" "$(systemctl is-active "$unit")"
done

say "What is in each cgroup"
# This is the check that matters: everything a unit starts must be inside
# its own cgroup, because that is what systemd signals when the unit stops.
# An ffmpeg outside it would survive its worker and hold a core.
for unit in "${UNITS[@]}"; do
    echo "  $unit"
    systemctl show -p ControlGroup --value "$unit" | while read -r cg; do
        [ -n "$cg" ] && cat "/sys/fs/cgroup${cg}/cgroup.procs" 2>/dev/null |
            while read -r pid; do
                printf '      %-7s %s\n' "$pid" \
                    "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-70)"
            done
    done
done

cat <<'NOTE'

  systemctl status vsw-worker vsw-web
  journalctl -u vsw-worker -f

  A stop during a render costs a re-render: the broker still holds the
  message and the per-attempt record stops finished work being repeated,
  so the job comes back rather than being lost.
NOTE
