"""Release a request that could not be rendered when it was made.

Two kinds of job end up here.

A HELD job was asked for when no score was installed for the piece. The
recording, the address and the score name the visitor chose were all kept,
so once the package is on the server there is nothing missing — the job just
has to be put back in the queue.

A FAILED job ran and did not finish. Its score may have been wrong, the
engraving may have had a bad band, or ffmpeg may have died. Releasing it
starts a fresh attempt against whatever is on disk now.

Nothing releases either kind on its own, deliberately. A held job becomes
renderable the moment a folder appears in the score root, and a render costs
real minutes on a machine somebody is paying for; the person who built the
package is the one who knows whether it is finished, so the trigger is his.

Usage:
    python tools/retrigger.py                 # what is waiting, and why
    python tools/retrigger.py <job_id>        # release that one
    python tools/retrigger.py --held          # every held job that can now run
    python tools/retrigger.py --failed        # every failed job, again

This writes the job row and stops there. It never talks to the broker: the
web process sweeps for queued work that was never handed over and offers it
within about half a minute. That is the same path a lost handover already
takes, so there is one recovery route rather than two, and this works
whether the queue is RabbitMQ or in-process.

The web application does NOT need restarting. It does need to be running,
or nothing will pick the job up until it is.
"""

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import pipeline                        # noqa: E402
from app import store                           # noqa: E402

WIDTH = 78


def _age(seconds: float | None) -> str:
    if not seconds:
        return "?"
    delta = max(0.0, time.time() - seconds)
    if delta < 3600:
        return f"{delta / 60:.0f} min"
    if delta < 86400:
        return f"{delta / 3600:.0f} h"
    return f"{delta / 86400:.0f} d"


def _installed(score: str | None) -> bool:
    """Is there a package this job could run against right now?"""
    return bool(score) and pipeline.find_package(score) is not None


def show() -> int:
    """List what is waiting. Returns the number of releasable jobs."""
    held = store.held()
    failed = store.query(
        "SELECT * FROM jobs WHERE state = ? ORDER BY finished DESC LIMIT 20",
        (store.ERROR,))

    ready = 0

    print()
    print("HELD - asked for before the score existed")
    print("-" * WIDTH)
    if not held:
        print("  nothing held")
    for row in held:
        can = _installed(row["score"])
        ready += can
        mark = "READY" if can else "no score yet"
        print(f"  {row['id']}  waited {_age(row['created']):>7}  [{mark}]")
        print(f"      wants: {row['score']}")

    print()
    print("FAILED - ran and did not finish")
    print("-" * WIDTH)
    if not failed:
        print("  nothing failed")
    for row in failed:
        print(f"  {row['id']}  {_age(row['finished']):>7} ago  "
              f"attempt {row['attempt']}")
        print(f"      score: {row['score']}")
        if row["error"]:
            print(f"      error: {str(row['error'])[:60]}")

    print()
    if ready:
        print(f"{ready} held job(s) can run now:  "
              f"python tools/retrigger.py --held")
    print()
    return ready


def release(row, *, force: bool = False) -> bool:
    """Put one job back in the queue. True if it was released.

    The mode is deliberately not carried over from the held row — it was
    never chosen, because choosing it needs the package and the package did
    not exist. `pipeline.choose_mode` settles it here against the real
    thing.
    """
    job_id = row["id"]
    score = row["score"]
    package = pipeline.find_package(score) if score else None

    if package is None:
        if not force:
            print(f"  {job_id}: no package named {score!r} - not released")
            return False
        print(f"  {job_id}: no package named {score!r} - releasing anyway, "
              f"it will fail")

    mode = row["mode"]
    if package is not None:
        try:
            mode = pipeline.choose_mode(package, None)
        except Exception as exc:                       # noqa: BLE001
            print(f"  {job_id}: {package.name} cannot be rendered - {exc}")
            return False

    store.update_job(
        job_id,
        state=store.QUEUED,
        mode=mode,
        # A fresh attempt, so a message still in flight from the previous run
        # cannot be mistaken for this one's and overwrite what it produces.
        attempt=(row["attempt"] or 1) + 1,
        queued_at=time.time(),
        # NULL is what tells the sweep this work was never handed over. It is
        # the whole mechanism by which this script reaches the worker.
        published_at=None,
        started=None, finished=None,
        error=None, result=None,
        stage=None, detail="")
    print(f"  {job_id}: queued against {score}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?", help="the job to release")
    parser.add_argument("--held", action="store_true",
                        help="release every held job whose score is installed")
    parser.add_argument("--failed", action="store_true",
                        help="try every failed job again")
    parser.add_argument("--force", action="store_true",
                        help="release even with no package, so it fails "
                             "visibly rather than sitting held")
    args = parser.parse_args()

    if not (args.job_id or args.held or args.failed):
        show()
        return 0

    rows = []
    if args.job_id:
        row = store.get_job(args.job_id)
        if row is None:
            print(f"no job {args.job_id!r}")
            return 1
        if row["state"] not in (store.HELD, store.ERROR, store.DONE):
            print(f"job {args.job_id} is {row['state']}; only held, failed "
                  f"or finished jobs can be released")
            return 1
        rows = [row]
    else:
        if args.held:
            rows += list(store.held())
        if args.failed:
            rows += list(store.query(
                "SELECT * FROM jobs WHERE state = ? ORDER BY finished",
                (store.ERROR,)))

    if not rows:
        print("nothing to release")
        return 0

    print()
    released = sum(release(r, force=args.force) for r in rows)
    print()
    print(f"{released} of {len(rows)} released. The web application picks "
          f"them up within about half a minute.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
