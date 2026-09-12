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
    # Empty means the in-process queue, which is what a developer machine
    # and CI both use — no broker to install anywhere. Set it and the same
    # messages travel through RabbitMQ instead.
    rabbitmq_url: str = ""
    identify_min_seconds: float = 20.0
    identify_timeout: float = 600.0
    # Alignment. VideoScoreSync turns the performance into a chroma matrix
    # and warps it against the package's reference. Its own interpreter
    # again, and again not by preference: chroma extraction ABORTS the
    # process under this app's environment (numba, missing Intel SVML), and
    # an abort cannot be caught.
    # Turning an address into a country and a city, WITHOUT asking anybody.
    # A MaxMind-format database on this disk; the lookup is a read of a local
    # file, so no visitor's address is ever sent to a geolocation service.
    # Unset — or the file absent — means addresses are listed unresolved,
    # which is a smaller loss than the alternative.
    # The bucket a finished video is put in, so the machine that made it
    # can be destroyed. Private, presigned links only; nothing here is ever
    # public. Unset means results stay on local disk, which is what happens
    # today and is NOT safe once the renderer is a disposable host.
    object_endpoint: str = ""
    object_region: str = ""
    object_bucket: str = ""
    object_key: str = ""
    object_secret: str = ""
    # The compute node's economics. Nothing creates a machine yet; these
    # drive the SHADOW decision, so the grace period can be chosen from what
    # real traffic actually does rather than guessed and paid for.
    #
    # Grace is the single largest cost lever: creating costs ~2 minutes of a
    # visitor's wait, so tearing down the instant a job ends makes the next
    # one pay that again. Ten minutes is the design's starting figure.
    # Where operational mail goes. The operator's own mailbox,
    # never a visitor's, and nothing a visitor causes reaches it.
    alert_email: str = ""
    compute_grace_seconds: float = 600.0
    # What an hour of the plan costs, for turning hours into money on the
    # dashboard. 8 GB / 4 dedicated cores is the size the measurements argue
    # for; adjust to whatever plan is actually chosen.
    compute_hourly_cost: float = 0.108
    # How busy an hour of the day has to be, historically, before a machine
    # is held through it rather than released. Keeping never saves money —
    # the hour a job runs in is paid for either way — so this is bought
    # latency, not thrift, and the default is deliberately high enough that
    # a quiet site always releases.
    compute_keep_if_arrivals: float = 1.0
    # The provider, and what to make. The token is the only credential in
    # this system that can spend money without limit: it lives in .env on
    # the web box and never reaches a compute node or an image.
    linode_token: str = ""
    compute_region: str = "eu-central"
    compute_image: str = ""
    compute_plan: str = "g6-dedicated-4"
    # A ceiling, because a loop that creates machines is not a bug that
    # costs an afternoon. Refused beyond this many in an hour, whatever the
    # queue says.
    compute_max_creates_per_hour: int = 4
    # Passed to each machine at create, so somebody can get in and look.
    compute_ssh_key: str = ""
    # OFF until somebody turns it on. Everything up to creating runs and is
    # recorded regardless; this is the first code here that can spend money
    # without a person watching, and a switch is a different kind of safety
    # from a limit one hopes holds.
    compute_enabled: bool = False
    # Where a compute node reaches the broker: the web box's VLAN address,
    # never its public one.
    compute_broker_host: str = "10.0.0.2"
    # The private VLAN a node is attached to, and the address it takes on it.
    # A Linode VLAN is named by a label and is region-local; the first machine
    # to use a label creates it. The web box sits at compute_broker_host on
    # the same VLAN. Without an interface on this VLAN a created node has only
    # a public NIC and cannot reach the broker at all — the node comes up,
    # restart-loops, and is billed for nothing.
    compute_vlan: str = "vsw-vlan"
    compute_node_ip: str = "10.0.0.3"
    # The broker user the RABBITMQ_URL carries. A node is handed the narrow
    # `vsw-compute` user; the web box uses the full `vsw`. It is the one
    # reliable "am I a node?" signal reachable from inside the worker — the
    # node has no other flag — and it decides two things: a node must NOT try
    # to declare the topology (its user is forbidden to, and the web box has
    # already made it), and the web box's own worker must stand aside when
    # compute is on so a node, not it, takes the render.
    geoip_db: pathlib.Path | None = None
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
    # Cloudflare Turnstile — a CAPTCHA that is free, needs no account beyond
    # a Cloudflare login, and does NOT require the domain to be on Cloudflare.
    # It is the one control that stands between an automated agent and the
    # expensive, unauthenticated upload→recognise→render chain. Unset, the
    # check is skipped and the site behaves exactly as before — the same
    # "configured or inert" pattern as mail — so this can ship dark and go
    # live the moment the keys are added to .env. The secret is a credential
    # and belongs only in .env; the site key is public and reaches the page.
    turnstile_site_key: str = ""
    turnstile_secret: str = ""
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
    def is_compute_node(self) -> bool:
        """Whether this process is a disposable render node, not the web box.

        Read from the broker user in RABBITMQ_URL: a node is handed the narrow
        `vsw-compute` user, the web box uses `vsw`. It is the only signal a
        worker has about which machine it is on, and it decides that a node
        must not declare the topology (forbidden to, and already made) and
        that the web box's worker steps aside for a node when compute is on.
        """
        url = self.rabbitmq_url
        try:
            user = url.split("://", 1)[1].split("@", 1)[0].split(":", 1)[0]
        except (IndexError, AttributeError):
            return False
        return user == "vsw-compute"

    @property
    def bot_check(self) -> bool:
        """Whether a Turnstile token is demanded before an upload.

        On only when BOTH keys are set. The site key alone would render a
        widget whose token nothing checks — worse than none, because it
        looks protected — so both or neither.
        """
        return bool(self.turnstile_site_key and self.turnstile_secret)

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
        # A message whose only link is http://127.0.0.1 is useless to whoever
        # receives it, and worse than useless to the domain that sent it:
        # Gmail reads an unreachable private address as close to conclusive
        # evidence of spam, and every such message spends reputation that
        # real messages will need later. Better not to send at all.
        host = self.public_base_url.split("//", 1)[-1].split("/", 1)[0]
        host = host.split(":", 1)[0].lower()
        if host in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]"):
            return (f"PUBLIC_BASE_URL is {self.public_base_url!r}. A link to "
                    f"this machine means nothing to whoever gets the mail, "
                    f"and being seen to send one costs the domain its "
                    f"standing. Set a real address before sending.")
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
        # `.env.prod` is committed and so carries a placeholder where the
        # broker password goes. Copied to `.env` and left alone, the failure
        # is an authentication refusal from RabbitMQ on every publish, which
        # reads like a broken broker rather than an unfinished deployment
        # step. Say which it is.
        # A title panel needs the Futura faces. They are licensed and in no
        # repository, so a fresh host has none until somebody puts them
        # there — and the failure lands halfway through a render, minutes
        # in, as "No Futura LT Pro font in ...". Renders without a panel
        # succeed throughout, which is what makes it easy to miss.
        # Asked of `fonts` rather than kept here as well: FONT_DIR is read
        # there, with a default beside the code, and two places deciding
        # where the fonts are is how they come to disagree.
        from . import fonts
        where = fonts.font_dir()
        if not where.is_dir():
            issues.append(
                f"FONT_DIR does not exist: {where}. Renders with a title "
                f"panel will fail; ones without will not.")
        if "CHANGE_ME" in self.rabbitmq_url:
            issues.append(
                "RABBITMQ_URL still carries the placeholder password from "
                ".env.prod. Put the real one in .env, which is not committed.")
        return issues


