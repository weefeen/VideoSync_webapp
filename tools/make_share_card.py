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

# 1200x630 EXACTLY, because that is what Facebook renders. Handing it
# 1920 does not buy detail: it buys a downscale done by them, badly, after
# the file leaves us. Below 1200x630 the card degrades to a thumbnail
# beside the text, so this is a floor as well as a target.
#
# The card is DRAWN at this size rather than drawn large and shrunk. Type
# reduced after the fact is soft type; type set at the size it will be
# read at is not.
WIDTH, ASPECT = 1200, 1200 / 630

# The site's own palette, from app/static/svs/index.html.
PAPER = (246, 241, 232)
INK = (28, 22, 34)
MAGENTA = (204, 35, 126)

# How much of the engraved band to show. The renderer cuts roughly nine
# bars to a frame; a share card is read at a quarter that size, so it
# shows a third of them at three times the scale.
# How much of the card the engraving takes. The bars that fit follow from
# it -- about seven at this share -- rather than the other way round.
BAND_SHARE = 0.40

# The shape of a band as the engraver cuts it: wide and short, with its ink
# filling it. Measured on a rasterised band, 3493x605.
BAND_ASPECT = 5.77

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


def draw(frame: pathlib.Path, out: pathlib.Path,
         band_png: "pathlib.Path | None" = None) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    src = Image.open(frame).convert("RGB")
    height = round(WIDTH / ASPECT)
    # CROPPED IN THE SOURCE'S OWN SCALE, then resized. Cropping to the
    # card's pixel height first takes that many pixels off a 1920-wide
    # frame, which is a different share of the picture entirely -- it cut
    # away the performer and left almost nothing but the score.
    #
    # Off the TOP, because the engraved band runs along the bottom and is
    # the whole point.
    keep = round(src.width / ASPECT)
    card = src.crop((0, max(0, src.height - keep), src.width, src.height))
    if (card.width, card.height) != (WIDTH, height):
        card = card.resize((WIDTH, height), Image.LANCZOS)

    # THE MUSIC IS ENLARGED, because the card is READ AT 500 PIXELS. A feed
    # shows it at roughly a quarter of its width, and the band the renderer
    # cuts carries about nine bars across the frame -- which at that size
    # is 55 pixels a bar, where staff lines and noteheads dissolve into
    # grey. That is the whole reason this card looked soft beside cards
    # built from large flat shapes. It is SCALE, not compression, and no
    # format or quality setting reaches it.
    #
    # THE HEIGHT FOLLOWS FROM THE MUSIC, not the other way round. A band is
    # about 5.8:1 and its ink fills it, so there is no slack to reclaim:
    # how many bars you want at full width fixes how tall the band must be,
    # and the photograph takes what is left. Six bars makes an even split.
    # THE WORDS DECIDE, AND THE MUSIC TAKES THE REST. Choosing a number of
    # bars instead fixed the band's height, and at six bars that height ate
    # the headline -- "playing along" sheared off and "Free" gone
    # altogether. The text block is the thing that must not be cut, so the
    # band gets the remaining share and however many bars fit in it.
    band_h = round(height * BAND_SHARE)
    band_from = height - band_h
    bars = WIDTH * 9.0 / (band_h * BAND_ASPECT)
    band = card.crop((0, band_from, card.width, height))
    band = band.crop((0, 0, max(1, int(band.width * bars / 9.0)),
                      band.height))
    band = band.resize((WIDTH, band_h), Image.LANCZOS)

    # FROM VECTOR WHERE THERE IS ONE. The band inside a video frame is a
    # raster the encoder has already been through, so enlarging it
    # magnifies what was thrown away. The engraving exists as SVG in every
    # score package: rasterised well above the target and reduced, it lands
    # crisp, which is the difference between notation you can read at feed
    # size and a grey texture.
    if band_png is not None and band_png.is_file():
        v = Image.open(band_png)
        paper = Image.new("RGB", v.size, PAPER)
        paper.paste(v, (0, 0), v if v.mode == "RGBA" else None)
        cut = paper.crop((0, 0, max(1, int(v.width * bars / 9.0)),
                          v.height))
        band = cut.resize((WIDTH, band_h), Image.LANCZOS)

    card.paste(band, (0, band_from))

    # Restore the edge contrast compression flattened. Light on purpose:
    # the detail is not there to recover, and a heavier hand buys halos
    # that look worse than the softness they replace.
    card = card.filter(ImageFilter.UnsharpMask(radius=1.4, percent=70,
                                               threshold=3))

    # A SCRIM, NOT A BOX. The headline sits on the photograph, so it needs
    # the photograph darkened under it -- but a rectangle would read as a
    # sticker laid on top. This is a horizontal ramp: opaque at the left
    # edge where the words are, gone by the middle where the piano is.
    band_top = band_from
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
    small = _font(round(WIDTH * 0.017), "body")
    x = left
    for ch in KICKER:
        pen.text((x, int(height * 0.075)), ch, font=small,
                 fill=(233, 226, 240))
        x += pen.textlength(ch, font=small) + WIDTH * 0.002

    big = _font(round(WIDTH * 0.052), "display")
    y = int(height * 0.155)
    for line in HEADLINE.split("\n"):
        pen.text((left, y), line, font=big, fill=PAPER)
        y += round(WIDTH * 0.059)

    # The one piece of colour, and the word that matters most.
    rule_y = y + round(WIDTH * 0.0155)
    pen.rounded_rectangle([left, rule_y, left + WIDTH * 0.034,
                           rule_y + max(2, WIDTH * 0.003)], radius=3,
                          fill=MAGENTA)
    free = _font(round(WIDTH * 0.0202), "body")
    pen.text((left + WIDTH * 0.046, rule_y - WIDTH * 0.0077), NOTE,
             font=free, fill=PAPER)

    # PNG, NOT JPEG. This card is mostly crisp things -- set type, a
    # magenta rule, engraved notation -- and lossy compression smears
    # exactly those, which is why a JPEG card looks fuzzy next to one that
    # is flat colour and lettering. The photograph costs more bytes as PNG
    # and the total still lands near half a megabyte, well inside the 8 MB
    # Facebook allows and the 1 MB that is reliable everywhere.
    if out.suffix.lower() in (".jpg", ".jpeg"):
        card.save(out, quality=95, subsampling=0, optimize=True,
                  progressive=True)
    else:
        card.save(out, optimize=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", help="a rendered score-video to take the frame from")
    ap.add_argument("--out", default="app/static/svs/og-cover.png")
    ap.add_argument("--band", help="a rasterised band SVG; the engraving is "
                                   "taken from this rather than from the "
                                   "video frame, which is the difference "
                                   "between readable notation and a texture")
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
        draw(pathlib.Path(best), out,
             pathlib.Path(args.band) if args.band else None)
        size = out.stat().st_size
        print(f"  wrote {out} ({size / 1000:.0f} KB)")
        print("  now bump the version in index.html: the url carries the "
              "file's fingerprint and a cache only refetches a new url")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
