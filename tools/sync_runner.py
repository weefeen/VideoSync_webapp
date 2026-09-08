"""Align a performance to a score package. Runs under the audio interpreter.

The web app's side of the boundary with VideoScoreSync, not part of it. It
is executed as a subprocess by `app/sync.py`, because chroma extraction
aborts the process outright under the interpreter the web app runs on:

    LLVM ERROR: Symbol not found: __svml_cosf8_ha

numba wants Intel's vector math library, which is present in the audio
environment and absent in the app's. That abort cannot be caught, so this
has to be a separate process rather than an import.

VideoScoreSync is read only: this adds its root to the path and calls two
services that import nothing but `api_audio`. Importing from a checkout
normally leaves `__pycache__` behind in it — VideoScoreSync even tracks
its own bytecode — so the parent runs this with PYTHONDONTWRITEBYTECODE.
Nothing is written into that repository, or into the score package.

    python sync_runner.py --vss-root R --package P --audio A
                          --out measures.data --work DIR

What the services expect and what a package ships are not the same file:
the service splits on tabs, demands exactly two columns and reads measure
first, while an exported package writes four columns with seconds first.
Translating between the two is this script's main job, and the reason it
exists rather than the CLIs being called directly.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import traceback

CONFIG = "config"
FAILURE = "failure"


class ConfigProblem(RuntimeError):
    """Something this run needs is missing or unusable."""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--vss-root", required=True, help="VideoScoreSync checkout")
    p.add_argument("--package", required=True, help="the score package folder")
    p.add_argument("--audio", required=True, help="the performance to align")
    p.add_argument("--out", required=True, help="where to write measures.data")
    p.add_argument("--work", required=True, help="scratch directory")
    p.add_argument("--status", default="", help="where to write a JSON result")
    return p.parse_args()


def read_measures(path: pathlib.Path) -> list[tuple[int, float]]:
    """(measure, seconds), whichever column order the file uses.

    Two conventions exist upstream and both turn up: an exported package
    writes `<seconds> <measure> <n> <n>`, the competition pipeline writes
    `<measure> <seconds>`. Decide per file rather than assume: measure
    numbers are whole, timestamps carry a fraction.
    """
    rows = [line.split() for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ConfigProblem(f"{path} is empty — this package has no alignment.")

    def integral(index: int) -> bool:
        try:
            return all(float(r[index]).is_integer() for r in rows)
        except (ValueError, IndexError):
            return False

    wide = len(rows[0]) >= 4
    measure_col = 1 if (wide or (not integral(0) and integral(1))) else 0
    time_col = 0 if wide else 1 - measure_col
    try:
        return [(int(float(r[measure_col])), float(r[time_col])) for r in rows]
    except (ValueError, IndexError) as exc:
        raise ConfigProblem(f"{path} could not be parsed: {exc}") from exc


def _first(package: pathlib.Path, *relatives: str) -> pathlib.Path:
    for rel in relatives:
        candidate = package / rel
        if candidate.is_file():
            return candidate
    raise ConfigProblem(
        f"{package.name} has none of {', '.join(relatives)} — without it "
        f"there is nothing to align against.")


def align(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(pathlib.Path(args.vss_root)))
    try:
        from services.audio_synchronization_service import wfn_combination_selector
        from services.audio_to_chroma import audio2chroma
    except ImportError as exc:
        raise ConfigProblem(
            f"VideoScoreSync's audio services could not be imported from "
            f"{args.vss_root}: {exc}") from exc

    package = pathlib.Path(args.package)
    work = pathlib.Path(args.work)
    work.mkdir(parents=True, exist_ok=True)

    # The reference chroma and the reference measures must describe the same
    # recording, and where each lives has moved. music_line_extractor now
    # writes chroma only to performance/ (CHROMA_ARCNAME); the root and
    # score/ copies are in its _ARCHIVE_STALE_ARCNAMES and are dropped on
    # every repack. Reading the legacy copy of a package whose reference was
    # later replaced would align against a recording that no longer exists
    # in it — monotonic, plausible, and wrong. Current homes first.
    ref_chroma = _first(package, "performance/chroma.npy",
                        "reference/chroma.npy", "chroma.npy",
                        "score/chroma.npy")
    ref_measures_file = _first(package, "performance/measures.data",
                               "reference/measures.data",
                               "export/measures.data", "measures.data")
    ref_measures = read_measures(ref_measures_file)
    ref_measures.sort(key=lambda pair: pair[1])
    last_measure = max(m for m, _ in ref_measures)

    # The two must describe the same audio. Chroma frames are 0.1 s apart,
    # so its length is checkable against the last timestamp the measures
    # claim — a stale pairing usually disagrees by minutes.
    import numpy as np
    frames = np.load(ref_chroma, mmap_mode="r").shape[-1]
    chroma_seconds = frames * 0.1
    if chroma_seconds + 5.0 < ref_measures[-1][1]:
        raise ConfigProblem(
            f"{package.name}: {ref_chroma.name} covers {chroma_seconds:.0f}s "
            f"but {ref_measures_file.name} runs to {ref_measures[-1][1]:.0f}s. "
            f"They describe different recordings, so this package cannot be "
            f"aligned against until it is re-exported.")

    # Rewritten into the two tab-separated measure-first columns the
    # service parses; anything else is silently skipped line by line and
    # leaves it with no timestamps at all.
    ref_two_column = work / "reference_measures.data"
    ref_two_column.write_text(
        "\n".join(f"{m}\t{t}" for m, t in ref_measures), encoding="utf-8")

    started = time.perf_counter()
    test_chroma = work / "performance.npy"
    audio2chroma(str(args.audio), str(test_chroma), True)
    chroma_done = time.perf_counter()

    sync_data = wfn_combination_selector(
        ref_chroma_path=str(ref_chroma),
        test_chroma_path=str(test_chroma),
        timestamp_measures_file_path=str(ref_two_column),
        movements=[[1, last_measure]],
    )
    aligned = time.perf_counter()

    if len(sync_data) == 0:
        raise RuntimeError(
            "Alignment produced no measures. The recording and the score may "
            "not be the same piece.")

    # Measure first, seconds second — what the rest of this ecosystem reads.
    rows = [(int(row[1]), float(row[0])) for row in sync_data]

    # The warper is a FULL alignment: it assumes the recording covers the
    # whole score. Given only part of the piece it places the music that is
    # there correctly and crams everything after it into the remaining
    # instants — which looks like success and is not. Two independent
    # measurements of that, both wide apart on real data (a complete
    # performance scores 0.000 and 1.000; a one-minute excerpt of the same
    # recording scores 0.790 and 0.132):
    times = [t for _, t in rows]
    crowded = sum(1 for a, b in zip(times, times[1:]) if abs(b - a) < 1e-9)
    crowding = crowded / len(times) if times else 0.0

    ref_span = ref_measures[-1][1] - ref_measures[0][1]
    span = times[-1] - times[0]
    span_ratio = (span / ref_span) if ref_span > 0 else 1.0
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(out.suffix + ".part")
    temp.write_text("\n".join(f"{m} {t}" for m, t in rows), encoding="utf-8")
    os.replace(temp, out)

    return {
        "ok": True,
        "measures": len(rows),
        "first_measure": rows[0][0], "last_measure": rows[-1][0],
        "starts_at": round(rows[0][1], 3), "ends_at": round(rows[-1][1], 3),
        # 0.0 when every measure got its own moment; high when measures had
        # to share one, which means the recording does not cover the score.
        "crowding": round(crowding, 4),
        "crowded_measures": crowded,
        # How much of the reference's extent this performance spans. Tempo
        # varies between performers; the whole piece being present does not.
        "span_ratio": round(span_ratio, 4),
        "span_seconds": round(span, 2),
        "reference_span_seconds": round(ref_span, 2),
        "reference": {
            "chroma": str(ref_chroma), "measures": str(ref_measures_file),
            "count": len(ref_measures), "last_measure": last_measure,
        },
        "timing": {
            "chroma_s": round(chroma_done - started, 2),
            "align_s": round(aligned - chroma_done, 2),
            "total_s": round(aligned - started, 2),
        },
    }


def main() -> int:
    args = parse_args()
    try:
        result = align(args)
    except ConfigProblem as exc:
        result = {"ok": False, "kind": CONFIG, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the caller only sees this
        result = {"ok": False, "kind": FAILURE,
                  "error": f"{type(exc).__name__}: {exc}".rstrip(": "),
                  "traceback": traceback.format_exc()}
    if args.status:
        status = pathlib.Path(args.status)
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
