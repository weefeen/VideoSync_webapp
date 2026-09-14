"""Align a performance to a score with music_line_extractor's own aligner.

SEPARATION OF CONCERNS. music_line_extractor prepares scores and does the
synchronisation; VideoScoreSync embeds the score into the video in sync.
The web application had drifted from that: it asked VideoScoreSync to do
the alignment too, through chroma matching, and chroma is right about
seventy percent of the time. The extractor's own aligner -- V11_SOD,
score-onset dynamic programming -- is right on everything except
concertos. This runner is how the web application consumes the second
one, so each library does the job it exists for.

Neither library is modified, and neither is imported into the web
process. This runs as a subprocess under the extractor's own interpreter,
which is what `settings` has described as the arrangement all along:

    music_line_extractor supplies auto-synchronisation. It is invoked as
    a subprocess through its own CLI and its own interpreter, never
    imported -- so its repo is untouched and its dependencies stay its own.

WHAT IS DIFFERENT ABOUT IT. The chroma path warped the visitor's
recording onto the package's REFERENCE RECORDING, so every package had to
carry a reference performance and its chroma -- 111 MB of audio for the
Ballade, and a reference pass for every new piece. This aligns the
visitor's recording against THE SCORE ITSELF. A package needs its score
and nothing else, and there is no reference for a performance to be
judged against other than the music.

Usage mirrors `tools/sync_runner.py` so `app/sync.py` can call either:

    python tools/mle_sync_runner.py --mle-root DIR --package DIR
        --audio FILE --out FILE --work DIR [--status FILE]

The status file carries the same JSON payload the chroma runner emits, so
the safety checks in `app/sync.py` read one shape whichever ran.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time
import traceback

CONFIG = "config"
FAILURE = "failure"

# The score, in the order the extractor's parser prefers. It routes on the
# extension, so all three work; a package ships whichever it was made from.
SCORE_NAMES = ("source.krn", "source.musicxml", "source.mxl")


class ConfigProblem(RuntimeError):
    """This machine cannot align anything. Not the recording's fault."""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mle-root", required=True,
                   help="the music_line_extractor checkout")
    p.add_argument("--package", required=True, help="the score package")
    p.add_argument("--audio", required=True,
                   help="the performance, video or audio")
    p.add_argument("--out", required=True,
                   help="where to write measures.data for the renderer")
    p.add_argument("--work", required=True, help="scratch directory")
    p.add_argument("--status", help="where to write the JSON result")
    return p.parse_args()


def _score_in(package: pathlib.Path) -> pathlib.Path:
    """The package's own score. Everything else is derived from it."""
    for name in SCORE_NAMES:
        for where in (package / "score" / name, package / name):
            if where.is_file():
                return where
    raise ConfigProblem(
        f"{package.name} ships none of {', '.join(SCORE_NAMES)} under "
        f"score/ -- without the score there is nothing to align against. "
        f"This package was built for the old chroma path, which aligned "
        f"against a reference recording instead.")


def _audio_for(media: pathlib.Path, work: pathlib.Path, mle: pathlib.Path
               ) -> pathlib.Path:
    """A wav of the performance, however it arrived.

    Through the extractor's OWN extraction service rather than a hand-
    rolled ffmpeg call: the transcription that follows is sensitive to
    what it is fed, and the extractor aligning its own packages uses this
    service. Two different preprocessings would make our results differ
    from the ones every package was validated with, for no reason.
    """
    if media.suffix.lower() == ".wav":
        return media
    out = work / "performance.wav"
    if out.is_file() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        from services.audio_extraction_service import extract_audio
    except ImportError as exc:
        raise ConfigProblem(
            f"music_line_extractor's audio services could not be imported "
            f"from {mle}: {exc}") from exc
    # `pcm_s16le` EXPLICITLY, exactly as the extractor's own auto_sync
    # workflow passes it. Left to itself the service AUTO-DETECTS and
    # stream-copies an AAC track straight through, producing a file named
    # .wav that holds AAC -- which the transcriber reads as almost
    # nothing. Measured on the Ballade: 7 notes found instead of 4981, so
    # every bar landed on the same second. It is not a preference.
    extract_audio(str(media), str(out), audio_codec="pcm_s16le")
    if not out.is_file() or out.stat().st_size == 0:
        raise ConfigProblem(f"No audio could be taken from {media.name}.")
    return out


# ONE PARSER, SHARED WITH THE OTHER RUNNER. Two column orders exist
# upstream -- an exported package writes `<seconds> <measure> <n> <n>`,
# the competition pipeline writes `<measure> <seconds>` -- and
# `sync_runner.read_measures` already decides per file by looking at which
# column is whole. A second copy here got it wrong in the obvious way,
# reading a measure number as a timestamp: the alignment was perfect and
# the span check refused it anyway, reporting a 263 second reference for a
# piece that runs 570. Importing the one that works means the two runners
# cannot drift apart on it.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from sync_runner import read_measures as _read_measures    # noqa: E402


def _measures_from(path: pathlib.Path) -> list[tuple[int, float]]:
    """(measure, seconds), in measure order."""
    rows = _read_measures(path)
    rows.sort(key=lambda r: r[0])
    return rows


