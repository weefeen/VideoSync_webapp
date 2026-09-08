"""Name the piece in an uploaded recording, via music_finrgerprint.

We do not implement recognition. music_finrgerprint does, and is reached
as a **subprocess with its own interpreter** — never imported. That is not
a style choice: identifying runs a neural piano transcription on the GPU,
so it needs torch and CUDA, and this app must stay free of both. The two
repositories share no code, and music_finrgerprint is never modified.

What comes back is a ranked list of recordings, each carrying the score
editions it backs. Turning those editions into something renderable is
`library.py`'s job, not this module's; here the answer stops at "which
recording, how sure".

    identify(video) -> Identification

Cost, measured on an RTX 2000 Ada: about 17 s to load torch and the two
indexes, then 35-55 s of work. One call per upload pays both, and calls
are serialised because there is one GPU. A resident worker would save the
first half and needs a `serve` mode upstream; deliberately not required to
get this working.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import subprocess
import tempfile
import threading

from .settings import settings

logger = logging.getLogger(__name__)

RUNNER = pathlib.Path(__file__).resolve().parent.parent / "tools" / "identify_runner.py"

# One GPU, one identification at a time. Two uploads a minute apart would
# otherwise put two torch processes on the same card, and the second gets
# a slower answer or an out-of-memory failure rather than a queue.
_gpu = threading.BoundedSemaphore(1)

# The recogniser's consensus gate was swept over 16 recordings (see
# MIN_CONSENSUS in its aggregate.py): in-corpus 62-100%, unknown 12-38%,
# threshold 0.50 at the midpoint. We do not second-guess that number — an
# earlier attempt here to demand 0.75 would have demoted a genuine 5-of-8
# match to a guess. What we add is a check the library cannot make: whether
# enough windows were voted on for its calibration to apply at all.
FULL_WINDOWS = 8          # what the sweep was run at; see MAX_WINDOWS upstream
MIN_WINDOWS = 2           # below this, consensus is arithmetic, not evidence

MATCHED = "matched"              # say what it is, and move on
UNRECOGNISED = "unrecognised"    # let them pick from the library instead


class IdentifyError(RuntimeError):
    """Recognition failed. The message is safe to show the user."""


class IdentifyUnavailable(IdentifyError):
    """This install cannot identify anything; a configuration problem."""


class TooShort(IdentifyError):
    """The recording is too short for the confidence gate to mean anything."""


class NoAudio(IdentifyError):
    """There is nothing to listen to."""


@dataclasses.dataclass(frozen=True)
class Candidate:
    piece_id: str
    score: float
    score_names: list[str]        # the editions this recording backs

    def public(self) -> dict:
        return {"piece_id": self.piece_id, "score": self.score,
                "score_names": list(self.score_names)}


@dataclasses.dataclass(frozen=True)
class Identification:
    mode: str                     # confident | not_in_collection | abstain
    winner: str | None
    consensus: float
    coverage: float
    n_windows: int
    candidates: list[Candidate]
    timing: dict

    @property
    def outcome(self) -> str:
        """What the interface should do about this.

        `mode` is the recogniser's judgement and we take it as given. The
        window count is ours to judge: the gate was calibrated at eight
        windows, so with fewer we hold the answer to unanimity rather than
        pretend a threshold swept elsewhere still applies here.
        """
        if self.mode != "confident" or not self.winner:
            return UNRECOGNISED
        if self.n_windows < MIN_WINDOWS:
            return UNRECOGNISED
        if self.n_windows < FULL_WINDOWS and self.consensus < 1.0:
            return UNRECOGNISED
        return MATCHED

    def public(self) -> dict:
        return {
            "outcome": self.outcome,
            "mode": self.mode,
            "winner": self.winner,
            "consensus": self.consensus,
            "coverage": self.coverage,
            "n_windows": self.n_windows,
            "candidates": [c.public() for c in self.candidates],
            "timing": dict(self.timing),
        }


def probe_media(media: pathlib.Path) -> tuple[float | None, bool]:
    """(seconds, has an audio stream). Length is None when unknowable.

    Deliberately not `render.probe`, which requires a *video* stream and
    raises on audio-only input. The length gate is a safety property, so it
    must not depend on the file happening to carry pictures.
    """
    def ask(*args: str) -> str:
        try:
            done = subprocess.run(
                [settings.ffprobe, "-v", "error", *args, str(media)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60)
            return done.stdout.strip() if done.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    raw = ask("-show_entries", "format=duration", "-of", "csv=p=0")
    try:
        duration = float(raw)
    except ValueError:
        duration = None
    has_audio = bool(ask("-select_streams", "a:0", "-show_entries",
                         "stream=codec_type", "-of", "csv=p=0"))
    return duration, has_audio


def identify(media: pathlib.Path, duration: float | None = None) -> Identification:
    """Identify the piece played in a recording."""
    if not settings.can_identify:
        raise IdentifyUnavailable(
            "Recognition isn't configured. " + settings.why_cannot_identify())
    if not media.is_file():
        raise IdentifyError(f"No such recording: {media}")

    measured, has_audio = probe_media(media)
    duration = duration if duration is not None else measured

    if not has_audio:
        raise NoAudio(
            "That file has no audio track. The piece is recognised from the "
            "sound, so there is nothing here to listen to.")
    if duration is None:
        # Refusing is the only safe answer: without a length we cannot tell
        # whether the recogniser will get enough windows for its confidence
        # to mean anything, and a wrong score is worse than no score.
        raise IdentifyError(
            "The length of that recording could not be read, so it cannot be "
            "checked. If it plays elsewhere, try exporting it again.")
    if duration < settings.identify_min_seconds:
        raise TooShort(
            f"That recording is {duration:.0f} seconds long. Below "
            f"{settings.identify_min_seconds:.0f} seconds there isn't enough "
            f"to tell one piece from another with any confidence.")

    with _gpu:
        payload = _run(media)

    if not payload.get("ok"):
        logger.error("recogniser failed on %s: %s\n%s", media.name,
                     payload.get("error"), payload.get("traceback", ""))
        message = payload.get("error") or "Recognition failed."
        # A configuration problem is not this upload's fault and retrying
        # will not help, so it is reported as such rather than as a failure.
        if payload.get("kind") == "config":
            raise IdentifyUnavailable(message)
        raise IdentifyError(message)

    return Identification(
        mode=payload["mode"],
        winner=payload.get("winner"),
        consensus=float(payload.get("consensus") or 0.0),
        coverage=float(payload.get("coverage") or 0.0),
        n_windows=int(payload.get("n_windows") or 0),
        candidates=[Candidate(piece_id=c["piece_id"], score=c["score"],
                              score_names=list(c.get("score_names") or []))
                    for c in payload.get("candidates", [])],
        timing=payload.get("timing") or {},
    )


def _run(media: pathlib.Path) -> dict:
    """Call the runner and return its verdict payload."""
    with tempfile.TemporaryDirectory(prefix="svs_id_") as tmp:
        out = pathlib.Path(tmp) / "verdict.json"
        command = [
            settings.id_python, "-u", str(RUNNER), str(media),
            "--root", str(settings.id_root),
            "--pitch-index", str(settings.pitch_index),
            "--chord-index", str(settings.chord_index),
            "--out", str(out),
        ]
        if settings.pair_list:
            command += ["--pair-list", str(settings.pair_list)]

        # The child prints the transcription checkpoint path, which contains
        # the home directory; on a console codepage that cannot encode it,
        # the run dies before doing any work. Force UTF-8 both ways.
        #
        # DONTWRITEBYTECODE because the child imports from a repository we
        # are only ever allowed to read: without it, running this leaves
        # __pycache__ directories behind inside music_finrgerprint.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env,
                                  timeout=settings.identify_timeout)
        except subprocess.TimeoutExpired as exc:
            raise IdentifyError(
                f"Recognition gave up after {settings.identify_timeout:.0f} "
                f"seconds. Without a GPU it takes many times longer than "
                f"that, which is the usual cause.") from exc
        except OSError as exc:
            raise IdentifyUnavailable(
                f"Could not run the recogniser with {settings.id_python!r}: "
                f"{exc}") from exc

        if not out.is_file():
            logger.error("recogniser produced no verdict for %s\nstderr:\n%s",
                         media.name, (done.stderr or "")[-4000:])
            raise IdentifyError(_failure(done))
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IdentifyError(
                f"The recogniser's result could not be read back: {exc}") from exc


def _failure(done: subprocess.CompletedProcess) -> str:
    """Explain a run that produced no verdict, using whatever it did say."""
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else "no output"
    if "No module named" in detail:
        detail += (" — the recogniser's interpreter is missing a dependency; "
                   "check ID_PYTHON points at the environment that has torch")
    return (f"The recogniser exited with code {done.returncode} without "
            f"producing a result: {detail}")
