"""HTTP surface for the score-sync web app."""

from __future__ import annotations

import json
import pathlib
import subprocess

from flask import (Blueprint, Flask, current_app, jsonify, render_template,
                   request, send_file, Response)
from werkzeug.utils import secure_filename

from . import jobs, pipeline, vss_bridge

bp = Blueprint("main", __name__)

MAX_UPLOAD_BYTES = 500 * 1024 * 1024      # matches the mockup's "up to 500 mb"


def _uploads_dir() -> pathlib.Path:
    d = pathlib.Path(current_app.config["UPLOAD_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _probe(path: pathlib.Path) -> dict:
    """Duration and stream summary, or {} if ffprobe can't read the file."""
    cfg = vss_bridge.activate()["config"]
    try:
        out = subprocess.run(
            [cfg.FFPROBE_EXE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, check=True).stdout
        info = json.loads(out)
    except Exception:  # noqa: BLE001 - a bad upload is a user error, not a crash
        return {}

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {
        "duration": float(info.get("format", {}).get("duration", 0) or 0),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/scores")
def api_scores():
    return jsonify({"scores": pipeline.list_ready_scores()})


@bp.post("/api/upload")
def api_upload():
    if "video" not in request.files:
        return jsonify({"error": "No file was sent."}), 400
    upload = request.files["video"]
    if not upload.filename:
        return jsonify({"error": "No file was selected."}), 400

    cfg = vss_bridge.activate()["config"]
    name = secure_filename(upload.filename)
    ext = pathlib.Path(name).suffix.lower()
    allowed = {e.lower() for e in cfg.VIDEO_VALID_EXTENSION}
    if ext not in allowed:
        return jsonify({
            "error": f"{ext or 'That file type'} isn't supported.",
            "detail": f"Use one of: {', '.join(sorted(allowed))}.",
        }), 415

    job = jobs.new_job(original_name=upload.filename,
                       upload_path=_uploads_dir() / f"pending{ext}")
    dest = _uploads_dir() / f"{job.id}{ext}"
    job.upload_path = dest
    upload.save(dest)

    probe = _probe(dest)
    if not probe.get("has_video"):
        dest.unlink(missing_ok=True)
        job.state, job.error = "error", "That file has no video track we can read."
        return jsonify({"error": job.error}), 415
    if not probe.get("has_audio"):
        dest.unlink(missing_ok=True)
        job.state, job.error = "error", (
            "That video has no audio track. The score is aligned to the "
            "performance audio, so we can't sync without it.")
        return jsonify({"error": job.error}), 415

    job.duration = probe.get("duration")
    job.size_bytes = dest.stat().st_size
    return jsonify({"job": job.public(), "probe": probe})


@bp.post("/api/jobs/<job_id>/render")
def api_render(job_id: str):
    job = jobs.registry.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job."}), 404
    if job.state == "running":
        return jsonify({"error": "That job is already rendering."}), 409

    body = request.get_json(silent=True) or {}
    score_id = str(body.get("score_id", "")).strip()
    if not score_id:
        return jsonify({"error": "Pick a score first."}), 400
    if score_id not in {s["score_id"] for s in pipeline.list_ready_scores()}:
        return jsonify({"error": f"Score {score_id} isn't ready to render."}), 400

    jobs.registry.start(job, score_id, body.get("meta") or {})
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
        "X-Accel-Buffering": "no",       # don't let a proxy buffer SSE
    })


@bp.get("/api/jobs/<job_id>/download")
def api_download(job_id: str):
    job = jobs.registry.get(job_id)
    if job is None or job.state != "done" or not job.result:
        return jsonify({"error": "That video isn't ready."}), 404

    stem = pathlib.Path(job.original_name).stem
    return send_file(job.result, as_attachment=True,
                     download_name=f"{stem}_score_sync.mp4")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        UPLOAD_DIR=str(pathlib.Path(__file__).resolve().parent.parent / "var" / "uploads"),
    )
    app.register_blueprint(bp)

    @app.errorhandler(413)
    def too_large(_):
        return jsonify({"error": "That file is larger than 500 MB."}), 413

    # Fail loudly at startup rather than on the first upload.
    vss_bridge.activate()
    return app
