"""Put finished videos made before the bucket existed into the bucket.

    python tools/backfill_outputs.py            say what would happen
    python tools/backfill_outputs.py --do       do it

Every job finished before object storage was wired up has its video only on
local disk. That is the state this exists to end: a video that exists in one
place is a video one disk failure away from gone, and once the renderer
moves to a host that is destroyed after each job, "one place" becomes "a
place that is about to be deleted".

Idempotent, and safe to stop half way. A job already carrying an object_key
is skipped, and the upload is confirmed with a HEAD before the row is
written — so a row that says the video is in the bucket means it is.

The local file is NOT deleted. Reclaiming that space is a separate decision
from making the copy, and doing both at once means a mistake in either takes
the only copy with it.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import storage, store                                  # noqa: E402
from app.settings import settings                               # noqa: E402


def _find(job: str, recorded: str | None) -> pathlib.Path | None:
    """The video, wherever it actually is.

    The recorded path is where it was written, and WORK_DIR has moved since:
    five rows here still name /srv/vsw/work, which no longer exists, while
    the files sit under the current work directory. Trusting the row alone
    would silently skip exactly the oldest videos — the ones that have been
    unprotected longest.
    """
    if recorded:
        p = pathlib.Path(recorded)
        if p.is_file():
            return p
        # Same file name, current work directory.
        here = settings.work_dir / job / p.name
        if here.is_file():
            return here
    # Last resort: any rendered output in this job's directory.
    folder = settings.work_dir / job
    if folder.is_dir():
        for candidate in sorted(folder.glob("*_synced.*")):
            if candidate.is_file():
                return candidate
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--do", action="store_true",
                   help="actually upload; without it, only report")
    args = p.parse_args()

    status = storage.status()
    if not status["available"]:
        print(f"  no bucket: {status['problem']}")
        return 1
    print(f"  bucket: {status['bucket']}\n")

    rows = store.query(
        "SELECT id, result, object_key, finished, size_bytes FROM jobs"
        " WHERE state = ? ORDER BY finished", (store.DONE,))

    done = skipped = missing = failed = 0
    moved = 0
    for row in rows:
        job = row["id"]
        if row["object_key"]:
            skipped += 1
            continue
        local = _find(job, row["result"])
        if local is None:
            print(f"  {job}  no file anywhere (row says {row['result']})")
            missing += 1
            continue

        key = storage.output_key(job, local.suffix or ".mp4")
        size = local.stat().st_size
        if not args.do:
            print(f"  {job}  would upload {size / 1e6:6.1f} MB -> {key}")
            done += 1
            continue

        try:
            # Already there at the right size? Then only the row is behind,
            # and re-uploading 125 MB to learn that would be wasteful.
            got = storage.head(key)
            if got == size:
                print(f"  {job}  already in the bucket; recording it")
            else:
                got = storage.put(local, key)
                print(f"  {job}  uploaded {got / 1e6:6.1f} MB -> {key}")
            store.update_job(job, object_key=key)
            done += 1
            moved += got
        except storage.StorageError as exc:
            print(f"  {job}  FAILED: {exc}")
            failed += 1

    print()
    verb = "would copy" if not args.do else "copied"
    print(f"  {verb} {done}, already done {skipped}, "
          f"no file {missing}, failed {failed}")
    if args.do and moved:
        print(f"  {moved / 1e6:.1f} MB now has a second copy")
    if not args.do and done:
        print("\n  nothing was uploaded. Run again with --do.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
