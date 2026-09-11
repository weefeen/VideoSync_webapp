#!/usr/bin/env bash
#
# Let the compute node reach the broker, over the private VLAN and nowhere
# else.
#
#   sudo deploy/install-vlan-broker.sh
#
# Safe to run more than once.
#
# WHY A VLAN AND NOT A PUBLIC LISTENER. Linode's VLAN is account-isolated
# Layer 2: only machines on this account, in this region, attached to this
# VLAN can see it. A public listener would put the queue on the internet
# behind a password, and the firewall could not help — the compute node's
# public address changes on every create, so any allow-rule naming it would
# be wrong for a window on each one.
#
# The broker keeps its loopback listener too. The web app and the worker on
# this box connect over 127.0.0.1 and have no business on the VLAN.

set -euo pipefail

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this as root" >&2; exit 1; }

VLAN_IP="${VSW_VLAN_IP:-10.0.0.2}"
NODE_IP="${VSW_NODE_IP:-10.0.0.3}"

if ! ip -brief addr show | grep -q "$VLAN_IP"; then
    echo "No interface holds $VLAN_IP. Attach the VLAN in Cloud Manager and" >&2
    echo "reboot before running this." >&2
    exit 1
fi
echo "  VLAN address present: $VLAN_IP"

say "Bind the broker to loopback AND the VLAN — never the public interface"
# Listing the addresses explicitly rather than leaving the default, which is
# every interface. The distinction matters on a box with a public IP: the
# default would have put the queue on the internet the moment the firewall
# was opened for anything.
install -d /etc/rabbitmq
# REWRITTEN, not appended to. Appending broke this the first time in two
# ways at once: `listeners.tcp.default` and `listeners.tcp.local` both named
# 127.0.0.1:5672, which is "address already in use", and an earlier run had
# already added `distribution.listener.interface`, so it appeared twice —
# and a duplicate key in rabbitmq.conf is a parse error rather than
# last-one-wins. The broker simply did not come back.
[ -f /etc/rabbitmq/rabbitmq.conf ] &&
    cp /etc/rabbitmq/rabbitmq.conf "/etc/rabbitmq/rabbitmq.conf.$(date +%s)"
cat > /etc/rabbitmq/rabbitmq.conf <<CONF
# Explicit listen addresses. The default is 0.0.0.0, which on a box with a
# public IP means the internet. Exactly one listener per address:port.
listeners.tcp.local = 127.0.0.1:5672
listeners.tcp.vlan  = ${VLAN_IP}:5672

# The Erlang distribution port, used for clustering. There is no cluster and
# it is protected only by the Erlang cookie. ONE entry.
distribution.listener.interface = 127.0.0.1
CONF

say "A separate broker user for the compute node"
# Not the web app's credentials. A compute node is created from an image,
# handed credentials, and deleted; whatever it holds should be revocable
# without touching the site. Permissions are limited to the two queues it
# actually uses.
COMPUTE_PASS="$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)"
rabbitmqctl -q list_users 2>/dev/null | grep -q '^vsw-compute' \
    && rabbitmqctl -q delete_user vsw-compute >/dev/null 2>&1 || true
rabbitmqctl -q add_user vsw-compute "$COMPUTE_PASS" >/dev/null
# Read the render queue, write the events queue, and nothing else: it may
# not declare, delete or purge, so a compromised node cannot remove the work
# of others or reshape the topology.
rabbitmqctl -q set_permissions -p vsw vsw-compute \
    '^$' '^(vsw\.events|vsw\.render)$' '^(vsw\.events|vsw\.render)$' >/dev/null
echo "  user vsw-compute created, limited to vsw.render and vsw.events"

say "Restart, and check what is listening"
systemctl restart rabbitmq-server
sleep 15
ss -ltn | awk '$4 ~ /:5672$/ {print "  " $4}'
if ss -ltn | awk '$4 ~ /:5672$/ {print $4}' | grep -qE '^(0\.0\.0\.0|\*|\[::\])'; then
    echo "  THE BROKER IS ON EVERY INTERFACE — fix before going further" >&2
    exit 1
fi
echo "  loopback and VLAN only, as intended"

say "Can the existing app still connect?"
systemctl restart vsw-web vsw-worker
sleep 8
timeout 60 rabbitmqctl -q list_queues -p vsw name consumers 2>/dev/null |
    tail -5 | sed 's|^|  |'

say "What a compute node will be told"
echo "  RABBITMQ_URL=amqp://vsw-compute:<password>@${VLAN_IP}:5672/vsw"
echo
echo "  The password is written to /srv/vsw/shared/compute-broker.pass so the"
echo "  scaler can put it in each machine's user_data. It is NOT in .env,"
echo "  because .env is copied to nothing and this has to be handed out."
printf '%s' "$COMPUTE_PASS" > /srv/vsw/shared/compute-broker.pass
chmod 600 /srv/vsw/shared/compute-broker.pass
chown vsw:vsw /srv/vsw/shared/compute-broker.pass
ls -l /srv/vsw/shared/compute-broker.pass | awk '{print "  " $1, $3, $4, $9}'

cat <<NOTE

  The node at ${NODE_IP} will reach the broker at ${VLAN_IP}:5672 over the
  VLAN. Nothing on the public internet can, and the firewall has no rule for
  5672 precisely because none is needed.
NOTE
