"""Where this app finds its inputs.

VideoSync_webapp creates videos. It never prepares scores and never runs
synchronisation — music_line_extractor does both, upstream, and writes an
export package. The two repositories share no code, only a folder layout,
so every path that crosses the boundary is configuration rather than an
import. That configuration lives in `.env` (see `.env.example`).

Score sources are declared per kind:

    digital — exported from a digital score (Verovio/.krn). Ships .svg
              bands, so it can be rendered at any resolution, recoloured,
              and laid out for any aspect ratio.
    raster  — exported from a scanned/OMR source. Ships .png or .jpg
              bands at a fixed pixel size; usable, but it cannot be
              scaled up cleanly or recoloured as faithfully.

A package states which it is via `export.json` -> `options.is_digital`;
these roots only say where to look.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil

from dotenv import load_dotenv

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Local overrides first, then the committed template's defaults.
load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / ".env.example")

DIGITAL = "digital"
RASTER = "raster"


@dataclasses.dataclass(frozen=True)
class ScoreRoot:
    kind: str                 # DIGITAL | RASTER
    path: pathlib.Path

    @property
    def exists(self) -> bool:
        return self.path.is_dir()


class ConfigError(RuntimeError):
    """Configuration is unusable. Message is meant for the operator."""


def _paths(var: str) -> list[pathlib.Path]:
    """Read one env var holding os.pathsep-separated directories."""
    raw = os.getenv(var, "").strip()
    return [pathlib.Path(p.strip().strip('"')) for p in raw.split(os.pathsep)
            if p.strip()]


def _tool(var: str, default: str) -> str:
    """Resolve an executable from env, falling back to PATH lookup."""
    value = os.getenv(var, "").strip() or default
    return shutil.which(value) or value


@dataclasses.dataclass(frozen=True)
class Settings:
    score_roots: list[ScoreRoot]
    video_roots: list[pathlib.Path]
    ffmpeg: str
    ffprobe: str
    work_dir: pathlib.Path
    # music_line_extractor supplies auto-synchronisation. It is invoked as
    # a subprocess through its own CLI and its own interpreter, never
    # imported — so its repo is untouched and its dependencies stay its own.
    mle_root: pathlib.Path | None = None
    mle_python: str = ""

    @property
    def can_autosync(self) -> bool:
        return bool(self.mle_root and (self.mle_root / "services").is_dir()
                    and self.mle_python)

    @property
    def upload_dir(self) -> pathlib.Path:
        return self.work_dir / "uploads"

    @property
    def output_dir(self) -> pathlib.Path:
        return self.work_dir / "output"

    @property
    def cache_dir(self) -> pathlib.Path:
        """Rasterised bands, keyed by package + style."""
        return self.work_dir / "bands"

    def ensure_dirs(self) -> None:
        for d in (self.work_dir, self.upload_dir, self.output_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)

    def problems(self) -> list[str]:
        """Human-readable configuration issues; empty means good to go."""
        issues = []
        if not self.score_roots:
            issues.append(
                "No score roots configured. Set SCORE_ROOT_DIGITAL (and/or "
                "SCORE_ROOT_RASTER) in .env to where music_line_extractor "
                "writes its export packages.")
        for root in self.score_roots:
            if not root.exists:
                issues.append(f"{root.kind} score root does not exist: {root.path}")
        for name, exe in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)):
            if not shutil.which(exe) and not pathlib.Path(exe).is_file():
                issues.append(f"{name} not found (looked for {exe!r}). "
                              f"Install it or set {name.upper()}_EXE in .env.")
        return issues


def load() -> Settings:
    roots = [ScoreRoot(DIGITAL, p) for p in _paths("SCORE_ROOT_DIGITAL")]
    roots += [ScoreRoot(RASTER, p) for p in _paths("SCORE_ROOT_RASTER")]

    work = os.getenv("WORK_DIR", "").strip()
    mle = os.getenv("MLE_ROOT", "").strip().strip('"')
    return Settings(
        score_roots=roots,
        video_roots=_paths("VIDEO_ROOT"),
        ffmpeg=_tool("FFMPEG_EXE", "ffmpeg"),
        ffprobe=_tool("FFPROBE_EXE", "ffprobe"),
        work_dir=pathlib.Path(work) if work else REPO_ROOT / "var",
        mle_root=pathlib.Path(mle) if mle else None,
        mle_python=os.getenv("MLE_PYTHON", "").strip().strip('"') or "python",
    )


settings = load()
