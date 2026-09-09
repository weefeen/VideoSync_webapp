"""Compose a performance video with its score band burned in.

The alignment is decided upstream — measures.data says which measure sounds
when — so rendering is: pick the right band for each moment, style it, place
it, and composite.

Everything the user can choose lives in `Style`, and every position is
derived from it by `compute_layout`. Nothing is hardcoded to 1920x1080, so
one code path serves all combinations:

    background   none | static image | looping video
    band         top | bottom
    panel        a left column of title text, or not
    aspect       16:9 | 1:1 | 9:16

The band and the video ALWAYS keep their own aspect ratio; neither is ever
stretched. The band is fitted inside its box. The video spans the content
width instead, so it stays as large as the frame allows, and whatever
height that demands beyond the space left over is cropped — `video_offset`
chooses which slice survives.

Two ffmpeg passes, because it is far easier to reason about than one:
the band images become a video whose cuts land on the measure timestamps,
then that strip is composited with the performance and the background.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import shutil
import shlex
import logging
import subprocess
import tempfile
from typing import Callable

from PIL import Image, ImageOps

from . import panel as panel_mod
from . import svg
from .package import Band, ScorePackage
from .settings import settings

ProgressFn = Callable[[str, str], None]


def _noop(stage: str, detail: str = "") -> None:
    pass


ASPECTS = {"16/9": (1920, 1080), "1/1": (1080, 1080), "9/16": (1080, 1920)}

NONE, STATIC, DYNAMIC = "none", "static", "dynamic"
BACKGROUNDS = (NONE, STATIC, DYNAMIC)

TOP, BOTTOM = "top", "bottom"


logger = logging.getLogger(__name__)


class RenderError(RuntimeError):
    """Rendering failed. Message is safe to show the user."""


@dataclasses.dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclasses.dataclass(frozen=True)
class Style:
    """Every choice the user makes about the look of the output."""
    aspect: str = "16/9"
    background: str = NONE
    background_path: str | None = None    # required for static/dynamic
    band_position: str = BOTTOM
    panel: bool = False
    # 578/1920 — the column width the original title panel used.
    panel_width: float = 0.301
    # No inset by default: the band spans the frame edge to edge.
    margin: int = 0
    # Nothing between the video and the score: they meet.
    gap: int = 0
    canvas_bg: str = "#141019"
    band_bg: str = "#ffffff"          # the band's paper
    band_fg: str = "#1c1622"          # the notes
    # Paper transparency, 0.0 = invisible (notes float over the backdrop),
    # 1.0 = solid. Independent of band_opacity, which fades the whole band.
    band_bg_opacity: float = 1.0
    band_opacity: float = 1.0
    # Which slice of the video's height survives when it is taller
    # than the space beside the band. 0 keeps the top of the frame,
    # 1 the bottom, 0.5 the middle.
    video_offset: float = 0.5
    crf: int = 20

    @property
    def needs_alpha(self) -> bool:
        """Whether the band has to survive as RGBA through the strip."""
        return self.band_bg_opacity < 1.0 or self.band_opacity < 1.0

    def validate(self) -> None:
        if self.aspect not in ASPECTS:
            raise RenderError(f"Unsupported aspect {self.aspect!r}. "
                              f"Choose from {', '.join(ASPECTS)}.")
        if self.background not in BACKGROUNDS:
            raise RenderError(f"Unknown background {self.background!r}. "
                              f"Choose from {', '.join(BACKGROUNDS)}.")
        if self.band_position not in (TOP, BOTTOM):
            raise RenderError(f"band_position must be {TOP!r} or {BOTTOM!r}, "
                              f"got {self.band_position!r}.")
        if self.background in (STATIC, DYNAMIC):
            if not self.background_path:
                raise RenderError(
                    f"background={self.background!r} needs background_path.")
            if not pathlib.Path(self.background_path).is_file():
                raise RenderError(f"Background not found: {self.background_path}")
        if not 0.0 <= self.panel_width < 0.9:
            raise RenderError("panel_width must be between 0 and 0.9.")

    @property
    def canvas(self) -> tuple[int, int]:
        return ASPECTS[self.aspect]

    def key(self) -> str:
        """Identifies prepared bands for this styling."""
        blob = json.dumps({k: v for k, v in dataclasses.asdict(self).items()
                           if k in ("band_bg", "band_fg", "band_opacity",
                                    "band_bg_opacity")},
                          sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]


@dataclasses.dataclass(frozen=True)
class Layout:
    canvas: tuple[int, int]
    band: Rect
    video: Rect                      # where the video lands on the canvas
    panel: Rect | None
    # The video keeps its aspect and spans the full content width, so when
    # the space left beside the band is shorter than that demands, the
    # surplus height is cropped rather than the picture shrunk. These say
    # what to scale to and which slice of it to keep.
    video_source: tuple[int, int] = (0, 0)
    video_crop_x: int = 0
    video_crop_y: int = 0

    @property
    def crops(self) -> bool:
        return self.video_source[1] > self.video.h


def _even(n: float) -> int:
    """x264 needs even dimensions in yuv420p, and at least two pixels."""
    return max(2, int(round(n)) - (int(round(n)) % 2))


def _even_at(n: float) -> int:
    """The same rounding for a position, where zero is a real answer.

    A coordinate is not a size: pushing it to two pixels because nothing
    can be one pixel wide puts a gap along the top and left of anything
    that should sit flush against the edge.
    """
    whole = int(round(max(0.0, n)))
    return whole - (whole % 2)


def _fit(max_w: float, max_h: float, aspect: float) -> tuple[int, int]:
    """Largest even-sided box within (max_w, max_h) holding `aspect`.

    Rounding each side to even independently distorts the ratio by up to a
    pixel each way — visible as a stretch on a wide, short band. So the
    height is rounded first, then the width is recomputed from that actual
    height, which converges to a far closer match than one pass.
    """
    width = min(max_w, max_h * aspect)
    height = _even(width / aspect)
    width = _even(height * aspect)
    if width > max_w:                       # rounding nudged it over
        width = _even(max_w)
        height = _even(width / aspect)
    return width, height


def compute_layout(style: Style, band_aspect: float,
                   video_aspect: float) -> Layout:
    """Place the band and the video, each keeping its own aspect ratio.

    The band spans the content width and takes whatever height its aspect
    demands. The video also spans that width — fitting it inside the space
    left over would shrink it and leave a bar down each side — so its
    height is whatever its aspect demands, and the surplus is cropped.
    `style.video_offset` chooses which slice of it survives.
    """
    width, height = style.canvas
    panel_w = _even(width * style.panel_width) if style.panel else 0
    panel = Rect(0, 0, panel_w, height) if panel_w else None

    content_x = panel_w + style.margin
    content_w = width - panel_w - 2 * style.margin
    content_y = style.margin
    content_h = height - 2 * style.margin
    if content_w < 16 or content_h < 16:
        raise RenderError("The canvas is too small for this panel and margin.")

    # The band spans the content width, taking whatever height its own
    # aspect demands — never stretched to fill.
    band_w, band_h = _fit(content_w, content_h, band_aspect)
    if band_h >= content_h:
        raise RenderError(
            "The score band alone is taller than the frame. Reduce the panel "
            "width, or choose a wider aspect ratio.")

    video_box_h = content_h - band_h - style.gap
    if video_box_h < 16:
        raise RenderError("No room left for the video beside the band.")

    # The video fills the space beside the band completely, in both
    # directions, and whatever overflows is cropped. Fitting it inside
    # instead would leave a bar down the sides on a wide frame and a band
    # of dead space above it on a narrow one — which is what happened
    # beside a title panel, where the picture is too short for its column.
    video_w = _even(content_w)
    video_h = _even(video_box_h)

    # Scale until it covers the box on both axes, then take the middle of
    # whatever is left over horizontally and the chosen slice vertically.
    scale = max(video_w / video_aspect, video_h)
    source_h = _even(scale)
    source_w = _even(source_h * video_aspect)
    if source_w < video_w:                       # rounding nudged it under
        source_w = video_w
        source_h = _even(source_w / video_aspect)

    video_crop_x = _even_at((source_w - video_w) / 2)
    video_crop_y = _even_at(max(0, source_h - video_h) * _clamp01(style.video_offset))

    video_x = content_x
    band_x = content_x + _even_at((content_w - band_w) / 2)

    # The video is pushed against the band rather than centred in what is
    # left, so the two always meet. Where the picture is shorter than its
    # box — a narrower column beside a panel — the slack goes to the far
    # edge instead of opening a seam down the middle.
    if style.band_position == TOP:
        band_y = content_y
        video_y = content_y + band_h + style.gap
    else:
        band_y = content_y + video_box_h + style.gap
        video_y = band_y - style.gap - video_h

    return Layout(canvas=(width, height),
                  band=Rect(band_x, band_y, band_w, band_h),
                  video=Rect(video_x, video_y, video_w, video_h),
                  panel=panel,
                  video_source=(source_w, source_h),
                  video_crop_x=video_crop_x,
                  video_crop_y=video_crop_y)


def _clamp01(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else float(value)


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------
def probe(video: pathlib.Path) -> dict:
    """Duration, dimensions, fps and stream presence."""
    try:
        out = subprocess.run(
            [settings.ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(video)],
            capture_output=True, text=True, check=True,
            # A malformed file can make ffprobe hunt for a stream header
            # indefinitely. Without a bound that hangs a request thread at
            # the upload gate, where anyone can reach it — and enough of
            # them stop the app answering at all.
            timeout=60).stdout
        info = json.loads(out)
    except subprocess.TimeoutExpired as exc:
        raise RenderError(
            "That file could not be read within a reasonable time. It may "
            "be damaged, or not really a video.") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RenderError(f"Could not read {video.name}: {exc}") from exc

    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RenderError(f"{video.name} has no video track.")

    duration = float(info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise RenderError(f"{video.name} has no readable duration.")

    fps = 25.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = v.get(key) or ""
        if "/" in raw:
            num, den = raw.split("/", 1)
            if float(den or 0):
                fps = float(num) / float(den)
                break

    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    if not width or not height:
        raise RenderError(f"{video.name} reports no frame size.")
    # Respect anamorphic sources when working out the true display aspect.
    sar = str(v.get("sample_aspect_ratio") or "1:1").replace(":", "/")
    try:
        sn, sd = (float(x) for x in sar.split("/"))
        pixel = sn / sd if sd else 1.0
    except (ValueError, ZeroDivisionError):
        pixel = 1.0

    return {"duration": duration, "fps": round(fps, 3),
            "width": width, "height": height,
            "aspect": (width * (pixel or 1.0)) / height,
            "has_audio": a is not None}


# --------------------------------------------------------------------------
# band preparation
# --------------------------------------------------------------------------
def _rasterize(band: Band, size: tuple[int, int]) -> Image.Image:
    """Band artwork at exactly `size`, as opaque greyscale-able RGB.

    Vector bands are rasterised at the output resolution rather than
    resampled from a fixed bitmap, which is the whole reason to prefer
    them. They are rendered onto white so that the recolouring below sees
    the same ink-on-paper signal it gets from a PNG — one code path serves
    both, and transparency is decided afterwards rather than inherited
    from whatever the SVG happened to declare.
    """
    width, height = size
    if band.is_vector:
        try:
            return svg.rasterize(band.path, width, height)
        except RuntimeError as exc:
            raise RenderError(
                f"This package ships vector bands, but they can't be "
                f"rasterised: {exc}. Re-export the package with --fmt-png "
                f"to use raster bands instead.") from exc

    img = Image.open(band.path)
    if img.mode in ("RGBA", "LA", "P"):
        # Flatten onto white so partially transparent source art doesn't
        # read as black ink once converted to greyscale.
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        img = flat
    else:
        img = img.convert("RGB")
    return img.resize(size, Image.LANCZOS) if img.size != size else img


def _recolour(img: Image.Image, style: Style) -> Image.Image:
    """Map the band's greyscale onto a two-colour ramp, with transparency.

    Score bands are dark ink on light paper, so luminance alone carries the
    notation: black becomes the note colour, white the paper colour, and the
    anti-aliased greys between them interpolate. Applying this after
    rasterisation means the colour controls work identically for vector and
    raster bands.

    Alpha follows the same ramp, so `band_bg_opacity` fades the paper while
    leaving the notes solid — set it to 0 and the notation floats directly
    over the video. `band_opacity` then fades the whole band on top of that.
    """
    grey = ImageOps.grayscale(img)
    out = ImageOps.colorize(grey, black=style.band_fg,
                            white=style.band_bg).convert("RGBA")

    paper = max(0.0, min(1.0, style.band_bg_opacity))
    overall = max(0.0, min(1.0, style.band_opacity))
    if paper < 1.0 or overall < 1.0:
        # v=0 is ink (stays opaque), v=255 is paper (fades to `paper`).
        span = 255.0 - paper * 255.0
        out.putalpha(grey.point(
            lambda v: int(round((255.0 - span * (v / 255.0)) * overall))))
    return out


def prepare_bands(pkg: ScorePackage, size: tuple[int, int], style: Style,
                  on_progress: ProgressFn = _noop) -> dict[int, pathlib.Path]:
    """Render every band to a PNG at output size. Cached by package+style+size."""
    # The source format is part of the key: the same package renders
    # differently once a rasteriser becomes available, and cached bands
    # derived from the PNG twins must not be reused for vector output.
    source = "svg" if any(b.is_vector for b in pkg.bands) else "raster"
    cache = (settings.cache_dir /
             f"{pkg.name[:40]}-{style.key()}-{source}-{size[0]}x{size[1]}")
    cache.mkdir(parents=True, exist_ok=True)

    out: dict[int, pathlib.Path] = {}
    for i, band in enumerate(pkg.bands):
        dest = cache / f"{band.first_measure}.png"
        if not dest.is_file():
            try:
                _recolour(_rasterize(band, size), style).save(dest, "PNG")
            except RenderError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RenderError(f"Could not prepare band "
                                  f"{band.first_measure}: {exc}") from exc
        out[band.first_measure] = dest
        if i % 10 == 0:
            on_progress("bands", f"{i + 1}/{len(pkg.bands)}")
    on_progress("bands", f"{len(pkg.bands)} bands at {size[0]}x{size[1]}")
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
class ToolFailed(RenderError):
    """A tool exited non-zero, carrying enough to reproduce the run.

    The filter graphs here run to hundreds of characters and are assembled
    from the visitor's own crop, colours and panel choice, so without the
    exact command a failure cannot be repeated — which in practice meant it
    could not be fixed. `command` is the argument list, ready to re-run or
    to paste after shlex.join.
    """

    def __init__(self, what: str, cmd: list[str], returncode: int,
                 stderr: str):
        self.what = what
        self.command = list(cmd)
        self.returncode = returncode
        self.stderr = stderr or ""
        tail = self.stderr.strip().splitlines()[-6:]
        super().__init__(f"{what} failed:\n" + "\n".join(tail))


def _run(cmd: list[str], what: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Logged as well as carried: what a visitor is shown must not
        # contain a command line, and an operator needs exactly that.
        logger.error("%s failed (exit %s)\n  %s\n%s", what, result.returncode,
                     shlex.join(cmd), (result.stderr or "").strip()[-4000:])
        raise ToolFailed(what, cmd, result.returncode, result.stderr or "")


def _band_strip(pkg: ScorePackage, images: dict[int, pathlib.Path],
                duration: float, fps: float, size: tuple[int, int],
                workdir: pathlib.Path, keep_alpha: bool = False) -> pathlib.Path:
    """The silent band video, whose cuts land on measure timestamps."""
    schedule = pkg.band_schedule(duration)
    if not schedule:
        raise RenderError("The alignment doesn't cover this video.")

    listing = workdir / "bands.txt"
    last: pathlib.Path | None = None
    with open(listing, "w", encoding="utf-8") as fh:
        for band, start, end in schedule:
            path = images.get(band.first_measure)
            if path is None:
                continue
            fh.write(f"file '{path.as_posix()}'\n")
            fh.write(f"duration {max(end - start, 0.001):.6f}\n")
            last = path
        if last is None:
            raise RenderError("No band images were prepared for this package.")
        # The concat demuxer ignores the last entry's duration unless the
        # file is repeated, which would otherwise clip the final band.
        fh.write(f"file '{last.as_posix()}'\n")

    # yuv420p discards alpha, so a transparent band needs a codec that keeps
    # it. QuickTime RLE is lossless, fast, and widely supported; it is only
    # used when transparency is actually asked for, since it is much larger.
    if keep_alpha:
        strip = workdir / "band.mov"
        codec = ["-c:v", "qtrle", "-pix_fmt", "argb"]
        pixfmt = "rgba"
    else:
        strip = workdir / "band.mp4"
        codec = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast"]
        pixfmt = "yuv420p"

    _run([settings.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
          "-vf", f"scale={size[0]}:{size[1]},format={pixfmt}",
          "-r", str(round(fps))] + codec + [str(strip)],
         "Building the score band")
    return strip


def _background_input(style: Style, canvas: tuple[int, int],
                      fps: float) -> list[str]:
    """ffmpeg input args for the backdrop, whichever kind it is."""
    width, height = canvas
    if style.background == STATIC:
        return ["-loop", "1", "-framerate", str(round(fps)),
                "-i", str(style.background_path)]
    if style.background == DYNAMIC:
        return ["-stream_loop", "-1", "-i", str(style.background_path)]
    colour = style.canvas_bg.lstrip("#")
    return ["-f", "lavfi",
            "-i", f"color=c=0x{colour}:s={width}x{height}:r={round(fps)}"]


def render(pkg: ScorePackage, video: pathlib.Path, output: pathlib.Path,
           style: Style | None = None, meta: dict | None = None,
           on_progress: ProgressFn = _noop) -> pathlib.Path:
    """Composite `video` with `pkg`'s score band. Returns `output`."""
    style = style or Style()
    style.validate()

    on_progress("probe", video.name)
    info = probe(video)
    native_w, native_h = pkg.band_size
    layout = compute_layout(style, native_w / native_h, info["aspect"])

    on_progress("bands", f"preparing bands at {layout.band.w}x{layout.band.h}")
    images = prepare_bands(pkg, (layout.band.w, layout.band.h), style, on_progress)

    output.parent.mkdir(parents=True, exist_ok=True)
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="videosync-"))
    temps: list[str] = []
    try:
        on_progress("strip", "timing bands to the performance")
        strip = _band_strip(pkg, images, info["duration"], info["fps"],
                            (layout.band.w, layout.band.h), workdir,
                            keep_alpha=style.needs_alpha)

        width, height = layout.canvas
        # Inputs: 0 = backdrop, 1 = performance, 2 = band strip.
        cmd = [settings.ffmpeg, "-y"]
        cmd += _background_input(style, layout.canvas, info["fps"])
        cmd += ["-i", str(video), "-i", str(strip)]

        chain = [
            # The backdrop keeps its aspect too: scaled to cover the canvas
            # and centre-cropped, never squeezed. A template designed for one
            # canvas shape will still be cropped when forced into another —
            # that is a limitation of the artwork, not of the scaling.
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,format=yuv420p[bg]",
            f"[1:v]setpts=PTS-STARTPTS,"
            f"scale={layout.video_source[0]}:{layout.video_source[1]},"
            f"crop={layout.video.w}:{layout.video.h}:"
            f"{layout.video_crop_x}:{layout.video_crop_y}[vid]",
            f"[bg][vid]overlay=x={layout.video.x}:y={layout.video.y}[s1]",
            f"[2:v]setpts=PTS-STARTPTS,scale={layout.band.w}:{layout.band.h}[bnd]",
            f"[s1][bnd]overlay=x={layout.band.x}:y={layout.band.y}:shortest=1[s2]",
        ]
        last_label = "s2"

        if layout.panel is not None and meta:
            on_progress("panel", "drawing the title panel")
            text_chain, temps = panel_mod.build_chain(
                last_label, "out", layout.panel, layout.canvas, meta)
            if text_chain:
                chain.append(text_chain)
                last_label = "out"

        if last_label != "out":
            chain.append(f"[{last_label}]null[out]")

        cmd += ["-filter_complex", ";".join(chain), "-map", "[out]"]
        if info["has_audio"]:
            cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-crf", str(style.crf), "-preset", "medium",
                "-pix_fmt", "yuv420p", "-t", f"{info['duration']:.3f}",
                str(output)]

        on_progress("encode", f"{width}x{height} · band {layout.band.w}x"
                              f"{layout.band.h} {style.band_position}"
                              f"{' · panel' if layout.panel else ''}"
                              f" · bg {style.background}")
        _run(cmd, "Rendering the video")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        for path in temps:
            pathlib.Path(path).unlink(missing_ok=True)

    if not output.is_file():
        raise RenderError("ffmpeg reported success but produced no file.")
    on_progress("done", f"{output.stat().st_size / 1e6:.1f} MB")
    return output
