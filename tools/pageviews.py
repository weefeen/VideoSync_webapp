"""Who came to the site, from the web server's own log.

    python tools/pageviews.py              a day-by-day summary
    python tools/pageviews.py --where      grouped by country
    python tools/pageviews.py --bots       what the scanners asked for
    python tools/pageviews.py --json       machine-readable

RUNS ON THE SERVER, because that is where the log is. `tools/visitors.py`
answers a different question from the database -- who UPLOADED -- and most
people who open a page never upload anything, so it cannot tell you how
many came.

WHY THE LOG AND NOT A COUNTER IN THE APP. The log is already written, is
already rotated, and already holds the history: a counter added today
would start at zero today. A counter is the better long-run answer and the
metrics carry one; this is how you see the weeks before it existed.

ONE VISIT IS ONE ADDRESS ON ONE DAY. Not a session and not a person: a
household behind one router counts once, a phone that moves from wifi to
mobile counts twice. It is the honest resolution of an access log and the
number is described that way everywhere it is printed.
"""

from __future__ import annotations

import argparse
import collections
import glob
import gzip
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

LOGS = "/var/log/apache2/vsw-access.log*"

# BOTH FORMATS, because the log holds both. It was `combined` until the
# hostname mattered and `vhost_combined` after: the older lines start with
# the client address, the newer ones with `name:port` in front of it.
# Reading only one would silently drop half the history.
LINE = re.compile(
    r'^(?:(\S+?):\d+ )?(\S+) \S+ \S+ \[([^:]+):\S+ [^\]]+\]'
    r' "(\S+) (\S+) [^"]*" (\d+) \S+ "([^"]*)" "([^"]*)"')

# THE SITE IS ITS NAME, NOT ITS ADDRESS. Crawlers find this box through
# the public certificate-transparency log and arrive at the raw IP;
# people arrive at the hostname somebody gave them. Counting both together
# was how 130 "visitors" turned out to be Amazon machines and one person.
SITE = "chopin.weefeen.com"

# THE PUBLIC INTERNET IS MOSTLY MACHINES, and the user agent does not say
# so. Most of what reaches this box is scanners asking for `/.env`,
# `/.aws/credentials` and `/.bash_history` -- those are easy. The ones
# that matter claim to be Chrome, run a headless browser, fetch every
# asset and call the API, and are indistinguishable from a visitor by
# anything in the request.
#
# WHAT GIVES THEM AWAY IS WHERE THEY LIVE. They run on rented machines,
# and rented machines have reverse DNS that says so. Measured on this
# site's first four days: 130 addresses looked like people by their
# behaviour, and reverse DNS resolved all but one to
# `ec2-*.compute-1.amazonaws.com`. The one exception was a Japanese
# consumer ISP and was the operator.
#
# Why they turn up at all: a new HTTPS certificate publishes its hostname
# in the public Certificate Transparency log, and crawlers watch those
# logs. They arrived the day the certificate was issued.
BOT = re.compile(
    r"bot|crawler|spider|scan|curl|wget|python-requests|go-http|libwww"
    r"|headless|phantom|semrush|ahrefs|mj12|dotbot|bingpreview|slurp"
    r"|facebookexternalhit|whatsapp|telegram|preview", re.I)

# Reverse-DNS fragments that mean "this is a rented machine, not a home".
# Checked against the name, never against the address, so a new range in
# an existing cloud is caught without a list of prefixes to maintain.
RENTED = re.compile(
    r"amazonaws|compute-1|ec2|googleusercontent|google\.com|azure|cloudapp"
    r"|linode|digitalocean|vultr|ovh|hetzner|scaleway|contabo|oracle"
    r"|alibaba|aliyun|tencent|bytedance|datacenter|hosting|server|vps"
    r"|colo|cdn|proxy", re.I)


def rented(ip: str) -> bool:
    """Whether this address belongs to a datacentre, by its own name.

    A LOOKUP PER ADDRESS, so this is slow on a large log and cached here.
    Unknown is treated as NOT rented: a home connection often has no
    reverse DNS at all, and guessing the other way would delete real
    visitors from the count, which is the error that matters.
    """
    import socket
    if ip in _NAMES:
        return _NAMES[ip]
    try:
        name = socket.gethostbyaddr(ip)[0]
    except Exception:                                  # noqa: BLE001
        name = ""
    _NAMES[ip] = bool(name) and bool(RENTED.search(name))
    return _NAMES[ip]


_NAMES: dict = {}

# The page itself, not its assets. A visit is a person opening the site;
# counting the javascript and the logo would multiply every visit by the
# number of files the page happens to be built from.
PAGES = ("/app/", "/app/index.html", "/")


