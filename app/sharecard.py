"""The card one performance shows when its link is shared.

THE SAME CARD THE SITE ALREADY HAS, with this performance's words in it.
`tools/make_share_card.draw` is the renderer -- the photograph, the scrim
that ramps rather than boxes, the engraving along the foot, the magenta
rule -- and it is called here rather than reimplemented, so the two cards
cannot drift into looking like two products.

WHAT CHANGES PER PERFORMANCE:

    the photograph   YouTube's still of that video, largest one they have
    the engraving    that piece's own first system, from the score package
    the headline     the piece
    the note         the pianist, where the magenta rule used to say "Free"

WHY THE ENGRAVING COMES FROM THE PACKAGE AND NOT THE PICTURE. The site
card cuts its band out of a frame of a video we rendered, which already
has the engraving burnt into it. A performance here is somebody else's
video with no band in it at all, so the band has to come from the score --
which is better anyway: `draw` prefers a vector-derived band when given
one, because a band rasterised above target size and reduced stays crisp
at feed size where a re-encoded one turns grey.
"""
from __future__ import annotations

import json
import logging
import pathlib
import urllib.parse
import urllib.request

from . import library, scorestore, svg as appsvg, viewer
from .settings import settings

logger = logging.getLogger(__name__)

#: Largest first. YouTube serves 404 for a size a video does not have
#: rather than falling back, so they are tried in order and the first that
#: answers is used. maxres exists on most modern uploads and is 1280x720;
#: hq always exists but is 480x360, which is soft when blown up to a
#: 1200-wide card -- worth the extra request to avoid.
_STILLS = ("maxresdefault", "sddefault", "hqdefault", "mqdefault")

#: Rasterised well above the 1200px the card is wide, then reduced by the
#: renderer. That reduction is what makes notation legible in a feed.
_BAND_WIDTH = 2600


def youtube_meta(vid: str) -> dict:
    """What YouTube calls this video: its title and the channel.

    oEmbed, which is YouTube's own published endpoint for exactly this, and
    it wants no API key, no quota and no yt-dlp. That matters here: the web
    box has none of those, and a card should not depend on the machinery
    that downloads audio.

    WHY THE YOUTUBE TITLE AND NOT OURS. A performance backfilled from the
    library is titled after the score package -- "3eme Scherzo pour le
    Piano" -- which names the work and nobody who played it. YouTube's
    title is almost always "Pianist - Work", which is both facts in one
    string and is what somebody scrolling a feed needs to see. The score
    package cannot know who sat at the piano; the video always does.

    Returns {} on any failure. The card falls back to what we already knew.
    """
    if not vid:
        return {}
    watch = urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="")
    url = f"https://www.youtube.com/oembed?url={watch}&format=json"
    try:
        with urllib.request.urlopen(url, timeout=20) as answer:
            if answer.status != 200:
                return {}
            got = json.loads(answer.read().decode("utf-8"))
    except Exception:                                # noqa: BLE001
        logger.info("%s: oEmbed did not answer", vid)
        return {}
    return {"title": (got.get("title") or "").strip(),
            "author": (got.get("author_name") or "").strip()}


def cache_dir() -> pathlib.Path:
    return settings.cache_dir / "cards"


def card_path(public_id: str) -> pathlib.Path:
    return cache_dir() / f"{public_id}.png"


def build(payload: dict, public_id: str) -> "pathlib.Path | None":
    """Draw the card for one performance, or None if it cannot be drawn.

    Never raises. A share card is a nicety: if the still will not download
    or the engraving cannot be rasterised here, the page falls back to
    YouTube's thumbnail as the og:image and nobody sees an error.
    """
    from tools.make_share_card import draw          # noqa: PLC0415

    vid = ((payload.get("media") or {}).get("external_id") or "").strip()
    if not vid:
        return None
    out = card_path(public_id)
    out.parent.mkdir(parents=True, exist_ok=True)

    still = _still(vid, out.parent / f"{public_id}-still.jpg")
    if still is None:
        return None
    band = _band(payload.get("edition") or "", out.parent / f"{public_id}-band.png")

    # YouTube's own words first, ours only if it will not answer.
    meta = youtube_meta(vid)
    said = dict(payload)
    if meta.get("title"):
        said["title"] = meta["title"]
    if meta.get("author") and not said.get("performer"):
        said["performer"] = meta["author"]

    try:
        draw(still, out, band_png=band,
             headline=_headline(said),
             kicker="CHOPIN.WEEFEEN.COM",
             note=_note(said))
    except Exception:                                # noqa: BLE001
        logger.warning("%s: could not draw the share card", public_id,
                       exc_info=True)
        return None
    finally:
        for scrap in (still, band):
            if scrap is not None and scrap.is_file():
                scrap.unlink(missing_ok=True)
    return out if out.is_file() else None


