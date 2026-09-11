"""The bucket: where a finished video lives once it is finished.

WHY THIS EXISTS. Until now a render's output was a path on the machine that
produced it, and the web process served it with `send_file`. That works
exactly as long as the renderer and the web app share a disk. The moment the
renderer moves to a host that is created for a job and destroyed afterwards —
which is the whole point of the compute design — the only copy of somebody's
video is on a machine that is about to be deleted. This is the thing that has
to exist before that machine may ever be destroyed.

THE HARD ORDERING RULE, and it is not negotiable: *"the render succeeded"* and
*"the result is safe"* are different events, and the second one is what the
worker may report. `put` therefore uploads AND confirms with a HEAD before it
returns, and the worker only announces `done` after it returns. Until then the
message is unacknowledged, the job is not finished, and a destroyed instance
costs a re-render rather than a video.

PRIVATE BUCKET, PRESIGNED LINKS ONLY. Nothing here sets an ACL and nothing is
ever public. A visitor's download is a 302 to a URL that works for fifteen
minutes and is generated per click, so the web box never proxies the bytes and
a link that leaks is a link that has already expired.

OPTIONAL, LIKE GEOLOCATION. Without `boto3`, or without credentials, this
reports why and the app falls back to serving from local disk — which is
exactly what it does today. That fallback is a development convenience and NOT
a production configuration: a compute host may not be destroyed while it is in
use, and `problem()` is what says so.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import time
import threading
from typing import Any

from .settings import settings

logger = logging.getLogger(__name__)

# Anything larger goes up in parts, so a dropped connection fails the PUT
# rather than silently truncating the object. A finished render is ~125 MB
# measured, so in practice every output takes this path.
MULTIPART_THRESHOLD = 100 * 1024 * 1024

_client = None
_tried = False
_problem = ""
# Resolved once, beside the client, rather than imported inside `put`. Doing
# it per call made the upload path depend on boto3 being importable even when
# a client had been supplied — which is how it is tested, and the test is the
# only reason that was noticed.
_transfer = None
_lock = threading.Lock()


def _connect():
    """The S3 client, or None with a reason recorded. Built once."""
    global _client, _tried, _problem
    with _lock:
        if _tried:
            return _client
        _tried = True

        if not settings.object_bucket:
            _problem = ("OBJECT_BUCKET is not set, so finished videos stay on "
                        "local disk. A compute host must not be destroyed "
                        "while this is true.")
            return None
        try:
            import boto3                              # noqa: PLC0415
            from boto3.s3.transfer import TransferConfig   # noqa: PLC0415
            from botocore.config import Config        # noqa: PLC0415
        except ImportError:
            _problem = ("The `boto3` package is not installed, so the bucket "
                        "cannot be reached. `pip install boto3`.")
            return None
        global _transfer
        _transfer = TransferConfig(multipart_threshold=MULTIPART_THRESHOLD)
        try:
            _client = boto3.client(
                "s3",
                endpoint_url=settings.object_endpoint or None,
                region_name=settings.object_region or None,
                aws_access_key_id=settings.object_key or None,
                aws_secret_access_key=settings.object_secret or None,
                # Retries are the default 'legacy' 3 attempts otherwise, and
                # this runs on a box whose network is shared with an upload.
                config=Config(retries={"max_attempts": 5, "mode": "standard"},
                              signature_version="s3v4"))
        except Exception as exc:                      # noqa: BLE001
            _problem = f"the bucket client could not be built: {exc}"
            logger.warning("object storage unavailable: %s", _problem)
        return _client


def available() -> bool:
    return _connect() is not None


def status() -> dict[str, Any]:
    """Whether the bucket works, and why not when it does not."""
    client = _connect()
    return {"available": client is not None,
            "bucket": settings.object_bucket,
            "endpoint": settings.object_endpoint,
            "problem": "" if client is not None else _problem}


class StorageError(RuntimeError):
    """The object could not be stored, or could not be confirmed stored."""


def output_key(job_id: str, suffix: str = ".mp4") -> str:
    """Where a finished render lives.

    Keyed by job rather than by the visitor's file name, which is untrusted
    text: a name is free to contain a slash, and a slash in an S3 key is a
    directory separator.
    """
    return f"jobs/{job_id}/output/{job_id}_PROCESSED{suffix}"


def put(local: pathlib.Path, key: str) -> int:
    """Upload, then CONFIRM, then return the confirmed size in bytes.

    The confirmation is the point. `upload_file` returning without raising
    means the parts were accepted; it does not mean an object of the right
    size is readable at that key. A HEAD costs one request and is the
    difference between "we think it is safe" and "it is safe" — and the
    worker is about to tell a visitor their video is ready on the strength
    of it.
    """
    client = _connect()
    if client is None:
        raise StorageError(_problem or "no object storage configured")

    expected = local.stat().st_size
    # No ACL argument anywhere: the bucket is private and objects inherit
    # that. Passing one is how a bucket accidentally becomes public.
    extra = {"ExtraArgs": {"ContentType": "video/mp4"}}
    if _transfer is not None:
        extra["Config"] = _transfer
    try:
        client.upload_file(str(local), settings.object_bucket, key, **extra)
    except Exception as exc:                          # noqa: BLE001
        raise StorageError(f"upload of {key} failed: {exc}") from exc

    got = head(key)
    if got is None:
        raise StorageError(f"{key} was uploaded and is not there")
    if got != expected:
        raise StorageError(
            f"{key} is {got} bytes, expected {expected} — the upload was "
            f"truncated and the local copy is the only whole one")
    return got


def head(key: str) -> int | None:
    """The object's size, or None if it is not there."""
    client = _connect()
    if client is None:
        return None
    try:
        got = client.head_object(Bucket=settings.object_bucket, Key=key)
        return int(got["ContentLength"])
    except Exception:                                 # noqa: BLE001
        return None


