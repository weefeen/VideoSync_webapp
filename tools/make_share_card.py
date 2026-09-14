"""Build the card that shows when somebody shares the site.

    python tools/make_share_card.py VIDEO [--out app/static/svs/og-cover.jpg]

WHY IT IS MADE AND NOT PHOTOGRAPHED. A share card is read at about 500
pixels wide in a feed, next to cards that are mostly type. A bare video
frame loses that comparison every time: a photograph of a dark piano in a
pale hall is the hardest thing JPEG has to carry, and ours comes from a
performance uploaded at 1.5 Mbps. Type drawn here is vector-sharp at any
size, so the card reads as considered even where the photograph behind it
cannot.

THE FRAME IS CHOSEN, NOT TAKEN. In a low-bitrate encode a keyframe holds
far more detail than the predicted frames around it, so this extracts the
keyframes and measures each one -- looking only at the PERFORMANCE half,
because the engraved band below is vector-sharp in every frame and would
drown out the difference being looked for.

It uses the site's own typefaces and palette, so the card and the page it
opens are recognisably the same thing.
"""

from __future__ import annotations

import argparse
import glob
import pathlib
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# The shape Facebook, Twitter and LinkedIn all lay out. Below 1200x630 the
# card degrades to a thumbnail beside the text, which is their decision
# and not ours.
WIDTH, ASPECT = 1920, 1.91

# The site's own palette, from app/static/svs/index.html.
PAPER = (246, 241, 232)
INK = (28, 22, 34)
MAGENTA = (204, 35, 126)

HEADLINE = "Your Chopin,\nwith the score\nplaying along"
KICKER = "CHOPIN.WEEFEEN.COM"
NOTE = "Free"