def load() -> Settings:
    roots = [ScoreRoot(DIGITAL, p) for p in _paths("SCORE_ROOT_DIGITAL")]
    roots += [ScoreRoot(RASTER, p) for p in _paths("SCORE_ROOT_RASTER")]

    work = os.getenv("WORK_DIR", "").strip()
    return Settings(
        score_roots=roots,
        video_roots=_paths("VIDEO_ROOT"),
        ffmpeg=_tool("FFMPEG_EXE", "ffmpeg"),
        ffprobe=_tool("FFPROBE_EXE", "ffprobe"),
        work_dir=pathlib.Path(work) if work else REPO_ROOT / "var",
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
        rabbitmq_url=os.getenv("RABBITMQ_URL", "").strip().strip('"'),
        identify_timeout=_number("IDENTIFY_TIMEOUT", 600.0),
        object_endpoint=os.getenv("OBJECT_ENDPOINT", "").strip(),
        object_region=os.getenv("OBJECT_REGION", "").strip(),
        object_bucket=os.getenv("OBJECT_BUCKET", "").strip(),
        object_key=os.getenv("OBJECT_KEY", "").strip(),
        object_secret=os.getenv("OBJECT_SECRET", "").strip(),
        alert_email=os.getenv("ALERT_EMAIL", "").strip(),
        compute_grace_seconds=_number("COMPUTE_GRACE_SECONDS", 600.0),
        compute_hourly_cost=_number("COMPUTE_HOURLY_COST", 0.108),
        compute_keep_if_arrivals=_number("COMPUTE_KEEP_IF_ARRIVALS", 1.0),
        linode_token=os.getenv("LINODE_TOKEN", "").strip(),
        compute_region=os.getenv("COMPUTE_REGION", "eu-central").strip(),
        compute_image=os.getenv("COMPUTE_IMAGE", "").strip(),
        compute_plan=os.getenv("COMPUTE_PLAN", "g6-dedicated-4").strip(),
        compute_max_creates_per_hour=int(
            _number("COMPUTE_MAX_CREATES_PER_HOUR", 4)),
        compute_ssh_key=os.getenv("COMPUTE_SSH_KEY", "").strip(),
        compute_enabled=os.getenv("COMPUTE_ENABLED", "").strip().lower()
        in ("1", "true", "yes"),
        compute_vlan=os.getenv("COMPUTE_VLAN", "vsw-vlan").strip(),
        compute_node_ip=os.getenv("COMPUTE_NODE_IP", "10.0.0.3").strip(),
        compute_broker_host=os.getenv("COMPUTE_BROKER_HOST",
                                      "10.0.0.2").strip(),
        geoip_db=_one("GEOIP_DB"),
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
        turnstile_site_key=os.getenv("TURNSTILE_SITE_KEY", "").strip(),
        turnstile_secret=os.getenv("TURNSTILE_SECRET", "").strip(),
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
