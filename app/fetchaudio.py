"""Turning a YouTube link into a file on this disk, and nothing else.

This is the one genuinely new capability the watch pipeline needs.
Everything after it — `identify`, `align`, the states in `watch` — already
works on a local media file and does not care where that file came from.
So this module is deliberately small and deliberately the only place that
knows about yt-dlp: swapping it for a different method, or for a licensed
feed, should touch this file and no other.

AUDIO ONLY, AND IT IS KEPT. The pipeline needs sound, not pictures: the
recogniser listens and the aligner listens, and the pianist's own video is
what plays on the watch page, streamed from YouTube's player. We never
show, re-encode or publish a frame of it. Downloading the video track
would cost ten times the bytes to produce nothing anybody sees.

The file is kept after identification, because a piece we cannot align
today is one we will align the moment its score is engraved, and throwing
the audio away would mean asking YouTube for it a second time.

WHAT THIS MODULE IS NOT ALLOWED TO DECIDE: whether a string is a YouTube
link. `app/youtube.py` is the reader, here and everywhere.
"""
from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess
import sys
import time

from . import settings as settings_mod

logger = logging.getLogger(__name__)

settings = settings_mod.settings


class FetchError(RuntimeError):
    """The audio could not be obtained. The message is for an operator."""


class FetchUnavailable(FetchError):
    """yt-dlp is not installed here, so this machine cannot fetch at all.

    Separate from FetchError because it is a DEPLOYMENT fault, not a fault
    of the video: retrying it on the same host will fail identically, and
    a performance must not be marked FAILED for it.
    """


class VideoUnusable(FetchError):
    """The video exists but cannot be used: private, removed, age-gated.

    Distinct from a transport failure for the same reason as above — this
    one is final, and the performance should go UNAVAILABLE rather than be
    retried forever.
    """


#: Read once. `shutil.which` is a directory walk, and the answer does not
#: change while the process lives.
_BINARY: "str | None | bool" = False


def binary() -> str | None:
    """Where yt-dlp is, or None.

    Prefers the module in this interpreter over a loose executable: on the
    web box they are the same install, and on a developer machine the
    module is the one the virtualenv pins while `yt-dlp` on PATH may be
    something a package manager put there years ago.
    """
    global _BINARY
    if _BINARY is not False:
        return _BINARY  # type: ignore[return-value]
    try:
        import yt_dlp  # noqa: F401
        _BINARY = sys.executable + " -m yt_dlp"
    except ImportError:
        found = shutil.which("yt-dlp")
        _BINARY = found
    return _BINARY  # type: ignore[return-value]


def _argv() -> list[str]:
    where = binary()
    if not where:
        raise FetchUnavailable(
            "yt-dlp is not installed here, so a link cannot be fetched. "
            "pip install -r requirements.txt")
    return where.split(" ") if " -m " in where else [where]


def version() -> str:
    """The yt-dlp version string, or '' if it cannot be asked.

    Used by the daily update check and by `doctor`. Never raises: a
    version that cannot be read is a thing to report, not to crash on.
    """
    try:
        done = subprocess.run(_argv() + ["--version"], capture_output=True,
                              text=True, timeout=60)
        return (done.stdout or "").strip()
    except (FetchUnavailable, OSError, subprocess.SubprocessError):
        return ""


# Things yt-dlp says when the VIDEO is the problem rather than the network.
# Matched on the message because yt-dlp's exit code is 1 for everything it
# ever fails at, so the text is the only signal there is.
#
# BOTH WORDINGS OF "unavailable" ARE HERE ON PURPOSE. yt-dlp says "Video
# unavailable" in some paths and "This video is unavailable" in others, and
# a list carrying only the first classified a dead link as retryable — so a
# video that will never exist again would have been fetched, failed and
# requeued forever. Caught by the test below; do not tidy either one away.
_FINAL = (
    "video unavailable", "video is unavailable", "is unavailable",
    "private video", "removed by the uploader", "video has been removed",
    "account associated with this video has been terminated",
    "is not available in your country", "who has blocked it",
    "sign in to confirm your age", "age-restricted",
    "members-only", "join this channel", "this live event",
    "video is not available", "does not exist",
)


