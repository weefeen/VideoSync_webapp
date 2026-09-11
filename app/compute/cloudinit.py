"""What a compute node is told at birth.

The image deliberately carries no credentials: it holds software and nothing
else, so a leaked image gives an attacker a copy of this application rather
than access to the queue, the bucket or anybody's video. Everything secret
therefore arrives here, once, in the `user_data` of the create call, and
cloud-init writes it before the worker starts.

WHAT TRAVELS AND WHAT DOES NOT. The web box's own `.env` is the source, with
two removals that matter:

    LINODE_TOKEN    creates and destroys machines. A node holding it could
                    create more nodes. It never leaves the web box.
    SMTP_*          only the web side sends mail — the ledger does it when a
                    render finishes. A worker has no reason to hold a
                    password that can send as info@weefeen.com.

and two replacements:

    RABBITMQ_URL    the node reaches the broker over the VLAN, as a
                    restricted user that may only read vsw.render and write
                    vsw.events.
    WORK_DIR        its own disk, which dies with it.

TWO THINGS THE IMAGE SHOULD CARRY AND DOES NOT, handled here until it is
rebuilt: the `vsw` user, and a worker unit. The unit shipped in deploy/ has
`Requires=rabbitmq-server.service`, which is right on the web box and wrong
here — a compute node has no broker of its own and the unit would refuse to
start.
"""
from __future__ import annotations

import re
import shlex

# Never sent to a compute node, whatever the source .env happens to contain.
# Matched as prefixes, so SMTP_PASSWORD goes with the rest of the family.
WITHHELD = ("LINODE_TOKEN", "SMTP_", "ALERT_EMAIL", "COMPUTE_",
            "TRUST_PROXY", "PUBLIC_BASE_URL", "GEOIP_DB")

WORKER_UNIT = """[Unit]
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
"""


def environment(source: str, broker_url: str,
                work_dir: str = "/srv/vsw/shared/var") -> str:
    """The .env a compute node should have, from the one the web box has.

    Taking the web box's file rather than listing what a worker needs, on
    purpose: the list would be wrong the first time somebody adds a setting
    and forgets this file, and the failure would be a render that behaves
    differently on the compute node than in testing — the worst kind.
    Removing what must not travel is a rule that stays correct as settings
    are added.
    """
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = stripped.split("=", 1)[0].strip()
        if any(name.startswith(p) for p in WITHHELD):
            continue
        if name in ("RABBITMQ_URL", "WORK_DIR"):
            continue
        out.append(stripped)

    out.append(f"RABBITMQ_URL={broker_url}")
    out.append(f"WORK_DIR={work_dir}")
    return "\n".join(out) + "\n"


def user_data(env: str) -> str:
    """The cloud-config a machine is created with.

    `write_files` before `runcmd`, which is cloud-init's own ordering, so the
    worker never starts against a half-written .env.
    """
    return f"""#cloud-config
# Written by the VideoSync scaler. Everything secret this machine holds
# arrives here and is not in the image.
write_files:
  - path: /srv/vsw/shared/.env
    permissions: '0600'
    owner: root:root
    content: |
{_indent(env, 6)}
  - path: /etc/systemd/system/vsw-worker.service
    permissions: '0644'
    content: |
{_indent(WORKER_UNIT, 6)}

runcmd:
  # The account the worker runs as. The image was built by copying files in
  # as root, so this does not exist yet; baking it into the next image would
  # save these two steps.
  - [ id, -u, vsw ]
  - bash -c 'id -u vsw >/dev/null 2>&1 || useradd --system --home /srv/vsw --shell /usr/sbin/nologin vsw'
  - [ install, -d, -o, vsw, -g, vsw, /srv/vsw/shared/var ]
  - [ chown, -R, 'vsw:vsw', /srv/vsw/shared ]
  - [ chown, -R, 'vsw:vsw', /srv/vsw/current ]
  # The venv's scripts are read and executed, not written, so ownership is
  # left alone — chowning 1.6 GB at every boot would add seconds to a
  # create that a visitor is waiting through.
  - [ systemctl, daemon-reload ]
  - [ systemctl, enable, --now, vsw-worker ]

# A marker the scaler can look for to know cloud-init finished rather than
# guessing from uptime.
final_message: "vsw-compute ready after $UPTIME seconds"
"""


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.rstrip("\n").splitlines())


def redacted(data: str) -> str:
    """The same thing with the secrets starred, for logs and tests.

    Exists because the obvious way to debug a bad user_data is to print it,
    and the obvious way is how a broker password ends up in a log file that
    outlives the machine.
    """
    def mask(match: re.Match) -> str:
        return f"{match.group(1)}=<redacted>"

    return re.sub(r"^(\w*(?:PASSWORD|SECRET|TOKEN|KEY|URL)\w*)=.*$",
                  mask, data, flags=re.M)
