"""Align a performance to a score, via VideoScoreSync.

We do not implement alignment. VideoScoreSync does — `audio_to_chroma`
turns a recording into a chroma matrix, `audio_synchronization_service`
warps it against the package's own reference chroma — and it is reached as
a **subprocess with its own interpreter**, never imported.

That is forced rather than chosen. Chroma extraction aborts the whole
process under the interpreter this app runs on:

    LLVM ERROR: Symbol not found: __svml_cosf8_ha

An abort cannot be caught, so an in-process call would take the web server
down with it. Measured: about 13 s of chroma and under a second of warping
for a seven-minute recording.

music_line_extractor takes no part here. It builds the score packages
offline; at run time we only read what it produced.

Every job aligns the recording it was given. A package's own
measures.data belongs to *its* reference performance, not to whatever
someone uploaded, so it is the thing we align against — never the answer.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import subprocess

from .settings import settings

logger = logging.getLogger(__name__)

RUNNER = pathlib.Path(__file__).resolve().parent.parent / "tools" / "sync_runner.py"


class SyncError(RuntimeError):
    """Alignment failed. The message is safe to show the user."""


class SyncUnavailable(SyncError):
    """This install cannot align anything; a configuration problem."""


# The warper aligns the whole score against the whole recording. Given a
# recording that covers only part of the piece, it places the music that is
# there correctly and crams the rest into whatever time is left — so the
# video looks right, then flips through hundreds of bands at the end.
#
# Measured on the same recording, complete and cut to one minute:
#
#                        complete    one-minute excerpt
#     crowding             0.000           0.790
#     span_ratio           1.000           0.132
#
# Two independent signals, both far from their limits. Crowding is the
# primary one — it directly counts measures with nowhere to go and needs no
# reference. A little is legitimate: the chroma hop is 0.1 s, so genuinely
# fast measures can land in the same frame.
MAX_CROWDING = 0.05
# Tempo differs between performers, but the extent of the music does not.
# Wide enough to admit an unusually slow or brisk reading, narrow enough
# that half a piece cannot pass.
SPAN_RANGE = (0.5, 2.0)


class PartialRecording(SyncError):
    """The recording stops before the score does."""


@dataclasses.dataclass(frozen=True)
class Alignment:
    """Where every measure falls in the uploaded performance."""
    measures_path: pathlib.Path
    measures: int
    first_measure: int
    last_measure: int
    starts_at: float
    ends_at: float
    crowding: float
    span_ratio: float
    timing: dict

    def public(self) -> dict:
        return {"measures": self.measures,
                "first_measure": self.first_measure,
                "last_measure": self.last_measure,
                "starts_at": self.starts_at, "ends_at": self.ends_at,
                "crowding": self.crowding, "span_ratio": self.span_ratio,
                "timing": dict(self.timing)}


def align(package_root: pathlib.Path, media: pathlib.Path,
          job_dir: pathlib.Path) -> Alignment:
    """Align `media` against `package_root`, writing measures into `job_dir`.

    The score package is only ever read: its reference recording is what
    makes alignment possible, so writing there would destroy the very thing
    the next job needs.
    """
    if not settings.can_sync:
        raise SyncUnavailable(
            "Alignment isn't configured. " + settings.why_cannot_sync())
    if not media.is_file():
        raise SyncError(f"No such recording: {media}")
    if not package_root.is_dir():
        raise SyncError(f"No such score package: {package_root}")

    job_dir.mkdir(parents=True, exist_ok=True)
    work = job_dir / "sync"
    measures = job_dir / "measures.data"
    status = work / "result.json"

    command = [
        settings.sync_python, "-u", str(RUNNER),
        "--vss-root", str(settings.vss_root),
        "--package", str(package_root),
        "--audio", str(media),
        "--out", str(measures),
        "--work", str(work),
        "--status", str(status),
    ]
    # DONTWRITEBYTECODE because the child imports from a repository we are
    # only ever allowed to read: without it, running this leaves __pycache__
    # directories behind inside VideoScoreSync, which even tracks its own.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONDONTWRITEBYTECODE": "1"}

    # A previous run in this folder must not be mistaken for this one: an
    # abort writes no status at all, and a stale success would be read as
    # though it belonged to the recording we just handed over.
    for leftover in (status, measures):
        leftover.unlink(missing_ok=True)

    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env,
                              timeout=settings.sync_timeout)
    except subprocess.TimeoutExpired as exc:
        raise SyncError(
            f"Alignment gave up after {settings.sync_timeout:.0f} seconds."
        ) from exc
    except OSError as exc:
        raise SyncUnavailable(
            f"Could not run the aligner with {settings.sync_python!r}: {exc}"
        ) from exc

    payload = _status(status, done)
    if not payload.get("ok"):
        logger.error("aligner failed on %s: %s\n%s", media.name,
                     payload.get("error"), payload.get("traceback", ""))
        message = payload.get("error") or "Alignment failed."
        raise (SyncUnavailable if payload.get("kind") == "config"
               else SyncError)(message)

    if not measures.is_file():
        raise SyncError("Alignment reported success but wrote no measures.")

    crowding = float(payload.get("crowding", 0.0))
    crowded = int(payload.get("crowded_measures", 0))
    span_ratio = float(payload.get("span_ratio", 1.0))
    low, high = SPAN_RANGE

    if crowding > MAX_CROWDING:
        raise PartialRecording(
            f"That recording covers only part of the piece — {crowded} of "
            f"{payload['measures']} measures had to share a moment with the "
            f"one before. The score is aligned against the whole "
            f"performance, so please upload the piece complete.")
    if not low <= span_ratio <= high:
        played = payload.get("span_seconds", 0)
        expected = payload.get("reference_span_seconds", 0)
        raise PartialRecording(
            f"That recording runs {played:.0f} seconds where this piece runs "
            f"about {expected:.0f}. It does not look like the whole work, "
            f"and the score can only be aligned to a complete performance.")

    logger.info("aligned %s: %d measures in %.1fs", media.name,
                payload["measures"], payload["timing"]["total_s"])
    return Alignment(
        measures_path=measures,
        measures=payload["measures"],
        first_measure=payload["first_measure"],
        last_measure=payload["last_measure"],
        starts_at=payload["starts_at"],
        ends_at=payload["ends_at"],
        crowding=crowding,
        span_ratio=span_ratio,
        timing=payload.get("timing") or {},
    )


def _status(status: pathlib.Path, done: subprocess.CompletedProcess) -> dict:
    """The runner's own account of what happened, or a reconstruction.

    A process that aborts rather than raises leaves no status file at all,
    which is exactly what an uncatchable LLVM error looks like — so say
    that plainly instead of reporting a bare exit code.
    """
    if status.is_file():
        try:
            return json.loads(status.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    output = (done.stderr or "") + (done.stdout or "")
    if "LLVM ERROR" in output or "Symbol not found" in output:
        return {"ok": False, "kind": "config", "error": (
            "The aligner's interpreter cannot run chroma extraction — its "
            "numba build is missing Intel's vector math library. Point "
            "SYNC_PYTHON at an environment where it works.")}
    tail = output.strip().splitlines()
    return {"ok": False, "kind": "failure", "error": (
        f"The aligner exited with code {done.returncode} without producing a "
        f"result: {tail[-1] if tail else 'no output'}")}
