"""Name the piece in a recording. Runs under the recognition interpreter.

This is the web app's side of the boundary with music_finrgerprint, not
part of it. It is executed as a subprocess by `app/identify.py` using
ID_PYTHON, because identifying needs torch and CUDA and the web app must
not carry either. Nothing here is imported by the Flask process.

music_finrgerprint is read only: this adds `<ID_ROOT>/src` to the path and
calls its public entry points. Importing from a checkout normally leaves
`__pycache__` behind in it, so the parent runs this with
PYTHONDONTWRITEBYTECODE; nothing is written into that repository.

    python identify_runner.py AUDIO --root R --pitch-index P --chord-index C
                              --out result.json [--pair-list L] [--top 5]

The verdict goes to --out as JSON, written atomically. stdout is left
alone deliberately — torch and the transcription model both print there,
so anything parsed out of it would break the first time a library changed
its logging.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import traceback

CONFIG = "config"        # the install is wrong; a person must fix something
FAILURE = "failure"      # this attempt went wrong


class ConfigProblem(RuntimeError):
    """Something this run needs is missing or unusable."""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("audio", help="video or audio file to identify")
    p.add_argument("--root", required=True, help="music_finrgerprint checkout")
    p.add_argument("--pitch-index", required=True)
    p.add_argument("--chord-index", required=True)
    p.add_argument("--out", required=True, help="where to write the JSON verdict")
    p.add_argument("--pair-list", default="", help="pair_list.json; enables score names")
    p.add_argument("--top", type=int, default=5, help="candidates to report")
    return p.parse_args()


def identify(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(pathlib.Path(args.root) / "src"))
    try:
        from weefeen_id.aggregate import identify_aggregated
        from weefeen_id.pipeline_v6 import load_v6_indexes
    except ImportError as exc:
        raise ConfigProblem(
            f"The recogniser's interpreter cannot import weefeen_id: {exc}") from exc

    # Resolved before the expensive part, so a misconfigured pair_list costs
    # a second rather than a minute of GPU time.
    names = _labels(args.pair_list)

    started = time.perf_counter()
    indexes = load_v6_indexes(args.pitch_index, args.chord_index)
    loaded = time.perf_counter()

    # identify_aggregated reads the media itself; librosa handles the
    # container, so a video needs no separate extraction step here.
    verdict = identify_aggregated(args.audio, indexes)
    finished = time.perf_counter()

    candidates = [
        {"piece_id": piece_id,
         "score": round(float(score), 4),
         "score_names": names(piece_id)}
        for piece_id, score in verdict.ranked[:args.top]
    ]

    return {
        "ok": True,
        "mode": verdict.mode,                    # confident | not_in_collection | abstain
        "winner": verdict.winner,
        "consensus": round(float(verdict.consensus), 4),
        "coverage": round(float(verdict.mean_coverage), 4),
        "n_windows": int(verdict.n_windows),
        "candidates": candidates,
        "device": _device(),
        "timing": {
            "load_indexes_s": round(loaded - started, 2),
            "identify_s": round(finished - loaded, 2),
            "total_s": round(finished - started, 2),
        },
    }


def _labels(pair_list: str):
    """A piece_id -> score-entry-names lookup.

    A missing pair_list is a legitimate configuration (piece ids only). A
    *broken* one is not, and must not be swallowed: without it every
    identification would resolve to no editions and the app would report
    "recognised, but no score installed" forever, for every piece, with
    nothing anywhere saying why. An individual id having no entry is the
    real quiet case, and that still returns an empty list.

    Read here rather than through `weefeen_id.labels.ScoreLabels`, which
    parses the same file but is not portable: see `_stem`. Only rows whose
    `level1` is "ok" count, which is the set of recordings the indexes were
    actually built from.
    """
    if not pair_list:
        return lambda _piece_id: []
    try:
        pairs = json.loads(
            pathlib.Path(pair_list).read_text(encoding="utf-8"))["pairs"]
        table: dict[str, list[str]] = {}
        for name, row in pairs.items():
            if row.get("level1") == "ok":
                table.setdefault(_stem(row["video"]), []).append(name)
    except Exception as exc:  # noqa: BLE001 - any failure here is fatal
        raise ConfigProblem(
            f"The score list at {pair_list} could not be read ({exc}). Without "
            f"it a recognised piece cannot be matched to a score.") from exc
    if not table:
        raise ConfigProblem(f"The score list at {pair_list} is empty.")
    table = {stem: sorted(set(names)) for stem, names in table.items()}
    return lambda piece_id: table.get(piece_id, []) if piece_id else []


def _stem(video: str) -> str:
    """The piece id a recording carries: its file name without extension.

    The Windows flavour on purpose, on every platform. Every `video` in the
    pair list is an absolute Windows path, and `pathlib.Path` on Linux is a
    PosixPath for which a backslash is an ordinary character — so the stem
    of `C:\\...\\work_op_39__troisieme_scherzo,_....mp4` came back as the
    whole path, matched no piece id, and every recognition on the Linux node
    ended "recognised, but no installed score to render it" while reporting
    full confidence. All 372 validated rows were affected, not some.

    PureWindowsPath accepts forward slashes too, so this keeps working if
    the list is ever rewritten with portable paths.
    """
    return pathlib.PureWindowsPath(video).stem


def _device() -> str:
    """Which processor did the work — the difference between 30 s and 5 min."""
    try:
        import torch
        return (f"cuda:{torch.cuda.get_device_name(0)}"
                if torch.cuda.is_available() else "cpu")
    except Exception:  # noqa: BLE001 - diagnostic only
        return "unknown"


def write(out: pathlib.Path, payload: dict) -> None:
    """Write the verdict so a reader never sees a half-written file."""
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(out.suffix + ".part")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temp, out)


def main() -> int:
    args = parse_args()
    try:
        result = identify(args)
    except ConfigProblem as exc:
        result = {"ok": False, "kind": CONFIG, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the caller only sees this file
        result = {"ok": False, "kind": FAILURE,
                  "error": f"{type(exc).__name__}: {exc}".rstrip(": "),
                  "traceback": traceback.format_exc()}
    write(pathlib.Path(args.out), result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
