"""Render one frame of every template, for every input shape, as contact sheets.

The checks prove the geometry: nothing is cropped in portrait, nothing leaves
the frame, the preview matches the renderer. None of them can tell you
whether the result LOOKS right, and three defects have shipped here that were
green in every check and obvious the moment somebody looked at the page.

So this looks. It builds a source clip in each shape, renders a single frame
of every template that shape can use, and lays them out side by side with
their settings written underneath.

    python tools/preview_matrix.py                     # find a video itself
    python tools/preview_matrix.py --video FILE        # use this recording
    python tools/preview_matrix.py --out DIR           # where the sheets go

One sheet per output shape, because the output frame is always the
recording's own shape - there is no chooser, and what changes between shapes
is the template. So the sheets answer "what can a 16:9 recording look like",
not "what shapes can I pick".

Needs ffmpeg and a score package, so it is NOT part of `selftest.py`, which
runs where neither exists. Run it by hand after touching `compute_layout` or
anything it calls.
"""

import argparse
import math
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw, ImageFont        # noqa: E402

from app import library                            # noqa: E402
from app import render as rnd                      # noqa: E402

# The shapes a recording actually arrives in, and the frame each one maps to.
# `nearestFrame` in the interface picks by log-ratio, so 4:3 goes to square
# rather than to widescreen - that is the interesting case to see, because
# the square frame crops a 4:3 picture by about a third.
SOURCES = [
    ("21x9", 2560, 1080),      # cinematic / ultrawide
    ("16x9", 1920, 1080),      # the ordinary one
    ("16x10", 1920, 1200),     # a laptop screen recording
    ("3x2", 1620, 1080),       # a camera's native stills shape
    ("4x3", 1440, 1080),       # older cameras, and most archive footage
    ("5x4", 1350, 1080),
    ("1x1", 1080, 1080),
    ("4x5", 1080, 1350),       # the Instagram portrait post
    ("3x4", 1080, 1440),
    ("9x16", 1080, 1920),      # a phone held upright
]

LABEL_H = 46
PAD = 18


def nearest_frame(ratio: float) -> str:
    """The frame a recording of this shape lands in.

    THE SAME RULE AS `nearestFrame` IN svs-min.js, and it has to stay that
    way: the interface decides the frame from the probe, and if these two
    disagree the sheets show a layout no visitor will ever get.

    Nearest by log-ratio rather than by difference, so the comparison is
    symmetric - 4:3 is closer to square than to widescreen, and picking
    "closest" on raw numbers would send it to 16:9 because 16:9 is a bigger
    number and the gaps are not comparable.
    """
    best, gap = "16/9", float("inf")
    for name in ("16/9", "1/1", "9/16"):
        w, h = rnd.ASPECTS[name]
        distance = abs(math.log(ratio / (w / h)))
        if distance < gap:
            gap, best = distance, name
    return best


def _font(size: int = 18):
    for name in ("DejaVuSans.ttf", "arial.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def find_video() -> pathlib.Path | None:
    """A real recording, so the frames show a piano rather than a test card."""
    roots = [pathlib.Path(r"C:\ZZ_perso\weefeen\Chopin_Companion"
                          r"\Videos_downloaded_youtube")]
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("*op_39*.mp4", "*.mp4"):
            found = sorted(root.glob(pattern))
            if found:
                return found[0]
    return None


def make_source(video: pathlib.Path, out: pathlib.Path,
                w: int, h: int, seconds: float = 1.0) -> pathlib.Path:
    """One second of `video`, filling a `w`x`h` frame.

    Scaled to cover and centre-cropped, so every source shape shows the same
    moment of the same performance and the only thing differing between
    sheets is the shape itself.
    """
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", "8", "-t", str(seconds),
         "-i", str(video),
         "-vf", (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                 f"crop={w}:{h},setsar=1"),
         "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-an",
         str(out)], check=True)
    return out


