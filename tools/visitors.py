"""The visitor list, on a terminal.

    python tools/visitors.py                 everything
    python tools/visitors.py --who           addresses, with country and city
    python tools/visitors.py --where         the same, grouped by country
    python tools/visitors.py --what          pieces, with how long they run
    python tools/visitors.py --recent        the individual recognitions
    python tools/visitors.py --json          the whole lot, machine-readable

Reads the database directly rather than the endpoint, so it works on a box
where the web process is not running — which is the box you are on when you
want to know what happened.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import visitors as visitorlist                        # noqa: E402
from app.settings import settings                              # noqa: E402


def _when(ms: int) -> str:
    if not ms:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


def _table(rows: list[dict], columns: list[tuple[str, str, str]]) -> None:
    """Print rows as aligned columns. `columns` is (key, heading, align)."""
    if not rows:
        print("    (nothing yet)")
        return
    text = [[str(r.get(k, "") if r.get(k) is not None else "-")
             for k, _, _ in columns] for r in rows]
    widths = [max(len(h), *(len(line[i]) for line in text))
              for i, (_, h, _) in enumerate(columns)]

    def render(cells: list[str]) -> str:
        return "  ".join(
            c.rjust(w) if a == "r" else c.ljust(w)
            for c, w, (_, _, a) in zip(cells, widths, columns))

    print("    " + render([h for _, h, _ in columns]))
    print("    " + "  ".join("-" * w for w in widths))
    for line in text:
        print("    " + render(line))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--who", action="store_true", help="addresses")
    p.add_argument("--where", action="store_true", help="by country")
    p.add_argument("--what", action="store_true", help="by piece")
    p.add_argument("--recent", action="store_true", help="individual answers")
    p.add_argument("--json", action="store_true", help="everything, as JSON")
    args = p.parse_args()

    if args.json:
        print(json.dumps(visitorlist.everything(), indent=2))
        return 0

    everything = not (args.who or args.where or args.what or args.recent)
    print(f"\n  {settings.work_dir / 'jobs.sqlite'}")

    geo = visitorlist.geolocation_status()
    if not geo["available"]:
        # Said once, at the top. Every country column below will be empty and
        # this is the only reason worth reading.
        print(f"\n  NO GEOLOCATION: {geo['problem']}")

    if everything or args.who:
        print("\n  WHO — one row per address")
        who = [{**r, "last_seen": _when(r["last_seen"])}
               for r in visitorlist.visitors()]
        _table(who, [
            ("address", "address", "l"),
            ("city", "city", "l"),
            ("region", "region", "l"),
            ("country", "country", "l"),
            ("uploads", "uploads", "r"),
            ("delivered", "done", "r"),
            ("failed", "failed", "r"),
            ("pieces", "pieces", "r"),
            ("minutes", "minutes", "r"),
            ("megabytes", "MB", "r"),
            ("last_seen", "last seen", "l"),
        ])

    if everything or args.where:
        print("\n  WHERE — by country, most video first")
        _table(visitorlist.countries(), [
            ("country", "country", "l"),
            ("uploads", "uploads", "r"),
            ("minutes", "minutes", "r"),
            ("city_count", "cities", "r"),
            ("cities", "where", "l"),
        ])

    if everything or args.what:
        print("\n  WHAT — by piece, most played first")
        _table(visitorlist.pieces(), [
            ("title", "piece", "l"),
            ("times", "asked", "r"),
            ("uploads", "uploads", "r"),
            ("confidence", "conf %", "r"),
            ("median_minutes", "median", "r"),
            ("shortest_minutes", "shortest", "r"),
            ("longest_minutes", "longest", "r"),
        ])

    if everything or args.recent:
        print("\n  RECENT — every answer the recogniser gave")
        rows = [{**r, "time": _when(r["time"])}
                for r in visitorlist.recent(40)]
        _table(rows, [
            ("time", "when", "l"),
            ("job", "job", "l"),
            ("city", "city", "l"),
            ("country", "country", "l"),
            ("outcome", "outcome", "l"),
            ("piece", "piece", "l"),
            ("confidence", "conf %", "r"),
            ("minutes", "minutes", "r"),
        ])

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
