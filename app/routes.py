"""HTTP surface for the score-sync web app."""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import pathlib
import re
import threading
from urllib.parse import quote

from flask import (Blueprint, Flask, current_app, jsonify, redirect,
                   request, send_file, send_from_directory,
                   Response)
from werkzeug.utils import secure_filename

from . import jobs, package as pkg, pipeline
from .queue import webside
from . import identify as ident
from . import library
from . import limits
from . import metrics
from . import notify
from . import render as rnd
from . import retention
from . import stats
from . import svg
from . import store
from . import sync as syncing
from .settings import settings

bp = Blueprint("main", __name__)

# 500 MB was the mockup's number and it rejects most real phone
# recordings: an iPhone at 4K30 passes it in three minutes, at 1080p30 in
# ten. Since the render is a fixed 1080p canvas either way, a 4K upload
# buys nothing but a longer wait — so the cap is generous and the page
# says 1080p is enough.
MAX_UPLOAD_GB = float(os.getenv("MAX_UPLOAD_GB", "4") or 4)
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_GB * 1024 * 1024 * 1024)

# Length is what costs: the encode grows with it, and the aligner's memory
# grows with its square. Nothing in the reference corpus of 249 recordings
# runs past 31 minutes, and the longest identified piece is a 23-minute
# concerto movement, so 25 admits everything real at a quarter of the
# memory 40 would need.
MAX_DURATION_MINUTES = float(os.getenv("MAX_DURATION_MINUTES", "25") or 25)
VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _uploads() -> pathlib.Path:
    d = pathlib.Path(current_app.config["UPLOAD_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------
@bp.get("/")
def index():
    """The site root is the designed interface.

    A development page used to live here, with its own controls and an SSE
    stream. It is gone; this redirects rather than 404s because the bare
    domain is what people type and what the mail links resolve against.
    """
    return redirect("/app/", code=302)


def _design_dir() -> pathlib.Path:
    return pathlib.Path(current_app.static_folder) / "svs"


# The design package ships as plain files linking to each other by relative
# name — `svs-min.js`, `assets/…`, `Credits.html`. It is served rather than
# templated so the markup and stylesheet stay byte for byte what was handed
# over, which means the whole folder has to answer under one prefix: served
# from anywhere else, the page asks for /svs-min.js and gets nothing.
@bp.get("/app/")
def studio():
    """The designed interface."""
    return send_file(_design_dir() / "index.html")


@bp.get("/app/<path:filename>")
def studio_file(filename: str):
    """Everything the page asks for beside itself."""
    return send_from_directory(_design_dir(), filename)


@bp.get("/app")
def studio_root():
    """Without the trailing slash every relative link would miss.

    The query survives the hop: `/app?debug` is the shape people type, and
    dropping it here would make the flag look broken rather than absent.
    """
    query = request.query_string.decode()
    return redirect("/app/" + (f"?{query}" if query else ""), code=308)


# --------------------------------------------------------------------------
# the library, in the shape the interface wants
# --------------------------------------------------------------------------
@bp.get("/api/library")
def api_library():
    """Every score we could actually render, for the manual picker.

    Keyed by the package's folder name, which is also what recognition
    resolves to, so the two lists refer to the same things by the same id.
    """
    works = []
    for p in library.packages():
        band_w, band_h = p.band_size
        works.append({
            "id": p.name,
            "t": p.title or p.display_name,
            "op": p.opus,
            "bars": p.last_measure,
            "ref": True,
            # The real strip, so the preview shows this score at its own
            # shape instead of a drawn approximation of one.
            "band_w": band_w,
            "band_h": band_h,
            "band": f"/api/library/{quote(p.name)}/band",
            # Vector bands are ink on transparency, so the preview can
            # recolour them the way the renderer does. A bitmap band is
            # ink already burnt onto paper and cannot be.
            "vector": p.has_vector,
            # Backdrop artwork is configured per install, not per score.
            "art": {"image": bool(settings.background_for("static")),
                    "video": bool(settings.background_for("dynamic"))},
            # Read from the score's own Humdrum header, because CC BY 4.0
            # requires the first edition be credited.
            "src": "nifc-first-editions",
            "ppr": p.metadata.get("PPR", ""),
            "ppp": p.metadata.get("PPP", ""),
        })
    return jsonify({"works": works,
                    "can_identify": settings.can_identify,
                    "can_sync": settings.can_sync,
                    # Asked so the interface never promises a message this
                    # install cannot send.
                    "can_email": settings.can_email,
                    # Stated so the free-tier copy quotes the number that is
                    # enforced, instead of drifting away from it.
                    "videos_per_week": limits.RULES["render_ip"].limit,
                    # Published so the privacy note quotes the window that
                    # is actually enforced rather than a number typed into
                    # copy once and then left behind by a config change.
                    "retention_hours": settings.retention_hot_hours,
                    "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
                    "max_minutes": MAX_DURATION_MINUTES,
                    "page_rule": _PAGE_RULE})


@bp.get("/metrics")
def api_metrics():
    """Prometheus scrapes this. It is not for the public.

    Loopback only in practice — gunicorn binds to 127.0.0.1 and Prometheus
    runs beside it — but whatever fronts the site in production has to keep
    this off the internet. It reports queue depth, what has failed and how
    much of the machine a render uses, which is a description of the
    business nobody outside it needs.
    """
    return Response(metrics.render(), mimetype="text/plain; version=0.0.4")


@bp.get("/api/stats")
def api_stats():
    """What this install has actually done. Counts, never estimates."""
    return jsonify({"videos": stats.videos()})


# Where the engraving actually sits on a plate, as fractions of it.
# Measured once per file and remembered: a page has wide blank margins —
# sixteen percent of this one is empty at the bottom — and tiling the whole
# plate puts two margins between every block of music, which reads as a gap
# rather than as continuous paper.
_INK: dict[tuple[str, int], tuple[float, float]] = {}


def _ink_band(path: pathlib.Path) -> tuple[float, float, float, float]:
    """(top, bottom, left, right) of the engraving, as fractions of the plate.

    All four, not just the vertical pair: plates tiled side by side show
    their side margins as a white channel between them, which is exactly
    the seam that makes a backdrop read as a stack of pages instead of as
    continuous music.
    """
    key = (str(path), path.stat().st_mtime_ns)
    if key in _INK:
        return _INK[key]
    band = (0.0, 1.0, 0.0, 1.0)
    try:
        import io
        from PIL import Image
        renderer = svg._cairosvg()
        if renderer is not None:
            png = renderer.svg2png(bytestring=path.read_bytes(), output_width=300)
            shot = Image.open(io.BytesIO(png)).convert("RGBA")
            # Transparent renders as black in greyscale, which would read as
            # ink everywhere; composite onto white first.
            flat = Image.new("RGB", shot.size, "white")
            flat.paste(shot, mask=shot.split()[3])
            grey = flat.convert("L")
            w, h = grey.size
            px = grey.load()
            rows = [y for y in range(h)
                    if any(px[x, y] < 200 for x in range(0, w, 2))]
            cols = [x for x in range(w)
                    if any(px[x, y] < 200 for y in range(0, h, 2))]
            if rows and cols:
                band = (rows[0] / h, (rows[-1] + 1) / h,
                        cols[0] / w, (cols[-1] + 1) / w)
    except Exception as exc:                     # noqa: BLE001
        logger.info("could not measure the engraving in %s: %s", path.name, exc)
    _INK[key] = band
    return band


def _trimmed(path: pathlib.Path) -> str:
    """The plate with its blank margins cropped away.

    A viewBox is added rather than the content moved: it is a viewport
    change, so nothing inside has to be understood or rewritten.
    """
    text = path.read_text(encoding="utf-8")
    top, bottom, left, right = _ink_band(path)
    if bottom - top > 0.98 and right - left > 0.98:
        return text

    # A join should show one margin, `a` — not two, and not none.
    #
    # Tiling whole plates puts two margins at every join: `2a` of blank
    # where the music should carry on. Cropping to the ink puts none, which
    # jams the last system of one page against the first of the next. So
    # crop to the ink and give back half a margin on each side; the two
    # halves meet and make exactly one.
    #
    # `a` is the SMALLER margin on each axis, and that matters here. This
    # plate has 1.7% at the head and 17% at the foot — the foot is not a
    # design margin at all, it is the space left when the music ran out
    # before the page did. Halving that left 277px at every vertical join,
    # which is precisely the `2a` this is meant to remove.
    gap_y = min(top, 1.0 - bottom) / 2
    gap_x = min(left, 1.0 - right) / 2
    top, bottom = max(0.0, top - gap_y), min(1.0, bottom + gap_y)
    left, right = max(0.0, left - gap_x), min(1.0, right + gap_x)

    height = _svg_px(text, "height") or 2970.0
    width = _svg_px(text, "width") or 2100.0
    x, y = left * width, top * height
    wide, tall = (right - left) * width, (bottom - top) * height
    opening = re.search(r"<svg\b[^>]*>", text)
    if not opening:
        return text

    # The plate is nested inside a new root rather than edited in place.
    #
    # Editing it in place did not work, through several attempts: Verovio
    # writes overflow="visible" on the root, and a root <svg> is where a
    # viewBox sets up the coordinate system rather than a window onto it.
    # The crop kept changing the numbers and drawing the whole page.
    #
    # An OUTER svg has no such ambiguity. Its viewport clips by default,
    # and the plate is placed inside it shifted by exactly the margin being
    # removed. Every renderer agrees about this, which the other approach
    # could not be relied on for.
    inner = text[opening.start():]
    inner = re.sub(r'^<svg\b[^>]*>',
                   f'<svg x="{-x:.0f}" y="{-y:.0f}" '
                   f'width="{width:.0f}" height="{height:.0f}" '
                   f'viewBox="0 0 {width:.0f} {height:.0f}" '
                   f'overflow="visible">',
                   inner, count=1)
    # Staff lines, stems, beams and barlines are stroked paths carrying a
    # stroke-width and no stroke: Verovio's own stylesheet paints them with
    # `stroke: currentColor`. As page furniture there is no document colour
    # for that to inherit, so every stroked symbol renders with no stroke at
    # all — the score arrives as noteheads floating with nothing to stand
    # on, because noteheads are filled rather than stroked.
    #
    # Written as an attribute rather than a rule. A <style> block depends on
    # the renderer implementing CSS inside SVG, which is exactly the
    # assumption that lost the lines in the first place; an attribute is
    # understood by everything that can draw a path at all.
    def paint(match: "re.Match[str]") -> str:
        tag = match.group(0)
        if " stroke=" in tag or "stroke-width" not in tag:
            return tag
        return "<path stroke=\"#1c1622\"" + tag[len("<path"):]

    inner = re.sub(r"<path\b[^>]*>", paint, inner)
    head = text[:opening.start()]
    return (f'{head}<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{wide:.0f}px" height="{tall:.0f}px" '
            f'viewBox="0 0 {wide:.0f} {tall:.0f}">{inner}</svg>')


def _svg_px(text: str, attr: str) -> float | None:
    m = re.search(rf'<svg\b[^>]*\s{attr}="([\d.]+)', text)
    return float(m.group(1)) if m else None


@bp.get("/api/library/<path:name>/page")
def api_page(name: str):
    """One full engraved page from a score, for use as page furniture.

    The bands are strips cut for the video; these are the whole plate as it
    was engraved, which is what you want behind a page of text. They are
    already beside the package — music_line_extractor writes them next to
    the bands — so nothing has to be opened or extracted.

    Vector only, deliberately: a bitmap of an A4 plate is a megabyte and
    cannot be recoloured, and this is decoration that must never cost more
    than the content it sits behind.
    """
    package = library.find(name)
    if package is None:
        return jsonify({"error": f"No score package named {name!r}."}), 404

    pages = sorted(package.root.glob("pages/page_*.svg"))
    if not pages:
        return jsonify({"error": "That score has no engraved pages."}), 404

    wanted = request.args.get("n", type=int) or 1
    chosen = pages[max(0, min(len(pages) - 1, wanted - 1))]

    ink = _hex_colour(request.args.get("ink", ""))
    # Trimmed by default: as page furniture the blank margins are
    # only a gap between one block of music and the next.
    trim = request.args.get("trim", "1") not in ("0", "false", "no")
    body = _trimmed(chosen) if trim else chosen.read_text(encoding="utf-8")
    if ink:
        body = _tint_text(body, ink)
    return _svg_response(
        body, f"{chosen}|{chosen.stat().st_mtime_ns}|{ink}|trim={trim}")


def _svg_response(body: str, tag: str):
    """An SVG, compressed when the client will take it.

    These plates are a third of a megabyte of markup each and compress
    about tenfold. Uncompressed, using them as page furniture would cost
    more than everything else on the page put together, which is not a
    trade decoration is allowed to make. Flask does not compress anything
    by itself, so it is done here.
    """
    raw = body.encode("utf-8")
    accepts = "gzip" in request.headers.get("Accept-Encoding", "").lower()
    payload = gzip.compress(raw, 6) if accepts else raw

    response = Response(payload, mimetype="image/svg+xml")
    if accepts:
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
    response.headers["Content-Length"] = str(len(payload))
    response.set_etag(hashlib.sha1(f"{tag}|{_TINT_RULE}".encode()).hexdigest())
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response.make_conditional(request)


@bp.get("/api/library/<path:name>/pages")
def api_pages(name: str):
    """How many engraved pages this score has."""
    package = library.find(name)
    if package is None:
        return jsonify({"error": f"No score package named {name!r}."}), 404
    return jsonify({"pages": len(sorted(package.root.glob("pages/page_*.svg")))})


@bp.get("/api/library/<path:name>/band")
def api_band(name: str):
    """One band image from a score, for the preview to show.

    Defaults to the first, which is what step three needs: the opening of
    the piece, at the real proportions the renderer will use. Vector is
    preferred where the package has it — the preview is scaled to whatever
    the frame is, and an svg survives that.
    """
    package = library.find(name)
    if package is None or not package.bands:
        return jsonify({"error": f"No score package named {name!r}."}), 404

    measure = request.args.get("measure", type=int)
    band = package.bands[0]
    if measure is not None:
        # The band in force at that measure: the last one that has started.
        for candidate in package.bands:
            if candidate.first_measure <= measure:
                band = candidate
            else:
                break
    elif not band.is_vector:
        band = next((b for b in package.bands
                     if b.first_measure == band.first_measure and b.is_vector), band)

    # Tint the engraving, when asked and when it is vector. Verovio's own
    # stylesheet inside the file sets `stroke: currentColor` and leaves
    # fills to inherit, so colouring the root element is enough and no path
    # is touched: `fill="none"` stays none, and the red marker stays red.
    #
    # This is done here rather than with a CSS mask in the page because the
    # engraving carries `width="100%"` and no height — it has no intrinsic
    # size, which makes it an unreliable mask and paints a solid block over
    # the band's paper instead of ink on it.
    ink = _hex_colour(request.args.get("ink", ""))
    if not (ink and band.is_vector):
        response = send_file(band.path, conditional=True)
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    response = Response(_tint(band.path, ink), mimetype="image/svg+xml")
    # The preview asks for this on every repaint, so it must be cacheable —
    # but a plain max-age pinned the browser to whatever tinting produced
    # the first time it saw a colour, which survived changes to how the
    # tinting works. Revalidating against a tag that covers the file, the
    # ink AND the rule keeps repaints cheap and never serves a stale idea
    # of what "in this colour" means.
    response.set_etag(hashlib.sha1(
        f"{band.path}|{band.path.stat().st_mtime_ns}|{ink}|{_TINT_RULE}"
        .encode()).hexdigest())
    response.headers["Cache-Control"] = "no-cache"
    return response.make_conditional(request)


# Bumped whenever _tint changes what it does, so caches let go of the old
# answer instead of showing a half-coloured score.
_TINT_RULE = 2

# Bumped when the plate's crop changes, so cached copies are let go of.
_PAGE_RULE = 9


def _hex_colour(raw: str) -> str:
    """`#rgb` or `#rrggbb`, or nothing — this ends up inside an attribute."""
    value = (raw or "").strip()
    if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value):
        return value
    return ""


def _tint(path: pathlib.Path, ink: str) -> str:
    """The engraving in one colour: every symbol, not merely the filled ones.

    Verovio's stylesheet inside the file draws strokes with `currentColor`
    and leaves fills to inherit, so colouring the root would seem to be
    enough — except that the inner `definition-scale` element carries
    `color="black"` and wraps nearly the whole score. That beats anything
    set above it, so the noteheads followed the ink while every stroked
    symbol — staff lines, stems, beams, slurs, barlines — stayed black.
    Both have to be answered.

    `fill="none"` is left alone: those paths are drawn by their stroke, and
    filling them would blot the score.
    """
    return _tint_text(path.read_text(encoding="utf-8", errors="replace"), ink)


def _tint_text(markup: str, ink: str) -> str:
    """The same, on markup already in hand — a plate that has been trimmed
    has no file to re-read."""
    out = markup.replace("<svg ", f'<svg style="color:{ink};fill:{ink}" ', 1)
    out = out.replace('color="black"', f'color="{ink}"')
    # Editorial marks are engraved in red. They are part of the score, so
    # they take the chosen colour along with everything else.
    out = out.replace('color="red"', f'color="{ink}"')
    out = out.replace('fill="red"', f'fill="{ink}"')
    return out


# --------------------------------------------------------------------------
# recognition
# --------------------------------------------------------------------------
# Identification takes the better part of a minute, so it runs on a thread
# and the page asks how it went. Kept beside the route rather than in the
# job record while this is the only thing that needs it.
_identifications: dict[str, dict] = {}
_identify_lock = threading.Lock()

# Below this share of the winner's score a candidate is noise: measured
# runner-ups at a hundredth of the winner reordered between identical runs,
# so offering them as "did you mean" would suggest a different wrong piece
# each time. One real alternative is worth more than three random ones.
ALTERNATIVE_FLOOR = 0.02


def _candidates(result) -> list[dict]:
    """Recognition's answer in the shape the interface draws."""
    resolved = [library.resolve(c) for c in result.candidates]
    if not resolved:
        return []
    total = sum(max(c["score"], 0.0) for c in resolved) or 1.0
    best = resolved[0]["score"] or 1.0
    out = []
    for c in resolved:
        if c is not resolved[0] and c["score"] < best * ALTERNATIVE_FLOOR:
            continue
        out.append({**c, "confidence": round(100 * c["score"] / total)})
    return out


def _identify_now(job: jobs.Job) -> dict:
    try:
        result = ident.identify(job.upload_path, job.duration)
    except ident.IdentifyUnavailable as exc:
        return {"state": "error", "recognised": False, "error": str(exc),
                "configured": False}
    except ident.IdentifyError as exc:
        return {"state": "error", "recognised": False, "error": str(exc)}

    candidates = _candidates(result)
    renderable = [c for c in candidates if c["renderable"]]
    if result.outcome != ident.MATCHED:
        state = "unrecognised"
    elif renderable:
        state = "matched"
    else:
        # Named it, but the score is not in this library yet. A different
        # answer from "we could not place it", and with one package
        # installed it is the likely one.
        state = "unavailable"
    return {"state": "done", "recognised": result.outcome == ident.MATCHED,
            "outcome": state, "candidates": candidates,
            "consensus": result.consensus, "windows": result.n_windows,
            "timing": result.timing}


@bp.post("/api/jobs/<job_id>/identify")
def api_identify(job_id: str):
    """Start listening to an upload. Returns at once; ask again for the answer."""
    job = jobs.registry.get(job_id)
    if job is None:
        return jsonify({"error": "No such job."}), 404
    if not settings.can_identify:
        return jsonify({"error": settings.why_cannot_identify()}), 503
    try:
        limits.guard("identify_ip", limits.client_key(request))
    except limits.Refused as exc:
        return jsonify({"error": str(exc)}), 429, {"Retry-After": str(exc.retry_after)}

    with _identify_lock:
        current = _identifications.get(job_id)
        if current and current.get("state") in ("running", "done"):
            return jsonify(current), 202
        _identifications[job_id] = {"state": "running"}

    def work() -> None:
        outcome = _identify_now(job)
        with _identify_lock:
            _identifications[job_id] = outcome

    threading.Thread(target=work, name=f"identify-{job_id}", daemon=True).start()
    return jsonify({"state": "running"}), 202


@bp.get("/api/jobs/<job_id>/identification")
def api_identification(job_id: str):
    """How the listening went, or that it is still going."""
    with _identify_lock:
        current = _identifications.get(job_id)
    if current is None:
        return jsonify({"state": "idle"})
    return jsonify(current)


# --------------------------------------------------------------------------
# what this install can do
# --------------------------------------------------------------------------
# `/api/options` and `/api/scores` were here. They existed only for the
# development page, and went with it: the designed interface builds its
# controls from `/api/library`, which carries the same facts per work.


# --------------------------------------------------------------------------
# uploading
# --------------------------------------------------------------------------
@bp.post("/api/upload")
def api_upload():
    try:
        limits.guard("upload_ip", limits.client_key(request))
    except limits.Refused as exc:
        return jsonify({"error": str(exc)}), 429, {"Retry-After": str(exc.retry_after)}

    # The rights confirmation is a checkbox in the page, which means a
    # script never sees it. Asserted here as well, so accepting somebody
    # else's recording takes a deliberate lie rather than a missing tick.
    if str(request.form.get("rights", "")).lower() not in ("1", "true", "yes", "on"):
        return jsonify({
            "error": "The rights to this recording have not been confirmed.",
        }), 400

    if "video" not in request.files:
        return jsonify({"error": "No file was sent."}), 400
    upload = request.files["video"]
    if not upload.filename:
        return jsonify({"error": "No file was selected."}), 400

    suffix = pathlib.Path(secure_filename(upload.filename)).suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        return jsonify({
            "error": f"{suffix or 'That file type'} isn't supported.",
            "detail": f"Use one of: {', '.join(sorted(VIDEO_SUFFIXES))}.",
        }), 415

    job = jobs.new_job(upload.filename, _uploads() / "pending")
    dest = _uploads() / f"{job.id}{suffix}"
    job.upload_path = dest
    upload.save(dest)

    try:
        info = rnd.probe(dest)
    except rnd.RenderError as exc:
        dest.unlink(missing_ok=True)
        job.state, job.error = "error", str(exc)
        return jsonify({"error": job.error}), 415

    minutes = (info["duration"] or 0) / 60.0
    if not info["duration"]:
        dest.unlink(missing_ok=True)
        job.state, job.error = "error", (
            "The length of that video could not be read, so we cannot tell "
            "how long it would take to process.")
        return jsonify({"error": job.error}), 415

    if minutes > MAX_DURATION_MINUTES:
        # Length is what costs: the encode grows with it and the aligner's
        # memory grows with its square. Refused here rather than after a
        # visitor has waited in a queue for it.
        dest.unlink(missing_ok=True)
        job.state, job.error = "error", (
            f"That recording is {minutes:.0f} minutes long, and we can take "
            f"up to {MAX_DURATION_MINUTES:.0f}. If it is a whole sonata or "
            f"a recital, upload one movement at a time — the scores are per "
            f"movement anyway.")
        return jsonify({"error": job.error}), 413

    if not info["has_audio"]:
        dest.unlink(missing_ok=True)
        job.state, job.error = "error", (
            "That video has no audio track. The score is aligned to the "
            "performance audio, so there's nothing to sync against.")
        return jsonify({"error": job.error}), 415

    job.duration = info["duration"]
    job.size_bytes = dest.stat().st_size
    return jsonify({"job": job.public(), "probe": info})


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _style_from(body: dict) -> rnd.Style:
    """Build a Style from the request, resolving backdrop art from config."""
    style = body.get("style") or {}
    background = style.get("background", rnd.NONE)
    path = style.get("background_path") or settings.background_for(background)

    def number(key: str, default: float) -> float:
        try:
            return float(style.get(key, default))
        except (TypeError, ValueError):
            return default

    return rnd.Style(
        aspect=style.get("aspect", "16/9"),
        background=background,
        background_path=path,
        band_position=style.get("band_position", rnd.BOTTOM),
        panel=bool(style.get("panel", False)),
        panel_width=number("panel_width", 0.301),
        canvas_bg=style.get("canvas_bg", "#141019"),
        band_bg=style.get("band_bg", "#ffffff"),
        band_fg=style.get("band_fg", "#1c1622"),
        band_bg_opacity=number("band_bg_opacity", 1.0),
        band_opacity=number("band_opacity", 1.0),
        # Which slice of the video's height survives the crop, when it is
        # taller than the space beside the band.
        video_offset=number("video_offset", 0.5),
    )


@bp.post("/api/jobs/<job_id>/render")
def api_render(job_id: str):
    job = jobs.registry.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job."}), 404
    if job.state in (store.QUEUED, store.RUNNING):
        return jsonify({"error": "That job is already in the queue."}), 409
    if not job.upload_path.is_file():
        return jsonify({"error": "The uploaded video is no longer on disk."}), 410

    body = request.get_json(silent=True) or {}
    address = str(body.get("email", "")).strip().lower()
    # Checked here as well as before sending, so somebody who mistypes is
    # told now rather than waiting out a render for a mail that never comes
    # — and so a string carrying several recipients never reaches the queue.
    if address:
        try:
            address = notify.one_address(address)
        except notify.MailError:
            return jsonify({"error": "That email address does not look right. "
                                     "Please check it and try again."}), 400
    try:
        limits.guard("render_ip", limits.client_key(request))
        if address:
            limits.guard("render_email", address)
    except limits.Refused as exc:
        return jsonify({"error": str(exc)}), 429, {"Retry-After": str(exc.retry_after)}

    score = str(body.get("score", "")).strip()
    if not score:
        return jsonify({"error": "Pick a score first."}), 400

    package = pipeline.find_package(score)
    if package is None:
        return jsonify({"error": f"No score package named {score!r}."}), 400

    mode = body.get("mode") or None
    try:
        style = _style_from(body)
        style.validate()
        mode = pipeline.choose_mode(package, mode)
    except (rnd.RenderError, pipeline.PipelineError) as exc:
        return jsonify({"error": str(exc)}), 400

    job.email = address
    jobs.registry.start(job, package.name, mode, style, body.get("meta") or {})
    return jsonify({"job": job.public()})


@bp.get("/api/jobs/<job_id>/status")
def api_status(job_id: str):
    job = jobs.registry.get(job_id)
    return (jsonify({"job": job.public()}) if job
            else (jsonify({"error": "Unknown job."}), 404))


@bp.get("/api/jobs/<job_id>/download")
def api_download(job_id: str):
    job = jobs.registry.get(job_id)
    if job is None or job.state != "done" or not job.result:
        return jsonify({"error": "That video isn't ready."}), 404

    # 410, not 404. The video still exists; it has moved to storage that
    # cannot serve it directly. "Gone" is the honest code, and the message
    # has to say archived rather than missing — whoever is reading this
    # waited for the file, and a bare 404 tells them we threw it away.
    if not retention.is_live(job.finished):
        return jsonify({"error": retention.gone_message(),
                        "expired": True}), 410

    # Named from our own library, never from what the visitor called their
    # file: that name is untrusted text and this sets a response header.
    stem = _safe_stem(job.score) or "score_video"
    return send_file(job.result, as_attachment=True,
                     download_name=f"{stem}_score_sync.mp4")


def _safe_stem(name: str | None) -> str:
    """A filename from a score's name: letters, digits, dot, dash, underscore."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "")).strip("._-")[:80]


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
def _ensure_logging() -> None:
    """Make sure something is listening, whatever started this process.

    `run.py` configures logging — but gunicorn never runs `run.py`. It
    imports `create_app` directly, so under gunicorn everything the applier
    and the janitor had to say went nowhere again, which is the exact
    problem configuring it in `run.py` was meant to solve. gunicorn sets up
    its own `gunicorn.error` and `gunicorn.access` loggers and leaves the
    root logger alone, so this fills that gap rather than fighting it.

    Only when the root has no handlers: a caller that has already decided
    how logging works keeps its decision.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> Flask:
    _ensure_logging()
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        UPLOAD_DIR=str(settings.work_dir / "uploads"),
    )
    app.register_blueprint(bp)

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"error": f"That file is larger than "
                                 f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."}), 413

    settings.ensure_dirs()

    # Finished videos outlive the process that made them.
    # Anything queued when this last stopped is still queued, and a
    # render caught in flight goes back to the front of the line
    # rather than being reported as finished. Done here rather than
    # in run.py so it also happens under gunicorn.
    webside.start_threads()
    return app
