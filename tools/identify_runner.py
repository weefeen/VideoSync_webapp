"""Name the piece in a recording. Runs under the recognition interpreter.

This is the web app's side of the boundary with music_finrgerprint, not
part of it. It is executed as a subprocess by `app/identify.py` using
ID_PYTHON, because identifying needs torch and CUDA and the web app must
not carry either. Nothing here is imported by the Flask process.

music_finrgerprint is read only: this adds `<ID_ROOT>/src` to the path and
calls its public entry points. It writes nothing into that repository.

    python identify_runner.py AUDIO --root R --index-dir D --out result.json
                              [--pair-list P] [--top 5]

The verdict goes to --out as JSON. stdout is left alone deliberately —
torch and the transcription model both print there, so anything parsed out
of it would break the first time a library changed its logging.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import traceback


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("audio", help="video or audio file to identify")
    p.add_argument("--root", required=True, help="music_finrgerprint checkout")
    p.add_argument("--index-dir", required=True, help="holds the two AMT indexes")
    p.add_argument("--out", required=True, help="where to write the JSON verdict")
    p.add_argument("--pair-list", default="", help="pair_list.json; enables score names")
    p.add_argument("--top", type=int, default=5, help="candidates to report")
    return p.parse_args()


def identify(args: argparse.Namespace) -> dict:
    root = pathlib.Path(args.root)
    sys.path.insert(0, str(root / "src"))

    from weefeen_id.aggregate import identify_aggregated
    from weefeen_id.pipeline_v6 import load_v6_indexes

    index_dir = pathlib.Path(args.index_dir)
    started = time.perf_counter()
    indexes = load_v6_indexes(index_dir / "amt_pitch_index.pkl",
                              index_dir / "amt_chord_index.pkl")
    loaded = time.perf_counter()

    # identify_aggregated reads the media itself; librosa handles the
    # container, so a video needs no separate extraction step here.
    verdict = identify_aggregated(args.audio, indexes)
    finished = time.perf_counter()

    # piece_id -> the score editions that recording backs. This mapping is
    # recognition-library knowledge (it reads pair_list.json), so it is
    # resolved here rather than guessed at on the other side.
    names = _labels(args.pair_list)

    candidates = []
    for rank, (piece_id, score) in enumerate(verdict.ranked[:args.top], start=1):
        candidates.append({
            "rank": rank,
            "piece_id": piece_id,
            "score": round(float(score), 4),
            "score_names": names(piece_id),
        })

    return {
        "ok": True,
        "mode": verdict.mode,                    # confident | not_in_collection | abstain
        "winner": verdict.winner,
        "consensus": round(float(verdict.consensus), 4),
        "coverage": round(float(verdict.mean_coverage), 4),
        "n_windows": int(verdict.n_windows),
        "candidates": candidates,
        "timing": {
            "load_indexes_s": round(loaded - started, 2),
            "identify_s": round(finished - loaded, 2),
            "total_s": round(finished - started, 2),
        },
    }


def _labels(pair_list: str):
    """A piece_id -> score-entry-names lookup, or a no-op if unavailable."""
    if not pair_list:
        return lambda _piece_id: []
    try:
        from weefeen_id.labels import ScoreLabels
        table = ScoreLabels.from_pair_list(pair_list)
        return lambda piece_id: table.names(piece_id) if piece_id else []
    except Exception:
        # A missing or malformed pair_list must not lose the identification
        # itself; the caller can still show piece ids and say why.
        return lambda _piece_id: []


def main() -> int:
    args = parse_args()
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = identify(args)
    except Exception as exc:  # noqa: BLE001 - the caller only sees this file
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc()}
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
