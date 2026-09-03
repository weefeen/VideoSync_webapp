"""Align a performance to a score, via music_line_extractor.

We do not implement alignment. music_line_extractor has a production
aligner (`ACTIVE_ALIGNER` in services/auto_sync_harness.py, currently
V11_SOD), reached through that repo's own workflow CLI as a **subprocess
with its own interpreter** — never imported. The two repositories stay
unlinked, music_line_extractor is never modified, and whichever aligner is
promoted there is automatically what we use.

INPUT CONTRACT — the package's `score/` folder, and nothing else:

    score/source.krn     the score, and the only alignment input
    score/lines/         band images (used by the renderer, not here)
    score/export.json    manifest

The package's `.spj` is deliberately NOT read: it is a large opaque bundle,
and everything needed is already in `score/`. A throwaway project is built
from `source.krn` inside the job folder instead, so nothing is written to
the corpus either.

`reference/`, when a package has one, is the curated reference recording.
It is optional: without it only automatic sync is possible.

Two modes, differing in what the recording is aligned AGAINST:

    AUTO       against the SCORE. Needs only score/source.krn.
    REFERENCE  against the REFERENCE RECORDING, audio to audio. Needs a
               reference/ folder, and is the closer match when present.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from typing import Callable

from .settings import settings

ProgressFn = Callable[[str, str], None]

# Workflow verbs. None of these names encode an algorithm version — the
# aligner is chosen by ACTIVE_ALIGNER inside music_line_extractor.
WORKFLOW_INGEST = "ingest_krn"          # source.krn -> job-local project
WORKFLOW_KRN_SYNC = "krn_to_synced_project"   # ingest + align, one call
WORKFLOW_REFERENCE = "auto_sync_reference"    # audio-to-audio alignment

# Score sources the ingest step accepts, in order of preference.
SCORE_SUFFIXES = (".krn", ".musicxml", ".mxl")

# Where a package may keep its reference recording, in the order
# music_line_extractor itself looks.
_REFERENCE_DIRS = ("reference", "performance", "audio", "cache")


def _noop(stage: str, detail: str = "") -> None:
    pass


class SyncError(RuntimeError):
    """Alignment failed. Message is safe to show the user."""


class SyncUnavailable(SyncError):
    """music_line_extractor isn't configured, so we cannot align at all."""


# --------------------------------------------------------------------------
# what a package offers
# --------------------------------------------------------------------------
def find_score_source(package_root: pathlib.Path) -> pathlib.Path | None:
    """The score file inside `score/`. The .spj is never considered."""
    score_dir = package_root / "score"
    for suffix in SCORE_SUFFIXES:
        for name in (f"source{suffix}", f"*{suffix}"):
            for hit in sorted(score_dir.glob(name)):
                return hit
    return None


def has_reference(package_root: pathlib.Path) -> bool:
    """Whether the package carries a reference recording to align against."""
    return reference_dir(package_root) is not None


def reference_dir(package_root: pathlib.Path) -> pathlib.Path | None:
    for name in _REFERENCE_DIRS:
        folder = package_root / name
        if folder.is_dir() and (folder / "audio.wav").is_file():
            return folder
    return None


# --------------------------------------------------------------------------
# talking to music_line_extractor
# --------------------------------------------------------------------------
_registry: set[str] | None = None