def presigned_get(key: str, seconds: int = 900,
                  filename: str | None = None) -> str | None:
    """A link that works for fifteen minutes and is generated per click.

    Short on purpose. The link is the only thing standing between a private
    bucket and the open internet, so it is made to expire before it can be
    shared, indexed or logged into anything that outlives the click.
    """
    client = _connect()
    if client is None:
        return None
    params = {"Bucket": settings.object_bucket, "Key": key}
    if filename:
        # So the browser saves it under our name rather than the key.
        params["ResponseContentDisposition"] = (
            f'attachment; filename="{filename}"')
    try:
        return client.generate_presigned_url("get_object", Params=params,
                                             ExpiresIn=seconds)
    except Exception:                                 # noqa: BLE001
        logger.exception("could not sign a link for %s", key)
        return None


def delete(key: str) -> bool:
    client = _connect()
    if client is None:
        return False
    try:
        client.delete_object(Bucket=settings.object_bucket, Key=key)
        return True
    except Exception:                                 # noqa: BLE001
        logger.exception("could not delete %s", key)
        return False


def reset() -> None:
    """Forget the client, so the next call rebuilds it. For tests."""
    global _client, _tried, _problem
    with _lock:
        _client, _tried, _problem = None, False, ""


def write_manifest(folder: pathlib.Path, job_id: str, *,
                   package: str = "", mode: str = "",
                   style: dict | None = None,
                   duration: float | None = None,
                   output: pathlib.Path | None = None,
                   output_bytes: int | None = None,
                   elapsed: float | None = None) -> pathlib.Path:
    """Write `manifest.json` into the job folder, so the project explains
    itself.

    Stored with everything else, and the reason it exists is a year from
    now: a folder holding `measures.data` and `performance.npy` and a video
    is not self-describing. Which score was it aligned against? Which
    edition? Did it use the reference recording or align directly? Without
    that written down, the corpus is a pile of arrays.

    CARRIES NO PERSONAL DATA, deliberately — no address, no email, no
    uploaded file name. These objects are kept indefinitely for training
    while the job row can be deleted on request, and a manifest holding a
    visitor's details would quietly defeat that. The length of the recording
    is a property of the music; the name of the file was typed by a person.
    """
    files = []
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({"path": path.relative_to(folder).as_posix(),
                          "bytes": path.stat().st_size})

    manifest = {
        "job": job_id,
        "written": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # What it was aligned against, and how. `mode` is the answer to
        # "reference recording or direct", which changes what the numbers in
        # measures.data mean.
        "score_package": package,
        "score_fingerprint": _fingerprint(package),
        "alignment_mode": mode,
        "style": style or {},
        "media_seconds": duration,
        "render_seconds": elapsed,
        # measures.data is the output of a particular DTW in a particular
        # librosa. Without this, "which code produced this alignment" has no
        # answer a year from now, and a corpus of alignments whose
        # provenance is unknown cannot be used to judge a new one.
        "produced_by": _versions(),
        "output": {
            "name": output.name if output else None,
            "bytes": output_bytes,
            "key": output_key(job_id, output.suffix if output else ".mp4"),
        },
        "files": files,
        "schema": 2,
    }
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def _fingerprint(package: str) -> str:
    """A short hash of the score this was aligned against.

    Packages get re-engraved. Without knowing WHICH engraving produced an
    alignment, an old `measures.data` cannot be checked against the score it
    describes — the bar numbers may no longer mean the same bars. The hash
    covers the score's own chroma and its reference measures, which are
    exactly the inputs the alignment depended on.

    Empty when the package cannot be read. A missing fingerprint is honest;
    a wrong one is worse than none.
    """
    try:
        from . import pipeline                          # noqa: PLC0415
        found = pipeline.find_package(package)
        if found is None:
            return ""
        digest = hashlib.sha256()
        root = pathlib.Path(getattr(found, "root", "") or "")
        for name in ("chroma.npy", "reference"):
            path = root / name
            if path.is_file():
                digest.update(path.read_bytes())
            elif path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        digest.update(child.read_bytes())
        return digest.hexdigest()[:16]
    except Exception:                                    # noqa: BLE001
        logger.debug("could not fingerprint %s", package, exc_info=True)
        return ""


