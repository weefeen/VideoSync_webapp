"""HTTP surface for the score-sync web app."""

from __future__ import annotations

import json
import pathlib

from flask import (Blueprint, Flask, current_app, jsonify, render_template,
                   request, send_file, Response)
from werkzeug.utils import secure_filename

from . import autosync, jobs, package as pkg, panel, pipeline
from . import render as rnd
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