def registered_workflows() -> set[str]:
    """Workflow names music_line_extractor currently exposes. Cached."""
    global _registry
    if _registry is not None:
        return _registry
    _registry = set()
    if not settings.can_autosync:
        return _registry
    try:
        out = subprocess.run(
            [settings.mle_python, "-m", "services.workflows.cli", "list"],
            cwd=str(settings.mle_root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return _registry
    for line in out.splitlines():
        name = line.strip()
        if name and re.fullmatch(r"[a-z][a-z0-9_]+", name):
            _registry.add(name)
    return _registry


def _require_workflow(name: str) -> str:
    available = registered_workflows()
    if available and name not in available:
        raise SyncUnavailable(
            f"music_line_extractor has no {name!r} workflow. It exposes: "
            f"{', '.join(sorted(available)) or 'nothing'}.")
    return name


def _cli(args: list[str], on_progress: ProgressFn) -> dict:
    """Run one workflow CLI verb and return its JSON result."""
    import json

    if not settings.can_autosync:
        raise SyncUnavailable(
            "Alignment needs music_line_extractor. Set MLE_ROOT and "
            "MLE_PYTHON in .env to its checkout and interpreter.")

    cmd = [settings.mle_python, "-m", "services.workflows.cli", *args, "--json"]
    proc = subprocess.Popen(
        cmd, cwd=str(settings.mle_root), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace")

    tail: list[str] = []
    blob: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-40]
        # --json makes the result the only structured output, but it is
        # pretty-printed across several lines.
        if line.startswith("{") or blob:
            blob.append(line)
            continue
        stage = re.search(r"\b(autosync\.[a-z_]+|[a-z_]+aligner)\b", line)
        on_progress("align", stage.group(1) if stage else line[:80])

    proc.wait()
    result = None
    if blob:
        try:
            result = json.loads("\n".join(blob))
        except json.JSONDecodeError:
            result = None
    if result is None:
        raise SyncError("Alignment produced no result:\n" + "\n".join(tail[-8:]))
    if str(result.get("status", "")).upper() not in ("OK", "SUCCESS", "COMPLETED"):
        raise SyncError(
            f"Alignment failed ({result.get('error_code') or result.get('status')}): "
            f"{result.get('message') or 'no detail'}")
    return result


# --------------------------------------------------------------------------
# the two modes
# --------------------------------------------------------------------------
def _job_project(package_root: pathlib.Path, job_dir: pathlib.Path
                 ) -> tuple[pathlib.Path, pathlib.Path, str]:
    """Locate the score source and decide where the job's project goes."""
    source = find_score_source(package_root)
    if source is None:
        raise SyncError(
            f"{package_root.name} has no score source in score/ — expected "
            f"one of: {', '.join('source' + s for s in SCORE_SUFFIXES)}.")
    workspace = job_dir / "sync"
    piece_id = package_root.name
    workspace.mkdir(parents=True, exist_ok=True)
    return source, workspace, piece_id


def align_to_score(package_root: pathlib.Path, video: pathlib.Path,
                   job_dir: pathlib.Path, on_progress: ProgressFn = _noop
                   ) -> pathlib.Path:
    """Automatic sync: align `video` against the score itself.

    One call does ingest and alignment. Band, chroma and geometry exports
    are switched off — the package already ships those, and re-deriving
    them would cost minutes for nothing.
    """
    source, workspace, piece_id = _job_project(package_root, job_dir)
    workflow = _require_workflow(WORKFLOW_KRN_SYNC)

    on_progress("align", f"aligning against the score ({workflow})")
    # --force/--overwrite: the workflow idempotency-skips when a
    # measures.data is already present, which would silently hand back a
    # previous job's alignment. Each job must align its own recording.
    _cli([workflow,
          f"--krn-path={source}",
          f"--project-folder={workspace}",
          f"--project-name={piece_id}",
          f"--video-path={video}",
          "--force", "--overwrite",
          "--no-include-bands",
          "--no-include-chroma",
          "--no-include-measures-geometry"], on_progress)
    return _require_measures(workspace / piece_id, on_progress)


def align_to_reference(package_root: pathlib.Path, video: pathlib.Path,
                       job_dir: pathlib.Path, on_progress: ProgressFn = _noop
                       ) -> pathlib.Path:
    """Reference sync: align `video` audio-to-audio against the reference.

    Builds a throwaway project from score/source.krn, mirrors the package's
    reference recording into it, then aligns. Passing no reference .spj
    makes the workflow use the project's own reference/ folder, which is
    exactly the copy we just placed there.
    """
    reference = reference_dir(package_root)
    if reference is None:
        raise SyncError(
            f"{package_root.name} has no reference recording — expected "
            f"audio.wav in one of: {', '.join(_REFERENCE_DIRS)}. "
            f"Use automatic sync, which aligns against the score.")

    source, workspace, piece_id = _job_project(package_root, job_dir)
    piece_folder = workspace / piece_id

    on_progress("align", f"building a project from {source.name}")
    _cli([_require_workflow(WORKFLOW_INGEST),
          f"--krn-path={source}",
          f"--project-folder={workspace}",
          f"--project-name={piece_id}",
          "--overwrite"], on_progress)

    local_reference = piece_folder / "reference"
    if not (local_reference / "audio.wav").is_file():
        on_progress("align", f"copying the reference recording "
                             f"({sum(f.stat().st_size for f in reference.iterdir() if f.is_file()) / 1e6:.0f} MB)")
        shutil.copytree(reference, local_reference, dirs_exist_ok=True)

    spj = piece_folder / f"{piece_id}.spj"
    if not spj.is_file():
        found = sorted(piece_folder.glob("*.spj"))
        if not found:
            raise SyncError(f"{WORKFLOW_INGEST} wrote no project under {piece_folder}.")
        spj = found[0]

    workflow = _require_workflow(WORKFLOW_REFERENCE)
    on_progress("align", f"aligning against the reference recording ({workflow})")
    # No --reference-spj-path: an empty value tells the workflow to use this
    # project's own reference/ folder, so the package's .spj is never read.
    _cli([workflow,
          f"--spj-path={spj}",
          f"--project-folder={workspace}",
          f"--video-path={video}"], on_progress)
    return _require_measures(piece_folder, on_progress)


def _require_measures(piece_folder: pathlib.Path,
                      on_progress: ProgressFn) -> pathlib.Path:
    measures = _locate_measures(piece_folder)
    if measures is None:
        raise SyncError("Alignment reported success but wrote no "
                        f"measures.data under {piece_folder}.")
    rows = sum(1 for line in measures.open(encoding="utf-8") if line.strip())
    on_progress("align", f"{rows} measures aligned")
    return measures


def _locate_measures(piece_folder: pathlib.Path) -> pathlib.Path | None:
    """Find the measures.data the workflow wrote, whatever it called the dir."""
    for rel in ("performance/measures.data", "audio/measures.data",
                "cache/measures.data", "measures.data"):
        path = piece_folder / rel
        if path.is_file() and path.stat().st_size > 0:
            return path
    hits = [p for p in piece_folder.rglob("measures.data")
            if p.stat().st_size > 0 and "reference" not in p.parts]
    return hits[0] if hits else None
