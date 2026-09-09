"""HTTP surface for the score-sync web app."""

from __future__ import annotations

import json
import pathlib
import threading
from urllib.parse import quote

from flask import (Blueprint, Flask, current_app, jsonify, redirect,
                   render_template, request, send_file, send_from_directory,
                   Response)
from werkzeug.utils import secure_filename

from . import autosync, jobs, package as pkg, panel, pipeline
from . import identify as ident
from . import library
from . import render as rnd
from . import sync as syncing
from .settings import settings

bp = Blueprint("main", __name__)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024       # matches the mockup's "up to 500 mb"
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
    return render_template("index.html")


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
                    "can_sync": settings.can_sync})


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

    response = send_file(band.path, conditional=True)
    # The library only changes when a package is added, and the preview
    # asks for this on every redraw.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


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
@bp.get("/api/options")
def api_options():
    """Everything the UI needs to build its controls."""
    return jsonify({
        "aspects": sorted(rnd.ASPECTS),
        "backgrounds": [
            {"value": kind,
             "available": kind == rnd.NONE or bool(settings.background_for(kind))}
            for kind in rnd.BACKGROUNDS
        ],
        "positions": [rnd.TOP, rnd.BOTTOM],
        "modes": [{"value": m, "label": pipeline.MODE_LABELS[m]}
                  for m in pipeline.MODES],
        "panel_fields": [f[0] for f in panel.FIELDS],
        "can_align": settings.can_autosync,
        "can_rasterize_svg": pkg.can_rasterize_svg(),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "composer_filter": settings.composer_filter,
    })


@bp.get("/api/scores")
def api_scores():
    """Usable score packages, and what each one supports."""
    out = []
    for p in pipeline.usable_packages():
        vector = sum(1 for b in p.bands if b.is_vector)
        has_ref = autosync.has_reference(p.root)
        out.append({
            "name": p.name,
            "label": p.display_name,
            "composer": p.composer,
            "title": p.title,
            "opus": p.opus,
            "bands": len(p.bands),
            "vector_bands": vector,
            "band_size": list(p.band_size),
            "measures": p.last_measure,
            "modes": pipeline.MODES if has_ref else [pipeline.AUTO],
            "has_reference": has_ref,
            "has_score_source": autosync.find_score_source(p.root) is not None,
        })
    return jsonify({"scores": sorted(out, key=lambda s: s["name"])})


# --------------------------------------------------------------------------
# uploading
# --------------------------------------------------------------------------
@bp.post("/api/upload")
def api_upload():
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
    if job.state == "running":
        return jsonify({"error": "That job is already running."}), 409
    if not job.upload_path.is_file():
        return jsonify({"error": "The uploaded video is no longer on disk."}), 410

    body = request.get_json(silent=True) or {}
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

    jobs.registry.start(job, package.name, mode, style, body.get("meta") or {})
    return jsonify({"job": job.public()})


@bp.get("/api/jobs/<job_id>/status")
def api_status(job_id: str):
    job = jobs.registry.get(job_id)
    return (jsonify({"job": job.public()}) if job
            else (jsonify({"error": "Unknown job."}), 404))


@bp.get("/api/jobs/<job_id>/events")
def api_events(job_id: str):
    if jobs.registry.get(job_id) is None:
        return jsonify({"error": "Unknown job."}), 404

    def stream():
        for event in jobs.registry.stream(job_id):
            yield f"data: {json.dumps(event)}\n\n"

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",          # don't let a proxy buffer SSE
    })


@bp.get("/api/jobs/<job_id>/download")
def api_download(job_id: str):
    job = jobs.registry.get(job_id)
    if job is None or job.state != "done" or not job.result:
        return jsonify({"error": "That video isn't ready."}), 404
    stem = pathlib.Path(job.original_name).stem
    return send_file(job.result, as_attachment=True,
                     download_name=f"{stem}_score_sync.mp4")


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
def create_app() -> Flask:
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
    return app
