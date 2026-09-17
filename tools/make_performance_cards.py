"""Draw the share card for performances that already have none.

    python tools/make_performance_cards.py            every READY one
    python tools/make_performance_cards.py --check    say what it would do
    python tools/make_performance_cards.py <id>       just this public id
    python tools/make_performance_cards.py --force    redraw existing ones

`app.prepare` draws a card when a performance reaches READY, so anything
prepared from now on arrives with one. This is for the ones that were
already here when that started -- the backfilled library, and anything
prepared before the card existed.

RUN IT WHERE cairosvg IS. Three of the four packages installed ship .svg
bands only, and a host without cairosvg draws the card with a photograph
and no engraving -- which is the one thing the card is for. The web box
has it; `2026liszt` does not.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import sharecard, store, svg as appsvg, viewer, watch  # noqa: E402

logger = logging.getLogger("cards")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("public_id", nargs="?", help="just this one")
    ap.add_argument("--check", action="store_true", help="change nothing")
    ap.add_argument("--force", action="store_true", help="redraw existing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not appsvg.available():
        print("! cairosvg is not installed here, so every card would be drawn")
        print("  without its engraving. Refusing rather than making bad cards.")
        print(" ", appsvg.why_unavailable())
        return 2

    if args.public_id:
        row = store.performance(args.public_id)
        rows = [row] if row is not None else []
        if not rows:
            print(f"no performance {args.public_id}")
            return 1
    else:
        rows = store.performances(watch.READY, limit=500)

    made = skipped = failed = 0
    for row in rows:
        public_id = row["public_id"]
        existing = sharecard.card_path(public_id)
        if existing.is_file() and not args.force:
            skipped += 1
            continue
        payload = viewer.payload(row)
        if not (payload.get("media") or {}).get("external_id"):
            # An upload, not a link: it has no YouTube still to build from.
            skipped += 1
            continue
        if args.check:
            print(f"  would draw  {public_id}  {(row['title'] or '')[:54]}")
            made += 1
            continue
        out = sharecard.build(payload, public_id)
        if out is None:
            print(f"  FAILED      {public_id}  {(row['title'] or '')[:54]}")
            failed += 1
            continue
        print(f"  drew        {public_id}  {out.stat().st_size / 1000:.0f} kB"
              f"  {(row['title'] or '')[:44]}")
        made += 1

    print()
    print(f"{made} drawn, {skipped} already had one or had no video, "
          f"{failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