def probe(video_id: str, *, timeout: int = 90) -> dict:
    """What YouTube says about a video, without downloading it.

    Returns {'title', 'uploader', 'duration'} — duration in seconds, 0 when
    YouTube did not say. Raises VideoUnusable when the video itself is the
    problem, FetchError when the attempt was.

    Cheap on purpose: it runs before the download so a two-hour upload or a
    dead link costs one metadata request instead of a file.
    """
    import json
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        done = subprocess.run(
            _argv() + ["--no-warnings", "--skip-download", "--dump-single-json", url],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"asking about {video_id} timed out") from exc
    except OSError as exc:
        raise FetchUnavailable(f"could not run yt-dlp: {exc}") from exc

    if done.returncode != 0:
        raise _classify(video_id, done.stderr or "")
    try:
        meta = json.loads(done.stdout or "{}")
    except ValueError as exc:
        raise FetchError(f"yt-dlp said something unreadable about {video_id}") from exc
    return {
        "title": (meta.get("title") or "").strip(),
        "uploader": (meta.get("uploader") or meta.get("channel") or "").strip(),
        # `duration` is absent on a live stream and on some premieres.
        "duration": float(meta.get("duration") or 0),
    }


def audio(video_id: str, into: pathlib.Path, *, timeout: int = 900) -> pathlib.Path:
    """Download the sound of a video into `into`, returning the file.

    `into` is a DIRECTORY, created if need be, and the file lands in it as
    `<video id>.<ext>` — named by the video rather than by the job, because
    the same recording asked for twice is one file and the id is the only
    stable name a video has.

    The format is whatever YouTube's best audio-only stream is, left in its
    own container. Neither ffmpeg nor a re-encode is asked for here:
    `identify` and `align` both hand the file to ffmpeg themselves, and
    transcoding on the way in would lose a little quality to save nothing.
    """
    into.mkdir(parents=True, exist_ok=True)
    existing = _already(into, video_id)
    if existing is not None:
        logger.info("%s: audio already here (%s)", video_id, existing.name)
        return existing

    url = f"https://www.youtube.com/watch?v={video_id}"
    template = str(into / f"{video_id}.%(ext)s")
    started = time.monotonic()
    try:
        done = subprocess.run(
            _argv() + [
                "--no-warnings", "--no-playlist",
                # Audio only. `bestaudio` alone can pick a stream ffmpeg
                # dislikes; the fallback keeps the common m4a path open.
                "-f", "bestaudio[ext=m4a]/bestaudio/best",
                "--output", template,
                # Retries are yt-dlp's own, which understands which of its
                # failures are worth repeating. A loop out here would
                # repeat the ones that are not.
                "--retries", "3", "--fragment-retries", "3",
                url,
            ],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FetchError(f"downloading {video_id} timed out") from exc
    except OSError as exc:
        raise FetchUnavailable(f"could not run yt-dlp: {exc}") from exc

    if done.returncode != 0:
        raise _classify(video_id, done.stderr or "")

    got = _already(into, video_id)
    if got is None:
        # yt-dlp reported success and produced nothing. Seen when a format
        # selector matches nothing at all, and it is not a video fault.
        raise FetchError(f"yt-dlp finished but left no file for {video_id}")
    logger.info("%s: %.1f MB of audio in %.0fs", video_id,
                got.stat().st_size / 1e6, time.monotonic() - started)
    return got


def _already(into: pathlib.Path, video_id: str) -> pathlib.Path | None:
    """The audio for this video if it is already on disk.

    `.part` is yt-dlp's half-finished download and must never be offered as
    a complete file: the aligner would read a truncated recording and
    produce a confident, wrong answer.
    """
    if not into.is_dir():
        return None
    for path in sorted(into.glob(f"{video_id}.*")):
        if path.suffix in (".part", ".ytdl", ".tmp"):
            continue
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _classify(video_id: str, stderr: str) -> FetchError:
    low = stderr.lower()
    for phrase in _FINAL:
        if phrase in low:
            return VideoUnusable(f"{video_id}: {phrase}")
    # A first line is usually the whole story; the rest is a traceback.
    first = next((ln for ln in stderr.splitlines() if ln.strip()), "")
    return FetchError(f"{video_id}: {first.strip() or 'yt-dlp failed'}")