def keyframes(video: pathlib.Path, into: pathlib.Path, seconds: int) -> list:
    """Every keyframe in the first `seconds`, as files."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-skip_frame", "nokey",
         "-i", str(video), "-t", str(seconds), "-vsync", "0", "-q:v", "1",
         str(into / "kf_%03d.png")],
        check=True)
    return sorted(glob.glob(str(into / "kf_*.png")))


def sharpest(paths: list):
    """The frame with the most detail in its upper, photographic half."""
    from PIL import Image
    import numpy as np

    best, best_score = None, -1.0
    for path in paths:
        with Image.open(path) as im:
            grey = im.convert("L")
            a = np.asarray(grey.crop((0, 0, grey.width,
                                      int(grey.height * 0.66))), float)
        lap = (-4 * a[1:-1, 1:-1] + a[:-2, 1:-1] + a[2:, 1:-1]
               + a[1:-1, :-2] + a[1:-1, 2:])
        score = float(lap.var())
        if score > best_score:
            best, best_score = path, score
    return best, best_score


def _font(size: int, which: str = "display"):
    """The site's typeface at this size, or a reasonable stand-in.

    Fraunces and Inter are fetched from Google's font service into the
    cache beside this file. A machine that cannot reach it still produces
    a card -- with a different face, which is visibly worse but not a
    failure.
    """
    from PIL import ImageFont
    import urllib.request

    urls = {
        "display": ("https://fonts.googleapis.com/css2?family=Fraunces:"
                    "opsz,wght@144,500&display=swap"),
        "body": "https://fonts.googleapis.com/css2?family=Inter:wght@500",
    }
    cache = pathlib.Path(__file__).resolve().parent / ".fontcache"
    cache.mkdir(exist_ok=True)
    local = cache / f"{which}.ttf"

    if not local.is_file():
        try:
            import re
            req = urllib.request.Request(
                urls[which], headers={"User-Agent": "Mozilla/5.0"})
            css = urllib.request.urlopen(req, timeout=30).read().decode()
            url = re.search(r"https://[^)]*\.ttf", css).group(0)
            local.write_bytes(
                urllib.request.urlopen(url, timeout=60).read())
        except Exception as exc:                       # noqa: BLE001
            print(f"  could not fetch the {which} face ({exc}); "
                  f"falling back", file=sys.stderr)
            for name in ("georgiab.ttf", "georgia.ttf", "arialbd.ttf"):
                where = pathlib.Path("C:/Windows/Fonts") / name
                if where.is_file():
                    return ImageFont.truetype(str(where), size)
            return ImageFont.load_default()
    return ImageFont.truetype(str(local), size)


def draw(frame: pathlib.Path, out: pathlib.Path) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    src = Image.open(frame).convert("RGB")
    height = round(WIDTH / ASPECT)
    # Cropped off the TOP: the engraved band runs along the bottom and is
    # the whole point of the picture.
    card = src.crop((0, src.height - height, src.width, src.height))
    if card.width != WIDTH:
        card = card.resize((WIDTH, height), Image.LANCZOS)

    # Restore the edge contrast compression flattened. Light on purpose:
    # the detail is not there to recover, and a heavier hand buys halos
    # that look worse than the softness they replace.
    card = card.filter(ImageFilter.UnsharpMask(radius=1.4, percent=70,
                                               threshold=3))

    # A SCRIM, NOT A BOX. The headline sits on the photograph, so it needs
    # the photograph darkened under it -- but a rectangle would read as a
    # sticker laid on top. This is a horizontal ramp: opaque at the left
    # edge where the words are, gone by the middle where the piano is.
    band_top = int(height * 0.66)
    scrim = Image.new("L", (WIDTH, band_top), 0)
    ramp = ImageDraw.Draw(scrim)
    for x in range(WIDTH):
        t = min(1.0, max(0.0, (x - WIDTH * 0.10) / (WIDTH * 0.52)))
        ramp.line([(x, 0), (x, band_top)], fill=int(238 * (1 - t) ** 1.5))
    # AND IT FADES AT THE FOOT. A scrim that stops where the engraved band
    # begins draws a straight dark edge across the picture, which reads as
    # a panel pasted on rather than as light. The last eighth ramps out so
    # the photograph meets the paper without a seam.
    foot = Image.new("L", (WIDTH, band_top), 255)
    fade = ImageDraw.Draw(foot)
    start = int(band_top * 0.80)
    for y in range(start, band_top):
        k = (y - start) / max(1, band_top - start)
        fade.line([(0, y), (WIDTH, y)], fill=int(255 * (1 - k)))
    from PIL import ImageChops
    scrim = ImageChops.multiply(scrim, foot)
    dark = Image.new("RGB", (WIDTH, band_top), INK)
    card.paste(dark, (0, 0), scrim)

    pen = ImageDraw.Draw(card)
    left = int(WIDTH * 0.055)

    # The kicker, letter-spaced by hand because PIL has no tracking.
    small = _font(26, "body")
    x = left
    for ch in KICKER:
        pen.text((x, int(height * 0.105)), ch, font=small,
                 fill=(233, 226, 240))
        x += pen.textlength(ch, font=small) + 3.4

    big = _font(78, "display")
    y = int(height * 0.20)
    for line in HEADLINE.split("\n"):
        pen.text((left, y), line, font=big, fill=PAPER)
        y += 89

    # The one piece of colour, and the word that matters most.
    rule_y = y + 26
    pen.rounded_rectangle([left, rule_y, left + 58, rule_y + 5], radius=3,
                          fill=MAGENTA)
    free = _font(34, "body")
    pen.text((left + 78, rule_y - 13), NOTE, font=free, fill=PAPER)

    # 4:4:4 rather than the default 4:2:0: this is a dark piano against a
    # pale hall and cream type on near-black, and colour subsampling shows
    # on exactly those edges.
    card.save(out, quality=95, subsampling=0, optimize=True,
              progressive=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", help="a rendered score-video to take the frame from")
    ap.add_argument("--out", default="app/static/svs/og-cover.jpg")
    ap.add_argument("--seconds", type=int, default=240,
                    help="how far into the video to look for a frame")
    args = ap.parse_args()

    video = pathlib.Path(args.video)
    if not video.is_file():
        print(f"no such video: {video}", file=sys.stderr)
        return 1

    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        frames = keyframes(video, tmp, args.seconds)
        if not frames:
            print("no keyframes found", file=sys.stderr)
            return 1
        best, score = sharpest(frames)
        print(f"  {len(frames)} keyframes, sharpest scores {score:.0f}")
        out = pathlib.Path(args.out)
        draw(pathlib.Path(best), out)
        size = out.stat().st_size
        print(f"  wrote {out} ({size / 1000:.0f} KB)")
        print("  now bump the version in index.html: the url carries the "
              "file's fingerprint and a cache only refetches a new url")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
