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
import math
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
    # Backdrop artwork offered by the UI for background=static / dynamic.
    background_static: pathlib.Path | None = None
    background_dynamic: pathlib.Path | None = None
    # Restrict the library to one composer. Matched against the score's own
    # Humdrum COM record, so it is based on what the score says rather than
    # on folder naming. Empty means no restriction.
    composer_filter: str = ""
    # Recognition. music_finrgerprint names the piece from the performance
    # audio. Like music_line_extractor it runs as a subprocess with its own
    # interpreter — and here that is not a preference: identifying needs
    # torch and CUDA, which this app must not carry.
    id_root: pathlib.Path | None = None
    id_python: str = ""
    id_index_dir: pathlib.Path | None = None
    pair_list: pathlib.Path | None = None
    # Below this the recogniser plans a single window, and a single window
    # always agrees with itself: consensus comes out at 1.0 and the open-set
    # gate stops meaning anything. Refuse such uploads rather than guess.
    identify_min_seconds: float = 20.0
    identify_timeout: float = 600.0
    # Alignment. VideoScoreSync turns the performance into a chroma matrix
    # and warps it against the package's reference. Its own interpreter
    # again, and again not by preference: chroma extraction ABORTS the
    # process under this app's environment (numba, missing Intel SVML), and
    # an abort cannot be caught.
    vss_root: pathlib.Path | None = None
    sync_python: str = ""
    sync_timeout: float = 900.0
    # Telling someone their video is ready. Credentials belong in .env,
    # which is not committed; .env.example carries the names only.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_ssl: bool = False           # implicit TLS, usually port 465
    smtp_starttls: bool = True       # upgrade in place, usually port 587
    public_base_url: str = ""        # where a link in the mail should point
    # How long the emailed link keeps working. Afterwards the video is
    # archived rather than deleted — still ours, no longer a download.
    #
    # This is enforced here, in our own code, and NOT by the storage
    # lifecycle rule. Those run in whole days and asynchronously, so a rule
    # set for the same moment would sometimes fire early and leave a live
    # link pointing at an object in cold storage: a download that hangs for
    # hours instead of failing in a way we can explain. The transition is
    # therefore set a day later than the expiry, and the gap costs nothing.
    retention_hot_hours: float = 48.0
    archive_transition_days: int = 3

    def allows(self, surname: str) -> bool:
        """Whether a score by this composer belongs in the library."""
        if not self.composer_filter:
            return True
        return self.composer_filter.strip().lower() in (surname or "").lower()

    def background_for(self, kind: str) -> str | None:
        """The configured artwork for a background kind, if it exists."""
        path = {"static": self.background_static,
                "dynamic": self.background_dynamic}.get(kind)
        return str(path) if path and path.is_file() else None

    @property
    def can_autosync(self) -> bool:
        return bool(self.mle_root and (self.mle_root / "services").is_dir()
                    and self.mle_python)

    @property
    def pitch_index(self) -> pathlib.Path | None:
        d = self.id_index_dir
        return (d / "amt_pitch_index.pkl") if d else None

    @property
    def chord_index(self) -> pathlib.Path | None:
        d = self.id_index_dir
        return (d / "amt_chord_index.pkl") if d else None

    @property
    def can_identify(self) -> bool:
        """Whether this install can name a piece from its audio."""
        return not self.why_cannot_identify()

    def why_cannot_identify(self) -> str:
        """A diagnostic for the operator; empty when recognition works.

        Every check here is one that would otherwise surface a minute later
        as a stack trace from another process.
        """
        if not self.id_root:
            return "ID_ROOT is not set."
        if not (self.id_root / "src" / "weefeen_id").is_dir():
            return f"No weefeen_id package under {self.id_root / 'src'}."
        if not self.id_python:
            return ("ID_PYTHON is not set. Recognition needs its own "
                    "interpreter — the one with torch, which is not this one.")
        if not (pathlib.Path(self.id_python).is_file()
                or shutil.which(self.id_python)):
            return f"No interpreter at {self.id_python!r}."
        for label, path in (("pitch", self.pitch_index), ("chord", self.chord_index)):
            if not path:
                return "ID_INDEX_DIR is not set."
            if not path.is_file():
                return (f"No {label} index at {path}. Build it in "
                        f"music_finrgerprint, or point ID_INDEX_DIR elsewhere.")
        # Configured but missing is worse than absent: without it every
        # recognition would resolve to no score at all.
        if self.pair_list and not self.pair_list.is_file():
            return f"PAIR_LIST is set but there is no file at {self.pair_list}."
        return ""

    @property
    def can_email(self) -> bool:
        """Whether this install can actually send a message."""
        return not self.why_cannot_email()

    def why_cannot_email(self) -> str:
        """Empty when mail works. The interface asks before promising one."""
        if not self.smtp_host:
            return "SMTP_HOST is not set."
        if not self.smtp_from:
            return "SMTP_FROM is not set — a message needs a sender."
        if not self.public_base_url:
            return ("PUBLIC_BASE_URL is not set, so a link in the mail would "
                    "point nowhere.")
        return ""

    @property
    def can_sync(self) -> bool:
        """Whether this install can align a performance to a score."""
        return not self.why_cannot_sync()

    def why_cannot_sync(self) -> str:
        """A diagnostic for the operator; empty when alignment works."""
        if not self.vss_root:
            return "VSS_ROOT is not set."
        if not (self.vss_root / "services").is_dir():
            return f"No services/ under {self.vss_root}."
        if not self.sync_python:
            return ("SYNC_PYTHON is not set. Alignment needs an interpreter "
                    "whose numba can run chroma extraction — not this one.")
        if not (pathlib.Path(self.sync_python).is_file()
                or shutil.which(self.sync_python)):
            return f"No interpreter at {self.sync_python!r}."
        return ""

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
        background_static=_one("BACKGROUND_STATIC"),
        background_dynamic=_one("BACKGROUND_DYNAMIC"),
        composer_filter=os.getenv("COMPOSER_FILTER", "").strip(),
        id_root=_one("ID_ROOT"),
        # No fallback to "python" on purpose: recognition runs in a *different*
        # interpreter from this one, so guessing the current one would only
        # turn a configuration mistake into a slow, obscure import error.
        id_python=os.getenv("ID_PYTHON", "").strip().strip('"'),
        id_index_dir=_one("ID_INDEX_DIR"),
        pair_list=_one("PAIR_LIST"),
        identify_min_seconds=_number("IDENTIFY_MIN_SECONDS", 20.0),
        identify_timeout=_number("IDENTIFY_TIMEOUT", 600.0),
        vss_root=_one("VSS_ROOT"),
        sync_python=os.getenv("SYNC_PYTHON", "").strip().strip('"'),
        sync_timeout=_number("SYNC_TIMEOUT", 900.0),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(_number("SMTP_PORT", 587)),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", "").strip(),
        smtp_ssl=_flag("SMTP_SSL", False),
        smtp_starttls=_flag("SMTP_STARTTLS", True),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        retention_hot_hours=_number("RETENTION_HOT_HOURS", 48.0),
        archive_transition_days=int(_number("ARCHIVE_TRANSITION_DAYS", 3)),
    )


def _flag(var: str, default: bool) -> bool:
    raw = os.getenv(var, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _number(var: str, default: float) -> float:
    """A positive, finite number from the environment, or the default.

    Anything else — a typo, a negative, an infinity — falls back rather
    than travelling on to fail somewhere less obvious, such as a timeout
    of NaN reaching subprocess.run.
    """
    raw = os.getenv(var, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def _one(var: str) -> pathlib.Path | None:
    raw = os.getenv(var, "").strip().strip('"')
    return pathlib.Path(raw) if raw else None


settings = load()