def templates_for(frame: str) -> list[tuple[str, rnd.Style]]:
    """Every template that frame can use, named for the sheet."""
    if frame == "9/16":
        # No panel: the renderer refuses one, because a column in a frame
        # 1080 across leaves too little picture. What varies instead is where
        # the group sits, which is the control that exists because no app
        # publishes where its own buttons are.
        return [(f"band {pos} · height {int(off * 100)}%",
                 rnd.Style(aspect=frame, band_position=pos,
                           portrait_offset=off))
                for pos in ("bottom", "top")
                for off in (0.0, 0.32, 1.0)]
    return [(f"panel {panel} · band {pos}",
             rnd.Style(aspect=frame, panel=panel, band_position=pos))
            for panel in ("off", "left", "centered")
            for pos in ("bottom", "top")]


# The panel is a column of TITLE TEXT, and it is only drawn when there is
# text to draw. Rendered with none, a "panel left" frame is an empty dark
# column that looks like dead space rather than like the layout - which is
# exactly the wrong thing to be judging a design by.
PANEL_TEXT = {
    "round_name": "Masterclass",
    "subtitle": "Weefeen",
    "name": "Julien Bedon",
    "country_age": "France",
    "composer": "Fryderyk Chopin",
    "composition": "Scherzo No. 3 in C sharp minor, Op. 39",
}


def frame_of(pkg, source: pathlib.Path, style: rnd.Style,
             work: pathlib.Path, name: str) -> Image.Image:
    """Render with this style and return the first frame."""
    out = work / f"{name}.mp4"
    rnd.render(pkg, source, out, style=style, meta=PANEL_TEXT,
               on_progress=lambda s, d: None)
    png = work / f"{name}.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(out),
                    "-frames:v", "1", str(png)], check=True)
    return Image.open(png).convert("RGB")


def sheet(frames: list[tuple[str, Image.Image]], title: str,
          cell_h: int = 420) -> Image.Image:
    """Lay the variants out in a row, each with its settings under it."""
    scaled = []
    for label, img in frames:
        w = max(1, round(img.width * cell_h / img.height))
        scaled.append((label, img.resize((w, cell_h), Image.LANCZOS)))

    width = PAD + sum(i.width + PAD for _, i in scaled)
    height = PAD + 34 + cell_h + LABEL_H + PAD
    canvas = Image.new("RGB", (width, height), "#15121b")
    draw = ImageDraw.Draw(canvas)
    draw.text((PAD, PAD), title, fill="#f6f1e8", font=_font(22))

    x = PAD
    for label, img in scaled:
        canvas.paste(img, (x, PAD + 34))
        draw.text((x, PAD + 34 + cell_h + 12), label, fill="#b9aecb",
                  font=_font(16))
        draw.text((x, PAD + 34 + cell_h + 30),
                  f"{img.width}\u00d7{img.height} shown", fill="#6f6480",
                  font=_font(13))
        x += img.width + PAD
    return canvas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", help="a recording to take the frames from")
    parser.add_argument("--out", default="preview", help="where sheets go")
    args = parser.parse_args()

    video = pathlib.Path(args.video) if args.video else find_video()
    if video is None or not video.is_file():
        print("no video to render. Pass one with --video.")
        return 1

    packages = library.packages()
    if not packages:
        print("no score package is installed here, so there is nothing to "
              "put in the band. Check SCORE_ROOT_DIGITAL.")
        return 1
    pkg = packages[0]

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="preview-matrix-"))

    print(f"recording : {video.name}")
    print(f"score     : {pkg.name}")
    print(f"sheets to : {out_dir.resolve()}")
    print()

    written = []
    started = time.time()
    for name, w, h in SOURCES:
        source = make_source(video, work / f"src-{name}.mp4", w, h)
        frame = nearest_frame(w / h)
        cells = []
        for label, style in templates_for(frame):
            tag = f"{name}-{label.replace(' ', '').replace('·', '-')}"
            t0 = time.time()
            cells.append((label, frame_of(pkg, source, style, work, tag)))
            print(f"  {name:>5} {label:<34} {time.time() - t0:5.1f}s")
        title = (f"input {w}\u00d7{h}  \u2192  output "
                 f"{rnd.ASPECTS[frame][0]}\u00d7{rnd.ASPECTS[frame][1]}"
                 f"    ({len(cells)} templates)")
        path = out_dir / f"{name}.png"
        sheet(cells, title).save(path)
        written.append(path)
        print(f"  -> {path}")
        print()

    print(f"{len(written)} sheets in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
