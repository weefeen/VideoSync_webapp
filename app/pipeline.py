"""End-to-end job: a video and a score package in, a synced video out.

Every job aligns the recording first — the package's own measures.data
belongs to its reference performance, not to whatever the user brought.
The two modes differ in what the recording is aligned AGAINST:

    AUTO       against the SCORE itself, read from the .spj. Needs no
               reference recording, so it works for any package.

    REFERENCE  against the package's curated REFERENCE RECORDING, audio to
               audio. Needs the reference files in performance/ (or a
               reference/ snapshot), and is the better alignment when
               they exist.

Alignment is music_line_extractor's job, reached as a subprocess; this
module only decides which workflow to call and renders the result.

Everything is written under the job folder. A score package is only ever
read — its performance/ folder holds the reference recording that
REFERENCE mode aligns against, so writing there would destroy the very
thing that makes the mode work.
"""

from __future__ import annotations

import dataclasses
import pathlib
import shutil
from typing import Callable

from . import autosync, package as pkg, render as rnd
from .settings import settings

ProgressFn = Callable[[str, str], None]

AUTO = "auto"              # align against the score
REFERENCE = "reference"    # align against the reference recording
MODES = (AUTO, REFERENCE)

MODE_LABELS = {
    AUTO: "automatic sync — aligned against the score",
    REFERENCE: "reference sync — aligned against the reference recording",
}

# Stage labels, in the order they run.
STAGES = ("prepare", "align", "bands", "strip", "encode", "done")


def _noop(stage: str, detail: str = "") -> None:
    pass


class PipelineError(RuntimeError):
    """A job failed. Message is safe to show the user."""


@dataclasses.dataclass
class JobResult:
    output: pathlib.Path
    mode: str
    measures: pathlib.Path
    job_dir: pathlib.Path
    package_name: str


def choose_mode(p: pkg.ScorePackage, requested: str | None = None) -> str:
    """Pick a mode, or validate the one asked for.

    Reference sync is preferred when the package has a reference recording
    — audio-to-audio alignment between two performances is a closer match
    than performance-to-score. Otherwise fall back to automatic sync.
    """
    if not settings.can_autosync:
        raise PipelineError(
            "Alignment needs music_line_extractor. Set MLE_ROOT and "
            "MLE_PYTHON in .env to its checkout and interpreter.")

    if autosync.find_score_source(p.root) is None:
        raise PipelineError(
            f"{p.name} has no score source in score/ — alignment needs "
            f"source.krn (or .musicxml/.mxl) there.")

    has_ref = autosync.has_reference(p.root)
    if requested:
        if requested not in MODES:
            raise PipelineError(f"Unknown mode {requested!r}. "
                                f"Choose from: {', '.join(MODES)}.")
        if requested == REFERENCE and not has_ref:
            raise PipelineError(
                f"{p.name} has no reference recording, so reference sync "
                f"isn't possible. Use mode '{AUTO}' instead.")
        return requested
    return REFERENCE if has_ref else AUTO


def job_folder(job_id: str) -> pathlib.Path:
    return settings.work_dir / job_id


def run(p: pkg.ScorePackage, video: pathlib.Path, job_id: str,
        style: rnd.Style | None = None, mode: str | None = None,
        meta: dict | None = None,
        on_progress: ProgressFn = _noop) -> JobResult:
    """Run one job to completion. Returns where the video landed."""
    if not video.is_file():
        raise PipelineError(f"No such video: {video}")

    style = style or rnd.Style()
    mode = choose_mode(p, mode)
    job_dir = job_folder(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    on_progress("prepare", f"{p.name} · {MODE_LABELS[mode]}")

    align = (autosync.align_to_reference if mode == REFERENCE
             else autosync.align_to_score)
    try:
        measures = align(p.root, video, job_dir, on_progress)
    except autosync.SyncError as exc:
        raise PipelineError(str(exc)) from exc

    try:
        timed = p.with_alignment(measures)
    except pkg.PackageError as exc:
        raise PipelineError(
            f"Alignment finished but its measures.data is unusable: {exc}") from exc

    if not timed.timeline:
        raise PipelineError("No alignment available, so the bands can't be timed.")

    output = job_dir / f"{_safe(p.name)}_synced.mp4"
    try:
        rnd.render(timed, video, output, style, meta or {}, on_progress)
    except rnd.RenderError as exc:
        raise PipelineError(str(exc)) from exc

    return JobResult(output=output, mode=mode, measures=measures,
                     job_dir=job_dir, package_name=p.name)


def _safe(name: str) -> str:
    """Filesystem-safe, readable, and short enough for any path limit."""
    keep = "".join(c if (c.isalnum() or c in " ._-") else "_" for c in name)
    return keep.strip().replace(" ", "_")[:60] or "score"


def find_package(name: str) -> pkg.ScorePackage | None:
    """Locate a package in the library by exact name, or unique fragment.

    Searches only what usable_packages() admits, so a filtered-out composer
    cannot be reached by naming it directly.
    """
    matches: list[pkg.ScorePackage] = []
    for p in usable_packages():
        if p.name == name:
            return p
        needle = name.lower()
        if needle in p.name.lower() or needle in p.display_name.lower():
            matches.append(p)
    return matches[0] if matches else None


def usable_packages() -> list[pkg.ScorePackage]:
    """Every renderable package the library admits, deduplicated across roots.

    Honours COMPOSER_FILTER, which is matched against each score's own
    Humdrum COM record rather than its folder name. A package whose source
    declares no composer is admitted — better to show an unlabelled score
    than to hide one because its metadata is thin.
    """
    seen: dict[str, pkg.ScorePackage] = {}
    for root in settings.score_roots:
        for _, p, _ in pkg.inspect(root.path):
            if p is None:
                continue
            if p.surname and not settings.allows(p.surname):
                continue
            seen.setdefault(p.name, p)
    return list(seen.values())


def cleanup(job_id: str) -> None:
    """Remove a job's workspace. Score packages are never touched."""
    shutil.rmtree(job_folder(job_id), ignore_errors=True)
