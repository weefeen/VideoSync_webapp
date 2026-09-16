"""Turn the scores installed here into performances you can watch.

Every renderable package already carries an alignment -- the reference
recording it was prepared against -- so the library has performances in it
before anybody pastes a single link. This makes them reachable: one
performance per installed edition, its media source the recording the
package was built from, its timings the package's own measures file.

Idempotent. Run it again after installing a score and only the new one is
added; run it twice and nothing is duplicated.

    python tools/backfill_performances.py            # do it
    python tools/backfill_performances.py --check    # say what would happen
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import library, scorestore, store, viewer, watch   # noqa: E402
from app import package as pkg                     # noqa: E402


def backfill(dry: bool = False) -> int:
    made = 0
    for package in library.packages():
        name = package.name
        if not package.bands:
            print(f"  skip   {name}: no engraved systems")
            continue
        timeline = _timeline(package)
        if not timeline:
            print(f"  skip   {name}: no alignment published for it")
            continue

        existing = _already(name)
        if existing is not None:
            print(f"  have   {name}: /p/{existing['public_id']}")
            continue
        if dry:
            print(f"  would  {name}: {len(timeline)} bars")
            made += 1
            continue

        performance = store.new_performance(
            watch.DISCOVERED,
            edition=name,
            title=package.title or package.display_name,
            priority=watch.PRIORITY[watch.ADMIN])
        # The recording the package was prepared against. It is not a
        # YouTube video and has no external id, so it cannot collide with
        # one: the partial unique index only covers rows that have one.
        store.attach_media(performance["id"], "reference",
                           storage_uri=f"{name}/{scorestore.ALIGNMENT}")
        media = store.media_for(performance["id"])[0]
        store.add_discovery(media["id"], source_type=watch.ADMIN,
                            reference="installed package",
                            priority=watch.PRIORITY[watch.ADMIN])

        # Straight to READY: this alignment was made and checked long before
        # the web application existed, so validating and identifying it
        # again would be theatre. The states it passes through are recorded
        # rather than skipped, because the log is how anybody later works
        # out where a performance came from.
        for step in (watch.VALIDATING, watch.IDENTIFYING, watch.READY_FOR_SYNC,
                     watch.SYNCHRONISING):
            watch.advance(performance["id"], step)
        store.put_sync(performance["id"], name, state="READY", method="package",
                       timeline=timeline)
        watch.advance(performance["id"], watch.QC)
        watch.advance(performance["id"], watch.READY)

        # Measure the engraving now rather than on somebody's page load.
        geometry = viewer.geometry(name)
        row = store.performance(performance["public_id"])
        print(f"  made   {name}: /p/{row['public_id']}  "
              f"{len(timeline)} bars, {len(geometry)} measured")
        made += 1
    return made


def _timeline(package) -> list:
    """The package's own alignment, from wherever this host can reach it.

    A box with the score on disk reads the file; a box that keeps no score
    bytes reads the copy published beside the bands. Same parser either
    way, because two copies of that parser is how a measure number got
    read as a timestamp once already.
    """
    local = getattr(package, "timeline", None)
    if local:
        return local
    raw = scorestore.preview_bytes(package.name, scorestore.ALIGNMENT)
    if raw is None:
        return []
    try:
        return pkg.read_measures_text(raw.decode("utf-8"),
                                      name=f"{package.name}/{scorestore.ALIGNMENT}")
    except pkg.PackageError as exc:
        print(f"  skip   {package.name}: {exc}")
        return []


def _already(edition: str):
    for row in store.performances(limit=1000):
        if row["edition"] == edition:
            return row
    return None


if __name__ == "__main__":
    dry = "--check" in sys.argv
    print("what the library would gain:" if dry else "backfilling:")
    count = backfill(dry)
    print(f"{count} performance(s) {'to add' if dry else 'added'}")
