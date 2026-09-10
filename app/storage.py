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

import logging
import pathlib
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