def _versions() -> dict[str, str]:
    """What produced this, so the corpus knows its own provenance."""
    out: dict[str, str] = {}
    for name in ("torch", "librosa", "numpy"):
        try:
            out[name] = __import__(name).__version__
        except Exception:                                # noqa: BLE001
            pass
    try:
        import subprocess                                # noqa: PLC0415
        from .settings import settings as _s             # noqa: PLC0415
        line = subprocess.run([_s.ffmpeg, "-version"], capture_output=True,
                              text=True, timeout=10).stdout.splitlines()
        if line:
            out["ffmpeg"] = line[0].split(" ")[2]
    except Exception:                                    # noqa: BLE001
        pass
    return out


def work_key(job_id: str, relative: str) -> str:
    """Where one of a job's working files lives.

    Under `work/` rather than beside the video, so the one key a visitor's
    download depends on cannot collide with an intermediate that happens to
    be named the same thing.
    """
    return f"jobs/{job_id}/work/{relative}"


def store_verdict(job_id: str, chosen_package: str) -> str | None:
    """Keep what the recogniser said, and whether the visitor agreed.

    THE MOST VALUABLE THING THIS SITE PRODUCES, and until now it was
    recorded nowhere a corpus could reach. When the recogniser names a piece
    and the visitor then renders against that same piece, a human has
    confirmed the answer. When they override it and pick something else, a
    human has confirmed an error — and a labelled error is worth more than
    ten unlabelled successes.

    Written on the WEB side, after the render finishes, because that is
    where both halves exist: the verdict is in `recognitions`, the choice is
    on the job row, and the worker has never seen either.

    No address, no email, no file name. These objects outlive the job row on
    purpose, and a verdict carrying a visitor's details would quietly undo
    the promise that deleting their recording deletes their data.
    """
    from . import store                                  # noqa: PLC0415

    rows = store.query(
        "SELECT * FROM recognitions WHERE job_id = ? ORDER BY at DESC",
        (job_id,))
    if not rows:
        return None

    latest = rows[0]
    recognised = latest["piece_id"] or ""
    try:
        candidates = json.loads(latest["candidates"] or "[]")
    except (TypeError, ValueError):
        candidates = []

    # Did the visitor go with it? Compared on the PACKAGE the winning
    # candidate resolved to, not on the piece id, because the piece id is
    # the recogniser's vocabulary and the package is what was rendered.
    suggested = next((c.get("package") for c in candidates
                      if c.get("piece_id") == recognised and c.get("package")),
                     None)
    if not recognised:
        agreement = "nothing recognised"
    elif not chosen_package:
        agreement = "unknown"
    elif suggested and suggested == chosen_package:
        agreement = "accepted"
    elif suggested:
        agreement = "overridden"
    else:
        agreement = "no package was offered for the recognised piece"

    verdict = {
        "job": job_id,
        "outcome": latest["outcome"],
        "recognised_piece": recognised,
        "recognised_title": latest["title"],
        "confidence": latest["confidence"],
        "consensus": latest["consensus"],
        "windows": latest["windows"],
        "suggested_package": suggested,
        "chosen_package": chosen_package,
        # accepted | overridden | nothing recognised | unknown
        "agreement": agreement,
        "candidates": candidates,
        "attempts": len(rows),
        "schema": 1,
    }

    key = work_key(job_id, "recognition.json")
    client = _connect()
    if client is None:
        return None
    try:
        client.put_object(Bucket=settings.object_bucket, Key=key,
                          Body=json.dumps(verdict, indent=2).encode(),
                          ContentType="application/json")
    except Exception:                                    # noqa: BLE001
        logger.warning("could not store the verdict for %s", job_id,
                       exc_info=True)
        return None
    logger.info("job %s: recogniser was %s", job_id, agreement)
    return key


def put_tree(folder: pathlib.Path, job_id: str,
             output: pathlib.Path | None = None) -> dict[str, Any]:
    """Store EVERYTHING a job produced, not only the video.

    The alignment, the chroma, the measures, the per-attempt record: 0.2 MB
    against a 131 MB video, measured. They cost nothing to keep and they are
    expensive to recreate — and once the renderer is a machine that is
    destroyed after each job, not keeping them means they are simply gone.

    For the corpus this is the valuable half. A finished video is a thing
    somebody watched once; `measures.data` and `performance.npy` are what a
    future model would be trained on.

    Never fails the job. The video is what was promised and it is stored by
    `put` before this runs; a working file that will not upload is worth a
    line in the log, not a render thrown away.
    """
    stored, failed, total = [], [], 0
    if not folder.is_dir():
        return {"stored": stored, "failed": failed, "bytes": 0}

    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if output is not None and path == output:
            continue                          # already stored, under its own key
        # Part files are a render in progress, not a result.
        if path.suffix == ".part" or path.name.endswith(".part.mp4"):
            continue
        key = work_key(job_id, path.relative_to(folder).as_posix())
        try:
            total += put(path, key)
            stored.append(key)
        except StorageError as exc:
            logger.warning("could not store %s: %s", key, exc)
            failed.append(key)

    logger.info("job %s: stored %d working file(s), %d failed, %d bytes",
                job_id, len(stored), len(failed), total)
    return {"stored": stored, "failed": failed, "bytes": total}
