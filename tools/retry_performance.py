"""Give a refused performance another go.

    python tools/retry_performance.py <public id>            re-prepare it
    python tools/retry_performance.py <public id> --check    say only

A row with an edition resumes at the alignment; one refused before any
piece was named is recognised again.

REJECTED is final in the state model, on purpose: discovery must stop
offering the same dead video back for ever. But a refusal can be OUR fault
-- a score package that names more measures than the music has made the
aligner call a complete Nocturne "part of the piece" -- and then the row is
dead with nothing to bring it back. This is that door, for an operator, on
the web box.

The row goes back to REVIEW with its edition kept, so the resume path takes
it: the volunteer still has the audio, recognition is skipped, only the
alignment runs. The reason and error are cleared so the page stops saying
what is no longer true.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import store, watch  # noqa: E402
from app.queue import watchledger  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("public_id")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    row = store.performance(args.public_id)
    if row is None:
        print(f"no performance {args.public_id}")
        return 1
    print(f"  {row['public_id']}  {row['state']}  {row['skip_reason'] or '-'}")
    print(f"  {row['title'] or ''}")
    print(f"  edition: {row['edition'] or '(none)'}")
    print(f"  error:   {(row['error'] or '')[:100]}")
    if row["state"] not in (watch.REJECTED, watch.FAILED, watch.UNAVAILABLE, watch.REVIEW):
        print(f"  {row['state']} is not a refusal; nothing to retry")
        return 1
    if not row["edition"]:
        # Refused before any piece was named (NOT_CHOPIN): the only way back
        # is to listen again. The audio is still cached on the volunteer.
        if args.check:
            print("  would set DISCOVERED and offer it for a fresh identification")
            return 0
        store.set_performance(row["id"], state=watch.DISCOVERED,
                              skip_reason=None, error=None)
        ok = watchledger.offer(row["id"])
        print("  set DISCOVERED and offered for a fresh identification" if ok
              else "  set DISCOVERED; the sweep will offer it")
        return 0
    if args.check:
        print("  would set REVIEW and offer it to resume as", row["edition"])
        return 0

    # Direct, not through watch.advance: REJECTED -> REVIEW is deliberately
    # not a move the model allows, and this is the one operator action that
    # overrides it.
    store.set_performance(row["id"], state=watch.REVIEW,
                          skip_reason=watch.NOT_ENGRAVED, error=None)
    ok = watchledger.offer(row["id"], resume_edition=row["edition"])
    print("  set REVIEW and offered to resume" if ok
          else "  set REVIEW; the sweep will offer it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
