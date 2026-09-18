"""Everything the watch page needs about one performance.

The page shows a video with the score beside it, following the playing bar
by bar. To do that it needs three things joined: which recording to play,
when each bar of it sounds, and where that bar sits across its system.

They come from three different places on purpose. The recording is a media
source. The timings are the alignment, held per bar so a click on a bar is
a seek and not a guess. The geometry is read off the engraving itself --
the package never stated it -- and cached per edition, because parsing a
third of a megabyte of SVG on a page load would be absurd and the answer
never changes for a given engraving.

Nothing here writes score bytes to this disk: bands are fetched from the
bucket, measured, and the numbers kept.
"""
from __future__ import annotations

import logging
import pathlib

from . import bars as barmod
from . import package as pkg
from urllib.parse import quote

from . import library, scorestore, store

logger = logging.getLogger(__name__)


def geometry(edition: str) -> list[dict]:
    """Where every bar of an edition sits, measuring it once if need be.

    Returns [{measure, band, x0, x1}] in bar order. Empty when the score
    is not installed here and the bucket has nothing for it, which is not
    an error: most of the library is not engraved yet.
    """
    cached = store.score_bars(edition)
    if cached:
        return [{"measure": r["measure"], "band": r["band"], "x0": r["x0"],
                 "x1": r["x1"], "aspect": r["aspect"]} for r in cached]

    starts = _band_starts(edition)
    if not starts:
        return []

    # THE EXTRACTOR'S OWN BOXES, where the package folder is on this disk;
    # barline detection only where it is not. Keyed by sync key either way
    # -- detection numbers bars from the band's first measure, which equals
    # the key only when the piece has no pickup and no split cadenza, so a
    # fallback there is logged as the approximation it is.
    root = _root(edition)
    rows: list[tuple] = []
    for first in starts:
        data = _band_bytes(edition, first)
        if data is None:
            logger.warning("%s: no band for measure %s in the bucket",
                           edition, first)
            continue
        try:
            aspect = barmod.band_aspect(data)
            bars = barmod.bars_from_project(root, first) if root else None
            if bars is None:
                bars = barmod.bars_from(data, first, name=f"{edition}/{first}")
                if root is None:
                    logger.info("%s: no project files here; bars of band %s "
                                "numbered from its barlines", edition, first)
            for bar in bars:
                rows.append((bar.measure, first, bar.x0, bar.x1, aspect))
        except barmod.BandGeometryError:
            # One unreadable system must not cost the whole score: the rest
            # still follows, and the bars on this one simply cannot be
            # clicked. Loud in the log, silent on the page.
            logger.warning("%s: could not measure the band at %s",
                           edition, first, exc_info=True)

    if rows:
        store.put_score_bars(edition, rows)
        logger.info("measured %d bars across %d systems of %s",
                    len(rows), len(starts), edition)
    return [{"measure": m, "band": b, "x0": x0, "x1": x1, "aspect": a}
            for m, b, x0, x1, a in rows]


def _root(edition: str) -> "pathlib.Path | None":
    """The package folder on this disk, by exact name, or None."""
    from . import pipeline                                  # noqa: PLC0415
    found = pipeline.find_package(edition)
    return found.root if found is not None and found.name == edition else None


def labels_for(edition: str) -> dict[int, int]:
    """Sync key -> the source bar a person reads. Empty when unknown."""
    root = _root(edition)
    try:
        if root is not None and (root / "reference" / "measures.data").is_file():
            return pkg.sync_key_to_bar(root / "reference" / "measures.data")
        raw = scorestore.preview_bytes(edition, scorestore.ALIGNMENT)
        return pkg.sync_key_to_bar(raw) if raw else {}
    except Exception:                                       # noqa: BLE001
        logger.warning("%s: could not read the bar labels", edition, exc_info=True)
        return {}


def _band_starts(edition: str) -> list[int]:
    """The measure each system begins at, in order."""
    package = library.find(edition)
    if package is not None and package.bands:
        return [b.first_measure for b in package.bands]
    return []


def _band_bytes(edition: str, first: int) -> bytes | None:
    """One system, from this disk if it is here and the bucket if it is not.

    The two library shapes differ: a package loaded from a folder knows
    where each band file is, while the catalogue a web box reads knows only
    which measures exist -- that host has no score bytes at all. Asking the
    second for a `.path` is how this broke the first time.
    """
    package = library.find(edition)
    for band in getattr(package, "bands", None) or []:
        path = getattr(band, "path", None)
        if band.first_measure == first and path is not None and path.is_file():
            return path.read_bytes()
    return scorestore.preview_bytes(edition, scorestore.band_name(first))


def payload(performance) -> dict:
    """The performance, as the page reads it.

    Bars carry their time and their box together: the page highlights by
    time and seeks by box, and a bar missing either is one the reader can
    see but not use.
    """
    edition = performance["edition"] or ""
    sync = store.latest_sync(performance["id"])
    timings = dict(store.sync_timeline(sync["id"])) if sync else {}
    boxes = {g["measure"]: g for g in geometry(edition)} if edition else {}

    # Grouped by system, because that is how the page draws it: one picture
    # with the bars laid over it as percentages of its width.
    # DISPLAY IS THE SOURCE NUMBER, SYNC IS THE SYNC KEY. Timings and boxes
    # meet on the key; the label a person reads is the source bar, and a
    # box no source bar owns -- a cadenza's extra boxes -- is labelled with
    # the bar before it, which is the bar the music is in.
    labels = labels_for(edition) if edition else {}
    by_band: dict[int, list[dict]] = {}
    aspects: dict[int, float] = {}
    shown = None
    for measure in sorted(set(timings) & set(boxes)):
        box = boxes[measure]
        shown = labels.get(measure, measure if not labels else shown)
        if shown is None:
            shown = measure
        by_band.setdefault(box["band"], []).append({
            "m": shown,
            "k": measure,
            # SECONDS, because the player reports seconds. The database keeps
            # milliseconds; the conversion happens once, here.
            "t": round(timings[measure] / 1000.0, 3),
            "a": round(box["x0"] * 100, 3),
            "b": round(box["x1"] * 100, 3),
        })
        aspects[box["band"]] = box.get("aspect") or 0

    bands = []
    for first in sorted(by_band):
        bands.append({
            "first": first,
            "aspect": round(aspects.get(first) or 5.0, 4),
            "img": f"/api/library/{quote(edition)}/band/{first}",
            "bars": by_band[first],
        })

    package = library.find(edition) if edition else None
    last = max((b["t"] for band in bands for b in band["bars"]), default=0.0)
    media = store.media_for(performance["id"])
    primary = media[0] if media else None
    return {
        "id": performance["public_id"],
        "state": performance["state"],
        "title": performance["title"] or (package.title if package else ""),
        "performer": performance["performer"],
        "edition": edition,
        "bands": bands,
        "end": last,
        "total_bars": package.last_measure if package else None,
        "media": {
            "provider": primary["provider"] if primary else None,
            "external_id": primary["external_id"] if primary else "",
            "url": primary["url"] if primary else "",
        },
        # Said plainly, because the page must never imply it can follow
        # something it cannot.
        "followable": any(band["bars"] for band in bands),
        "skip_reason": performance["skip_reason"],
    }