def _still(vid: str, into: pathlib.Path) -> "pathlib.Path | None":
    """YouTube's own frame of this video, the largest one they hold."""
    for size in _STILLS:
        url = f"https://i.ytimg.com/vi/{vid}/{size}.jpg"
        try:
            with urllib.request.urlopen(url, timeout=25) as answer:
                if answer.status != 200:
                    continue
                data = answer.read()
        except Exception:                            # noqa: BLE001
            continue
        # A 404 page and a 1x1 placeholder both arrive as bytes; a real
        # still is never this small.
        if len(data) < 2000:
            continue
        into.write_bytes(data)
        logger.info("%s: card still from %s (%.0f kB)", vid, size, len(data) / 1000)
        return into
    return None


def _band(edition: str, into: pathlib.Path) -> "pathlib.Path | None":
    """The piece's first system, rasterised big so it reduces cleanly."""
    if not edition:
        return None
    package = library.find(edition)
    firsts = [b.first_measure for b in (getattr(package, "bands", None) or [])]
    if not firsts:
        return None
    first = min(firsts)

    raw = viewer._band_bytes(edition, first)         # noqa: SLF001
    if raw is None:
        return None
    if raw[:4] == b"\x89PNG" or raw[:2] == b"\xff\xd8":
        into.write_bytes(raw)                        # already a raster
        return into
    if not appsvg.available():
        # The band is SVG and this host cannot rasterise it. The renderer
        # copes: given no band it keeps the one in the photograph, which
        # for a YouTube performance means no band at all -- still a card,
        # just without notation. Better than no card.
        logger.info("%s: no cairosvg here, card goes without the engraving",
                    edition)
        return None
    # `rasterize` takes a PATH and RETURNS a PIL image; its fourth argument
    # is the background COLOUR, not an output file. Passing the destination
    # there reached cairosvg as a colour name and it failed on a
    # WindowsPath having no .strip() -- a card that silently lost its
    # engraving, which is the one thing the card is for.
    tmp = into.with_suffix(".svg")
    try:
        tmp.write_bytes(raw)
        height = max(1, round(_BAND_WIDTH / 5.77))
        # PAPER, not the default white. The site card's band is cut from a
        # video frame and carries the paper colour with it; a band rasterised
        # onto white sits in the same layout looking like a different
        # material, which is exactly how two cards stop reading as one
        # product.
        appsvg.rasterize(tmp, _BAND_WIDTH, height,
                         background="#f6f1e8").save(into)
    except Exception:                                # noqa: BLE001
        logger.warning("%s: could not rasterise the band for the card",
                       edition, exc_info=True)
        return None
    finally:
        tmp.unlink(missing_ok=True)
    return into if into.is_file() else None


def _headline(payload: dict) -> str:
    """The piece, over at most three lines.

    Wrapped on words rather than measured, because the renderer draws each
    line at a fixed size and a long title has to be allowed to be long --
    the alternative is shrinking type that then does not match the site
    card beside it in a feed.
    """
    title = (payload.get("title") or "").strip() or "Chopin"
    title = title.replace("—", "-").replace("–", "-")
    # A YouTube title is usually "Pianist - Work (occasion)"; the work is
    # what the card is about and the pianist has the rule below it.
    for sep in (" - ", " – ", " — ", " | "):
        if sep in title:
            title = title.split(sep, 1)[1]
            break
    # What a channel appends after a pipe is its own series, not the work.
    title = title.split(" | ")[0]
    # And "Chopin:" in front of it is the one composer this whole site is,
    # so it is the least informative word the card could spend a line on.
    for prefix in ("Chopin:", "Chopin -", "Chopin,", "F. Chopin:", "Frederic Chopin:"):
        if title.strip().lower().startswith(prefix.lower()):
            title = title.strip()[len(prefix):]
            break
    title = title.split(" (")[0].strip().strip(",").strip() or "Chopin"

    words, lines, line = title.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if len(trial) > 20 and line:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    if len(lines) > 3:
        lines = lines[:3]
        lines[-1] = lines[-1].rstrip(",") + "…"
    return "\n".join(lines)


def _note(payload: dict) -> str:
    """What sits beside the magenta rule: the PIANIST.

    On the site card this slot says "Free". On a performance the person
    playing is the fact worth the one piece of colour, and it is not the
    same as `performer` -- that field holds the YouTube uploader, which for
    this library is usually a channel. "Chopin Institute" under a portrait
    of Kate Liu is the wrong name on the card, so the title is asked first:
    these are almost always "Pianist - Work (occasion)", and the half
    `_headline` throws away is exactly the half wanted here.
    """
    pianist = _pianist(payload.get("title") or "")
    if pianist:
        return pianist
    who = (payload.get("performer") or "").strip()
    if who:
        return who
    bars = payload.get("total_bars")
    return f"{bars} bars, followed" if bars else "With the score"


def _pianist(title: str) -> str:
    """The name in front of the dash, when there plainly is one.

    Guarded rather than trusting the shape: a title with no separator, or
    whose first half is long enough to be a work rather than a person, is
    left alone. A wrong name on the card is worse than no name.
    """
    for sep in (" - ", " – ", " — ", " | "):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if 2 <= len(head) <= 34 and not any(
                    w in head.lower() for w in
                    ("op.", "op ", "no.", "scherzo", "ballade", "nocturne",
                     "etude", "étude", "waltz", "valse", "prelude",
                     "sonata", "mazurka", "polonaise", "chopin")):
                return head
    return ""
