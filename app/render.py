"""Compose a performance video with its score band burned in.

This is the whole job of this app. The alignment is already decided
upstream — `package.measures.data` says which measure sounds when — so
rendering is: pick the right band image for each moment, style it, and
composite it over the video.

Two ffmpeg passes, because it is far easier to reason about than one:

    1. the band images become a silent video whose cuts land on the
       measure timestamps (concat demuxer, one entry per segment)
    2. that strip is overlaid on the performance video, audio copied

Bands are prepared with Pillow first (rasterise vector at the exact output
size, or resample raster; then recolour), so ffmpeg only ever sees ready
PNGs at final resolution.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import shutil
import subprocess
import tempfile
from typing import Callable

from PIL import Image, ImageOps

from .package import Band, ScorePackage
from .settings import settings

ProgressFn = Callable[[str, str], None]


def _noop(stage: str, detail: str = "") -> None:
    pass


# Output canvases offered by the UI.
ASPECTS = {
    "16/9": (1920, 1080),
    "1/1": (1080, 1080),
    "9/16": (1080, 1920),
}


class RenderError(RuntimeError):
    """Rendering failed. Message is safe to show the user."""


@dataclasses.dataclass(frozen=True)
class Style:
    """Everything the UI can change about the look of the output."""
    aspect: str = "16/9"
    band_position: str = "bottom"      # top | bottom
    band_width: float = 1.0            # fraction of canvas width
    band_bg: str = "#ffffff"           # paper colour behind the notes
    band_fg: str = "#1c1622"           # the notes themselves
    band_opacity: float = 1.0
    band_margin: int = 0               # px inset from the frame edge
    crf: int = 20                      # x264 quality; lower is better

    @property
    def canvas(self) -> tuple[int, int]:
        if self.aspect not in ASPECTS:
            raise RenderError(f"Unsupported aspect {self.aspect!r}. "
                              f"Choose from {', '.join(ASPECTS)}.")
        return ASPECTS[self.aspect]

    def band_size(self, native: tuple[int, int]) -> tuple[int, int]:
        """Band pixel size on this canvas, preserving the source aspect."""
        canvas_w, _ = self.canvas
        width = max(1, int(canvas_w * self.band_width) - 2 * self.band_margin)
        native_w, native_h = native
        height = max(1, round(width * native_h / native_w))
        return width - width % 2, height - height % 2   # even dims for x264

    def key(self) -> str:
        """Short hash identifying prepared bands for this style."""
        blob = json.dumps(dataclasses.asdict(self), sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:10]


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------
def probe(video: pathlib.Path) -> dict:
    """Duration, dimensions and stream presence for a video file."""
    try:
        out = subprocess.run(
            [settings.ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(video)],
            capture_output=True, text=True, check=True).stdout
        info = json.loads(out)
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
    return {"duration": duration, "fps": round(fps, 3),
            "width": v.get("width"), "height": v.get("height"),
            "has_audio": a is not None}


# --------------------------------------------------------------------------
# band preparation
# --------------------------------------------------------------------------
def _rasterize(band: Band, size: tuple[int, int]) -> Image.Image:
    """Band image at exactly `size`, from vector when available."""
    width, height = size
    if band.is_vector:
        try:
            import cairosvg
        except ImportError as exc:   # guarded upstream, belt and braces
            raise RenderError(
                "This package ships vector bands but cairosvg isn't "
                "installed. Run `pip install cairosvg`.") from exc
        png = cairosvg.svg2png(url=str(band.path),
                               output_width=width, output_height=height)
        import io
        return Image.open(io.BytesIO(png)).convert("RGBA")

    img = Image.open(band.path).convert("RGBA")
    if img.size != size:
        img = img.resize(size, Image.LANCZOS)
    return img


def _recolour(img: Image.Image, bg: str, fg: str, opacity: float) -> Image.Image:
    """Map the band's greyscale onto a two-colour ramp.

    Score bands are dark ink on light paper, so luminance alone carries the
    notation: black maps to the note colour, white to the paper colour, and
    the anti-aliased greys in between interpolate. That gives the UI real
    colour control over raster bands, not just vector ones.
    """
    grey = ImageOps.grayscale(img)
    tinted = ImageOps.colorize(grey, black=fg, white=bg).convert("RGBA")
    if opacity < 1.0:
        alpha = tinted.getchannel("A").point(lambda v: int(v * opacity))
        tinted.putalpha(alpha)
    return tinted


def prepare_bands(pkg: ScorePackage, style: Style,
                  on_progress: ProgressFn = _noop) -> dict[int, pathlib.Path]:
    """Render every band to a PNG at output size. Cached by package+style."""
    native = pkg.band_size
    size = style.band_size(native)
    cache = settings.cache_dir / f"{pkg.name}-{style.key()}-{size[0]}x{size[1]}"
    cache.mkdir(parents=True, exist_ok=True)

    out: dict[int, pathlib.Path] = {}
    for i, band in enumerate(pkg.bands):
        dest = cache / f"{band.first_measure}.png"
        if not dest.is_file():
            try:
                img = _recolour(_rasterize(band, size),
                                style.band_bg, style.band_fg, style.band_opacity)
                img.save(dest, format="PNG")
            except RenderError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RenderError(f"Could not prepare band "
                                  f"{band.first_measure}: {exc}") from exc
        out[band.first_measure] = dest
        if i % 10 == 0:
            on_progress("bands", f"{i + 1}/{len(pkg.bands)}")
    on_progress("bands", f"{len(pkg.bands)} bands ready at {size[0]}x{size[1]}")
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def _run(cmd: list[str], what: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-6:]
        raise RenderError(f"{what} failed:\n" + "\n".join(tail))


def _band_strip(pkg: ScorePackage, images: dict[int, pathlib.Path],
                duration: float, fps: float, size: tuple[int, int],
                workdir: pathlib.Path) -> pathlib.Path:
    """Build the silent band video whose cuts land on measure timestamps."""
    schedule = pkg.band_schedule(duration)
    if not schedule:
        raise RenderError("The package's alignment doesn't cover this video.")

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
        # The concat demuxer ignores the final entry's duration unless the
        # file is repeated once more, which would otherwise clip the last band.
        fh.write(f"file '{last.as_posix()}'\n")

    strip = workdir / "band.mp4"
    _run([settings.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
          "-vf", f"scale={size[0]}:{size[1]},format=yuv420p",
          "-r", str(round(fps)), "-c:v", "libx264", "-crf", "18",
          "-preset", "veryfast", str(strip)], "Building the score band")
    return strip


def render(pkg: ScorePackage, video: pathlib.Path, output: pathlib.Path,
           style: Style | None = None,
           on_progress: ProgressFn = _noop) -> pathlib.Path:
    """Composite `video` with `pkg`'s score band. Returns `output`."""
    style = style or Style()
    if style.band_position not in ("top", "bottom"):
        raise RenderError(f"band_position must be 'top' or 'bottom', "
                          f"got {style.band_position!r}")

    on_progress("probe", video.name)
    info = probe(video)
    canvas_w, canvas_h = style.canvas
    size = style.band_size(pkg.band_size)

    on_progress("bands", "preparing score bands")
    images = prepare_bands(pkg, style, on_progress)

    output.parent.mkdir(parents=True, exist_ok=True)
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="videosync-"))
    try:
        on_progress("strip", "timing bands to the performance")
        strip = _band_strip(pkg, images, info["duration"], info["fps"], size, workdir)

        x = (canvas_w - size[0]) // 2
        y = style.band_margin if style.band_position == "top" \
            else canvas_h - size[1] - style.band_margin

        # Video fills the canvas (cover-crop), band rides on top.
        filters = (
            f"[1:v]scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
            f"crop={canvas_w}:{canvas_h},setsar=1[bg];"
            f"[0:v]setpts=PTS-STARTPTS[band];"
            f"[bg][band]overlay=x={x}:y={y}:shortest=1[out]"
        )
        cmd = [settings.ffmpeg, "-y", "-i", str(strip), "-i", str(video),
               "-filter_complex", filters, "-map", "[out]"]
        if info["has_audio"]:
            cmd += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"]
        cmd += ["-c:v", "libx264", "-crf", str(style.crf), "-preset", "medium",
                "-pix_fmt", "yuv420p", "-t", f"{info['duration']:.3f}", str(output)]

        on_progress("encode", f"{canvas_w}x{canvas_h}, {info['duration'] / 60:.1f} min")
        _run(cmd, "Rendering the video")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if not output.is_file():
        raise RenderError("ffmpeg reported success but produced no file.")
    on_progress("done", f"{output.stat().st_size / 1e6:.1f} MB")
    return output