def _reference_span(package: pathlib.Path) -> float:
    """How long the package's own reference performance ran, if it has one.

    Only used for the span check, and only when a reference is present.
    Score-based alignment does not need one, so a package built without it
    is aligned on the strength of crowding alone -- which is the primary
    signal anyway, because it counts measures with nowhere to go and needs
    nothing to compare against.
    """
    for rel in ("reference/measures.data", "performance/measures.data"):
        where = package / rel
        if not where.is_file():
            continue
        try:
            times = [t for _, t in _read_measures(where)]
        except Exception:                              # noqa: BLE001
            continue
        if len(times) >= 2:
            return max(times) - min(times)
    return 0.0


def align(args: argparse.Namespace) -> dict:
    mle = pathlib.Path(args.mle_root)
    if not (mle / "services" / "auto_sync_harness.py").is_file():
        raise ConfigProblem(
            f"No music_line_extractor at {mle}: it has no "
            f"services/auto_sync_harness.py.")
    sys.path.insert(0, str(mle))

    package = pathlib.Path(args.package)
    work = pathlib.Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    score = _score_in(package)
    media = pathlib.Path(args.audio)
    if not media.is_file():
        raise ConfigProblem(f"No such recording: {media}")

    started = time.time()
    wav = _audio_for(media, work, mle)
    extracted = time.time()

    try:
        from services import auto_sync_harness as harness
    except ImportError as exc:
        raise ConfigProblem(
            f"music_line_extractor's aligner could not be imported from "
            f"{mle}: {exc}") from exc

    out_dir = work / "aligned"
    # THE PREPARED SCORE IS READ FROM THE PACKAGE WHEN IT HAS ONE. Parsing
    # a score is the slowest thing here after transcription, and the
    # package already carries what the extractor produced when it was
    # built. Given a package without it the harness parses into the
    # scratch directory instead, so this is an optimisation and not a
    # requirement.
    # THE `score` FOLDER, NOT `score/prepared`. The harness appends
    # `prepared/prepared_score.pkl` to whatever it is given, so pointing at
    # the prepared folder made it look one level too deep, find nothing,
    # and re-parse the whole score on EVERY job -- leaving a stray
    # `score/prepared/prepared/` behind as the evidence.
    score_dir = package / "score"
    result = harness.run(
        score, wav,
        cache_dir=work / "cache",
        out_dir=out_dir,
        score_prepared_root=score_dir if score_dir.is_dir() else None,
        progress=lambda m: print(m, flush=True))
    aligned = time.time()

    written = out_dir / "measures.data"
    if not written.is_file():
        raise RuntimeError(
            f"The aligner reported {len(result.measures)} measures but "
            f"wrote no measures.data to {out_dir}.")

    rows = _measures_from(written)
    if not rows:
        raise RuntimeError(f"{written} held no readable measures.")

    # Into the renderer's own format: measure first, space separated.
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{m} {t}\n" for m, t in rows), encoding="utf-8")

    times = [t for _, t in rows]
    crowded = sum(1 for a, b in zip(times, times[1:]) if abs(b - a) < 1e-9)
    crowding = crowded / len(times) if times else 0.0

    ref_span = _reference_span(package)
    span = times[-1] - times[0]
    # 1.0, not 0.0, when there is no reference: the span check is skipped
    # rather than failed. `app/sync.py` tests a RANGE around 1.0, so this
    # is the value that means "this signal has nothing to say".
    span_ratio = (span / ref_span) if ref_span > 0 else 1.0

    confidences = list(result.confidences or [])
    mean_conf = (sum(confidences) / len(confidences)) if confidences else None

    return {
        "ok": True,
        "aligner": "v11_sod",
        "measures": len(rows),
        "first_measure": rows[0][0], "last_measure": rows[-1][0],
        "starts_at": round(rows[0][1], 3), "ends_at": round(rows[-1][1], 3),
        "crowding": round(crowding, 4),
        "crowded_measures": crowded,
        "span_ratio": round(span_ratio, 4),
        "span_seconds": round(span, 2),
        "reference_span_seconds": round(ref_span, 2),
        # THE ALIGNER'S OWN VERDICT, which the chroma path had no equivalent
        # of. `monotonic` false means the bars do not run forwards, which is
        # a wrong alignment however plausible the timings look.
        "quality": {
            "monotonic": bool(result.monotonic),
            "pairs": int(result.n_pairs),
            "anomalies": int(result.n_anomalies),
            "repaired": int(result.n_repaired),
            "mean_confidence": (round(mean_conf, 4)
                                if mean_conf is not None else None),
        },
        "score": str(score),
        "timing": {
            "audio_s": round(extracted - started, 2),
            "align_s": round(aligned - extracted, 2),
            "total_s": round(aligned - started, 2),
        },
    }


def main() -> int:
    args = parse_args()
    try:
        result = align(args)
    except ConfigProblem as exc:
        result = {"ok": False, "kind": CONFIG, "error": str(exc)}
    except Exception as exc:                           # noqa: BLE001
        result = {"ok": False, "kind": FAILURE,
                  "error": f"{type(exc).__name__}: {exc}".rstrip(": "),
                  "traceback": traceback.format_exc()}
    if args.status:
        status = pathlib.Path(args.status)
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if not result.get("ok"):
        print(result.get("error", "alignment failed"), file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
