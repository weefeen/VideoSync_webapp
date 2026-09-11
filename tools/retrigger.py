"""Start a render on a visitor's behalf, or run a failed one again.

A request for a piece with no score installed is refused, and that is the
end of it: nothing is parked and nothing waits. What survives is the trace -
the recording on disk under its job id, the recognition in its table, and
the mail this sent you, which carries the job id, the folder name to build
and the visitor's address.

So once the score is engraved and on the server, one command finishes the
job somebody asked for days ago:

    python tools/retrigger.py <job_id> --score "<folder name>" --email <addr>

The address is typed in from that mail because the refused request never
stored it. Leave `--email` off and the video is made but nobody is told.

The other use is a render that ran and failed - a bad band in the engraving,
a recording over the memory cap, ffmpeg dying. Its address is on the row
already, so it needs nothing but the job id.

Usage:
    python tools/retrigger.py                       # what failed, and what
                                                    # was asked for and refused
    python tools/retrigger.py <job_id> --score S --email A
    python tools/retrigger.py <job_id>              # run a failed job again
    python tools/retrigger.py --failed              # every failed job again

Nothing here runs on its own. A render costs real minutes on a machine
somebody is paying for, and the person who built the package is the one who
knows whether it is finished.

This writes the job row and stops there. It never talks to the broker: the
web process already sweeps for queued work that was never handed over and
offers it within about half a minute. That is the same path a lost handover
takes, so there is one recovery route rather than two, and it works whether
the queue is RabbitMQ or in-process.

The web application does NOT need restarting. It does need to be running, or
nothing will pick the job up until it is.
"""

import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import notify                            # noqa: E402
from app import pipeline                          # noqa: E402
from app import store                             # noqa: E402

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


def _refused() -> list:
    """Uploads that were named and had no score, newest first.

    Reconstructed from `recognitions`, because the request itself left no row
    of its own - that is what "it died" means. `outcome = 'unavailable'` is
    exactly "we named the piece and have no score for it".
    """
    return store.query(
        "SELECT r.job_id, r.at, r.title, r.candidates, r.duration,"
        "       j.upload, j.state"
        "  FROM recognitions r JOIN jobs j ON j.id = r.job_id"
        " WHERE r.outcome = 'unavailable'"
        " ORDER BY r.at DESC LIMIT 30")


def _editions(candidates_json: str | None) -> list[str]:
    """The folder names that recording backs, in the order offered."""
    try:
        candidates = json.loads(candidates_json or "[]")
    except (TypeError, ValueError):
        return []
    names: list[str] = []
    for candidate in candidates:
        for name in candidate.get("editions") or []:
            if name and name not in names:
                names.append(name)
    return names


def show() -> None:
    """What failed, and what was asked for and refused."""
    failed = store.query(
        "SELECT * FROM jobs WHERE state = ? ORDER BY finished DESC LIMIT 20",
        (store.ERROR,))

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
    print("REFUSED - named the piece, no score installed")
    print("-" * WIDTH)
    refused = _refused()
    if not refused:
        print("  nothing refused")
    for row in refused:
        names = _editions(row["candidates"])
        wanted = names[0] if names else "(no edition offered)"
        have = pipeline.find_package(wanted) is not None if names else False
        gone = not pathlib.Path(row["upload"] or "").is_file()
        mark = "SCORE IS NOW INSTALLED" if have else "no score"
        if gone:
            mark = "recording gone"
        minutes = f"{row['duration'] / 60:.1f} min" if row["duration"] else "?"
        print(f"  {row['job_id']}  {_age(row['at']):>7} ago  "
              f"{minutes:>9}  [{mark}]")
        print(f"      {row['title'] or ''}")
        print(f"      wants: {wanted}")
        if have and not gone:
            print(f"      python tools/retrigger.py {row['job_id']} "
                  f'--score "{wanted}" --email <from the mail>')

    print()
    print("An address is not stored for a refused request. It is in the mail")
    print("that reported it, and is typed back in with --email.")
    print()


def start(row, score: str | None, address: str | None) -> bool:
    """Queue one job. True if it was queued."""
    job_id = row["id"]
    score = score or row["score"]
    if not score:
        print(f"  {job_id}: no score given and none on the row - use --score")
        return False

    upload = pathlib.Path(row["upload"] or "")
    if not upload.is_file():
        print(f"  {job_id}: the recording is gone from {upload}")
        return False

    package = pipeline.find_package(score)
    if package is None:
        print(f"  {job_id}: no package named {score!r}. Build it first, or "
              f"check the name character for character - the lookup is exact.")
        return False

    try:
        mode = pipeline.choose_mode(package, None)
    except Exception as exc:                           # noqa: BLE001
        print(f"  {job_id}: {package.name} cannot be rendered - {exc}")
        return False

    fields = dict(
        state=store.QUEUED,
        score=package.name,
        mode=mode,
        # A fresh attempt, so a message still in flight from a previous run
        # cannot be mistaken for this one's and overwrite what it produces.
        attempt=(row["attempt"] or 1) + 1,
        queued_at=time.time(),
        # NULL is what tells the sweep this work was never handed over, and
        # is the whole mechanism by which this script reaches the worker.
        published_at=None,
        started=None, finished=None,
        error=None, result=None,
        stage=None, detail="")

    if address:
        try:
            fields["email"] = notify.one_address(address)
        except notify.MailError:
            print(f"  {job_id}: {address!r} does not look like one address")
            return False

    store.update_job(job_id, **fields)
    told = fields.get("email") or row["email"]
    print(f"  {job_id}: queued against {package.name}")
    print(f"      {'mails ' + told if told else 'NOBODY WILL BE TOLD - no address'}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start a render on a visitor's behalf, or run a failed "
                    "one again.")
    parser.add_argument("job_id", nargs="?", help="the job to render")
    parser.add_argument("--score", help="the package folder name to render "
                                        "against. Needed for a refused "
                                        "request, which never stored one.")
    parser.add_argument("--email", help="where to send the finished video. "
                                        "Typed in from the mail that "
                                        "reported the request.")
    parser.add_argument("--failed", action="store_true",
                        help="run every failed job again")
    args = parser.parse_args()

    if not (args.job_id or args.failed):
        show()
        return 0

    rows = []
    if args.job_id:
        row = store.get_job(args.job_id)
        if row is None:
            print(f"no job {args.job_id!r}")
            return 1
        if row["state"] in store.LIVE:
            print(f"job {args.job_id} is already {row['state']}")
            return 1
        rows = [row]
    elif args.failed:
        rows = list(store.query(
            "SELECT * FROM jobs WHERE state = ? ORDER BY finished",
            (store.ERROR,)))

    if not rows:
        print("nothing to do")
        return 0

    print()
    queued = sum(start(r, args.score, args.email) for r in rows)
    print()
    print(f"{queued} of {len(rows)} queued. The web application picks them "
          f"up within about half a minute.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
