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
indexes, then 28-40 s of work. One call per upload pays both. A resident
worker would save the first half, and music_finrgerprint would need a
`serve` mode for that — worth having once the wait starts to matter, and
deliberately not required to get this working.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import subprocess
import tempfile

from .settings import settings

RUNNER = pathlib.Path(__file__).resolve().parent.parent / "tools" / "identify_runner.py"

# The recogniser's own open-set gate admits anything at consensus >= 0.5.
# Measured here, in-corpus recordings land at 0.88-1.00 and a recording of
# something else entirely at 0.25 — so 0.5 separates the two populations
# with room to spare. Between 0.5 and this line we have a piece the
# recogniser accepted but the windows disagreed about, which is a question
# to put to the person rather than an answer to give them.
SURE = 0.75

MATCHED = "matched"              # say what it is, and move on
AMBIGUOUS = "ambiguous"          # "did you mean", with the alternatives
UNRECOGNISED = "unrecognised"    # ask them to choose from the library


class IdentifyError(RuntimeError):
    """Recognition failed. The message is safe to show the user."""


class IdentifyUnavailable(IdentifyError):
    """This install cannot identify anything; a configuration problem."""


class TooShort(IdentifyError):
    """The recording is too short for the confidence gate to mean anything."""


@dataclasses.dataclass(frozen=True)
class Candidate:
    rank: int
    piece_id: str
    score: float
    score_names: list[str]        # the editions this recording backs

    def public(self) -> dict:
        return {"rank": self.rank, "piece_id": self.piece_id,
                "score": self.score, "score_names": list(self.score_names)}


@dataclasses.dataclass(frozen=True)
class Identification:
    mode: str                     # the library's verdict
    winner: str | None
    consensus: float
    coverage: float
    n_windows: int
    candidates: list[Candidate]
    timing: dict

    @property
    def outcome(self) -> str:
        """What the interface should do about this, which is our decision.

        `mode` is the recogniser's judgement and we do not second-guess it;
        the split between saying and asking is a product choice, made here.
        """
        if self.mode != "confident" or not self.winner:
            return UNRECOGNISED
        return MATCHED if self.consensus >= SURE else AMBIGUOUS

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


def duration_of(media: pathlib.Path) -> float | None:
    """Length in seconds, or None if it cannot be determined.

    Deliberately not `render.probe`, which requires a video stream and
    raises on audio-only input. The length gate is a safety property, so
    it must not depend on the file happening to carry pictures.
    """
    try:
        done = subprocess.run(
            [settings.ffprobe, "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(media)],
            capture_output=True, text=True, timeout=60)
        return float(done.stdout.strip()) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def identify(media: pathlib.Path, duration: float | None = None) -> Identification:
    """Identify the piece played in a recording.

    `duration` is measured here when the caller does not supply it, so the
    length gate always applies. Skipping it silently would be the worst of
    both worlds: a clip too short for consensus to mean anything, reported
    as a confident match.
    """
    if not settings.can_identify:
        raise IdentifyUnavailable(
            "Recognition isn't configured. " + settings.why_cannot_identify())
    if not media.is_file():
        raise IdentifyError(f"No such recording: {media}")

    if duration is None:
        duration = duration_of(media)
    if duration is not None and duration < settings.identify_min_seconds:
        raise TooShort(
            f"That recording is {duration:.0f} seconds long. Below "
            f"{settings.identify_min_seconds:.0f} seconds there isn't enough "
            f"to tell one piece from another with any confidence.")

    with tempfile.TemporaryDirectory(prefix="svs_id_") as tmp:
        out = pathlib.Path(tmp) / "verdict.json"
        command = [
            settings.id_python, "-u", str(RUNNER), str(media),
            "--root", str(settings.id_root),
            "--index-dir", str(settings.id_index_dir),
            "--out", str(out),
        ]
        if settings.pair_list:
            command += ["--pair-list", str(settings.pair_list)]

        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                  timeout=settings.identify_timeout)
        except subprocess.TimeoutExpired as exc:
            raise IdentifyError(
                f"Recognition gave up after "
                f"{settings.identify_timeout:.0f} seconds.") from exc
        except OSError as exc:
            raise IdentifyUnavailable(
                f"Could not run the recogniser with {settings.id_python!r}: "
                f"{exc}") from exc

        if not out.is_file():
            raise IdentifyError(_failure(done))
        payload = json.loads(out.read_text(encoding="utf-8"))

    if not payload.get("ok"):
        raise IdentifyError(payload.get("error") or "Recognition failed.")

    return Identification(
        mode=payload["mode"],
        winner=payload.get("winner"),
        consensus=float(payload.get("consensus") or 0.0),
        coverage=float(payload.get("coverage") or 0.0),
        n_windows=int(payload.get("n_windows") or 0),
        candidates=[Candidate(rank=c["rank"], piece_id=c["piece_id"],
                              score=c["score"],
                              score_names=list(c.get("score_names") or []))
                    for c in payload.get("candidates", [])],
        timing=payload.get("timing") or {},
    )


def _failure(done: subprocess.CompletedProcess) -> str:
    """Explain a run that produced no verdict, using whatever it did say."""
    tail = (done.stderr or done.stdout or "").strip().splitlines()
    detail = tail[-1] if tail else "no output"
    return (f"The recogniser exited with code {done.returncode} without "
            f"producing a result: {detail}")