def lines(pattern: str):
    for path in sorted(glob.glob(pattern)):
        opener = gzip.open if path.endswith(".gz") else open
        try:
            with opener(path, "rt", errors="replace") as fh:
                yield from fh
        except OSError as exc:
            print(f"  could not read {path}: {exc}", file=sys.stderr)


def read(pattern: str) -> dict:
    """Everything the log can say, in one pass."""
    days: dict[str, set] = collections.defaultdict(set)
    loads: collections.Counter = collections.Counter()
    people: set = set()
    bots: set = set()
    bot_paths: collections.Counter = collections.Counter()
    agents: collections.Counter = collections.Counter()
    referrers: collections.Counter = collections.Counter()

    for raw in lines(pattern):
        m = LINE.match(raw)
        if not m:
            continue
        host, ip, day, method, path, code, referrer, agent = m.groups()
        if method != "GET":
            continue
        # Where the hostname is recorded, it decides. Lines from before
        # the format changed have none and are judged on everything else,
        # so the history is not thrown away.
        if host and SITE not in host:
            bots.add(ip)
            continue
        if BOT.search(agent) or not agent.strip() or agent == "-":
            bots.add(ip)
            bot_paths[path] += 1
            continue
        if path not in PAGES:
            continue
        # A redirect is not a page view: `/` answers 302 to `/app/`, and
        # counting both would double every arrival.
        if code.startswith("3"):
            continue
        if rented(ip):
            bots.add(ip)
            continue
        days[day].add(ip)
        loads[day] += 1
        people.add(ip)
        agents[agent[:70]] += 1
        if referrer and referrer != "-":
            referrers[referrer[:70]] += 1

    return {"days": {d: sorted(v) for d, v in days.items()},
            "loads": dict(loads), "people": sorted(people),
            "bots": sorted(bots), "bot_paths": bot_paths.most_common(10),
            "agents": agents.most_common(8),
            "referrers": referrers.most_common(8)}


def _order(days) -> list:
    """Chronological, not alphabetical: 11/Sep sorts before 2/Oct."""
    import time as _t

    def key(d):
        try:
            return _t.mktime(_t.strptime(d, "%d/%b/%Y"))
        except ValueError:
            return 0.0
    return sorted(days, key=key)


def summary(data: dict) -> None:
    print()
    print("  VISITORS")
    print("  " + "-" * 44)
    if not data["days"]:
        print("  nothing in the log yet")
    print(f"  {'day':14} {'people':>7} {'loads':>7}")
    for day in _order(data["days"]):
        print(f"  {day:14} {len(data['days'][day]):7d} "
              f"{data['loads'].get(day, 0):7d}")
    print()
    print(f"  unique addresses overall : {len(data['people'])}")
    print(f"  machines, not counted    : {len(data['bots'])}")
    print()
    print("  One address on one day counts once. Not a person and not a")
    print("  session: a household behind one router counts once, a phone")
    print("  moving from wifi to mobile counts twice.")
    if data["referrers"]:
        print()
        print("  CAME FROM")
        for who, n in data["referrers"]:
            print(f"    {n:5d}  {who}")
    print()


def where(data: dict) -> None:
    """Grouped by country, using the same lookup the site uses."""
    try:
        from app import geo
        lookup = geo.locate
    except Exception:                                  # noqa: BLE001
        lookup = None
    if lookup is None:
        print("  no geo database on this machine; showing addresses only")
        for ip in data["people"][:40]:
            print("   ", ip)
        return
    tally: collections.Counter = collections.Counter()
    for ip in data["people"]:
        try:
            found = lookup(ip) or {}
        except Exception:                              # noqa: BLE001
            found = {}
        tally[(found.get("country") or "unknown")] += 1
    print()
    print("  BY COUNTRY")
    for country, n in tally.most_common():
        print(f"    {n:5d}  {country}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", default=LOGS, help="log glob to read")
    ap.add_argument("--where", action="store_true", help="group by country")
    ap.add_argument("--bots", action="store_true", help="what scanners asked")
    ap.add_argument("--json", action="store_true", help="machine-readable")
    args = ap.parse_args()

    data = read(args.log)

    if args.json:
        print(json.dumps(
            {"people": len(data["people"]), "bots": len(data["bots"]),
             "by_day": {d: {"people": len(v), "loads": data["loads"].get(d, 0)}
                        for d, v in data["days"].items()}},
            indent=2))
        return 0
    if args.bots:
        print()
        print("  WHAT THE SCANNERS ASKED FOR")
        for path, n in data["bot_paths"]:
            print(f"    {n:5d}  {path[:60]}")
        print()
        return 0
    summary(data)
    if args.where:
        where(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
