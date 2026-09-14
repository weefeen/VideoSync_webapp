"""Engrave one band per work, so a piece nobody has packaged still looks real.

WHY. A visitor whose piece is not in the library now has their request
taken rather than refused — the score is made from requests like theirs.
But the design screen previews the score, and it previews a REAL band:
`realBand()` uses `band`, `band_w` and `band_h` from the library, and
`staveHTML` falls back to drawn generic staves when they are missing. So
the person whose piece we have not engraved saw a different, plainly worse
screen than everybody else — the same refusal, expressed as squiggles.

A preview band is not a package. The package is bands for the whole work
plus the reference chroma and its alignment, which is what makes a video;
this is ONE system of the opening, which is what makes a picture. Verovio
renders it straight from the Humdrum source in about a second, so every
work in the corpus can have one long before anybody asks for it.

    python tools/make_preview_bands.py --list
    python tools/make_preview_bands.py --one 023-1-BH
    python tools/make_preview_bands.py --all           # publishes to the bucket

WHAT IT WRITES, per work:

    scores/<edition name>/band.svg     the opening system
    scores/previews.json               name -> width, height, title

`scores/<name>/band.svg` is exactly where a published package puts its own
preview, and `/api/library/<name>/band` already serves from there — so
nothing on the web box changes to consume these. A work that later gets a
real package overwrites its own preview and the two stay consistent.

THE NAMES ARE THE RECOGNISER'S. An edition is `Op.23_BALLADE_(Breitkopf)__023-1-BH`
and the corpus file is `023-1-BH.krn`: the code after the double underscore.
That mapping is the whole reason this can be done up front — the names come
from the pair list the recogniser answers with, so a band published here is
findable by the exact string a request will carry.

VEROVIO IS NOT IN THE APP'S ENVIRONMENT. It lives where the engraving
happens; this is an operator's tool and is run with that interpreter:

    C:\\Users\\msmabq\\.conda\\envs\\2026liszt\\python.exe tools/make_preview_bands.py --all
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import storage                                   # noqa: E402
from app.settings import settings                         # noqa: E402

# Where the Humdrum sources are. The corpus is a build input, in no
# repository, exactly like the packages.
CORPUS = pathlib.Path(
    r"C:\ZZ_perso\weefeen\PT\RD\music_line_extractor"
    r"\experiments\verovio_krn_spike\inputs")

PREVIEWS = "scores/previews.json"

# What a real band is, and the shape this aims at. `ScorePackage.band_size`
# defaults to these and every installed package so far reports exactly them,
# so a preview shaped like this predicts the video somebody will get.
BAND_W, BAND_H = 1306, 244

# ONE SYSTEM, AND ROUGHLY THE RIGHT SHAPE. Measured on the Ballade: the page
# height decides how many systems stack -- 900 gives one, 1100 gives two --
# and the width decides how much music goes on that system, which is what
# sets the aspect:
#
#     pageWidth 2400 -> 4.34      3200 -> 5.54
#               2800 -> 4.83      4000 -> 6.96      target 1306/244 = 5.35
#
# The exact ratio still varies with the music, so what is PUBLISHED is the
# viewBox Verovio actually produced rather than these numbers. A preview
# laid out at the wrong proportions is a preview that predicts a video
# nobody gets.
PAGE_W, PAGE_H = 3200, 900


def editions() -> dict[str, str]:
    """Every edition the recogniser can offer -> its corpus code."""
    raw = json.loads(pathlib.Path(settings.pair_list).read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for name in (raw.get("pairs") or {}):
        code = name.rsplit("__", 1)[-1].strip()
        if code:
            out[name] = code
    return out


def _verovio():
    try:
        import verovio                                    # noqa: PLC0415
    except ImportError as exc:
        raise SystemExit(
            "verovio is not importable under this interpreter. This is an "
            "operator's tool; run it with the environment that has it:\n"
            "  C:\\Users\\msmabq\\.conda\\envs\\2026liszt\\python.exe "
            "tools/make_preview_bands.py --all") from exc
    return verovio


def engrave(krn: pathlib.Path) -> str | None:
    """The opening system of a Humdrum score, as an SVG band. None if it
    cannot be read.

    `breaks="auto"` with a page wide enough for one system and short enough
    to hold nothing else: Verovio then lays the music out and the first page
    IS the first band. Asking for a page of music and cropping it would put
    a half-height second system under the first.
    """
    verovio = _verovio()
    for height in (PAGE_H, 760, 640):
        tk = verovio.toolkit()
        tk.setOptions({
            "pageWidth": PAGE_W,
            "pageHeight": height,
            "scale": 40,
            "adjustPageHeight": True,
            "breaks": "auto",
            "header": "none",
            "footer": "none",
            "svgViewBox": True,
            # The font the bands are drawn with everywhere else. It is
            # embedded in the output, which is what app/smufl.py reads and
            # what app/svg.py substitutes out of live text.
            "font": "Leipzig",
        })
        if not tk.loadFile(str(krn)):
            return None
        svg_text = tk.renderToSVG(1)
        # A BAND IS ONE SYSTEM. A page that fits two returns both, and they
        # arrive squashed into a band-shaped box -- which is how the first
        # attempt looked, and it looked wrong rather than small.
        if svg_text.count('class="system"') == 1:
            return as_a_package_would(svg_text)
    return None


def as_a_package_would(svg_text: str) -> str:
    """The three fixes a real band arrives already carrying.

    `docs/score-package-contract.md` records that music_line_extractor
    bakes these into the bands it exports, and that app/svg.py therefore
    asserts them rather than repeating them. A band engraved here comes
    straight out of Verovio and has none of them -- so without this, a
    preview would carry Verovio's engraver diagnostics (three small
    coloured marks on the Etude, measured) that no published package has,
    and `tools/check_score.py` would rightly call it broken.

    Same three, in the same order, as `_fix_svg` upstream:

      * `xlink:href` is the deprecated form; cairosvg resolves `href`
      * `<g class="dir problem">` is a note to the engraver, not the reader
      * red is Verovio's diagnostic colour on a turn, not the music's
    """
    svg_text = re.sub(r"\bxlink:href=", "href=", svg_text)
    svg_text = re.sub(r'<g\b[^>]*\bclass="dir problem"[^>]*>.*?</g>',
                      "", svg_text, flags=re.DOTALL)
    svg_text = re.sub(
        r'(<g\b[^>]*\bclass="turn"[^>]*)\bcolor="red"\s+fill="red"',
        r"\1", svg_text, flags=re.IGNORECASE)
    return svg_text


def measured(svg_text: str) -> tuple[int, int]:
    """The size Verovio actually drew, from its own viewBox.

    Published rather than assumed: the aspect moves with how much music
    fits on a system, and a preview laid out at the wrong proportions
    predicts a video nobody gets.
    """
    m = re.search(r'viewBox="0 0 (\d+)(?:\.\d+)? (\d+)(?:\.\d+)?"', svg_text)
    return (int(m.group(1)), int(m.group(2))) if m else (BAND_W, BAND_H)


def publish(name: str, svg_text: str, dry: bool = False) -> int:
    """Put one band where a package's own preview would go."""
    key = f"scores/{name}/band.svg"
    if dry:
        return len(svg_text.encode("utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "band.svg"
        out.write_text(svg_text, encoding="utf-8")
        return storage.put(out, key)


def read_previews() -> dict:
    if storage.head(PREVIEWS) is None:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "previews.json"
        storage.get(PREVIEWS, out)
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


def write_previews(index: dict) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "previews.json"
        out.write_text(json.dumps(index, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        storage.put(out, PREVIEWS)


def title_of(krn: pathlib.Path) -> str:
    """The work's own title, from its Humdrum header."""
    otl = ops = ""
    try:
        with krn.open("r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n > 400:
                    break
                if line.startswith("!!!OTL:"):
                    otl = line.split(":", 1)[1].strip()
                elif line.startswith("!!!OPS:"):
                    ops = line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ", ".join(p for p in (otl, ops) if p)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true",
                    help="what has a source, and what already has a band")
    ap.add_argument("--one", help="a single corpus code, e.g. 023-1-BH")
    ap.add_argument("--all", action="store_true", help="every work with a source")
    ap.add_argument("--out", help="write beside the tool instead of publishing")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not CORPUS.is_dir():
        print(f"no corpus at {CORPUS}")
        return 1

    known = editions()
    have = {p.stem: p for p in CORPUS.glob("*.krn")}
    pairs = [(name, have[code]) for name, code in known.items() if code in have]
    missing = sorted(c for c in known.values() if c not in have)

    print(f"  {len(known)} editions the recogniser can offer")
    print(f"  {len(have)} humdrum sources in the corpus")
    print(f"  {len(pairs)} matched by code")
    if missing:
        print(f"  {len(missing)} with no source: {', '.join(missing[:6])}"
              f"{' ...' if len(missing) > 6 else ''}")

    if args.list:
        for name, krn in sorted(pairs)[:args.limit or 12]:
            print(f"    {krn.stem:<14} {name[:62]}")
        return 0

    if args.one:
        pairs = [(n, k) for n, k in pairs if k.stem == args.one]
        if not pairs:
            print(f"  no edition uses the source {args.one!r}")
            return 1

    if not (args.one or args.all):
        ap.print_help()
        return 0

    out_dir = pathlib.Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    elif not storage.available():
        print(f"  no bucket: {storage.status().get('problem')}")
        return 1

    index = {} if out_dir else read_previews()
    done = failed = 0
    for name, krn in sorted(pairs)[:args.limit or None]:
        svg_text = None
        try:
            svg_text = engrave(krn)
        except Exception as exc:                          # noqa: BLE001
            print(f"    FAILED {krn.stem}: {exc}")
        if not svg_text:
            failed += 1
            continue
        if out_dir:
            (out_dir / f"{krn.stem}.svg").write_text(svg_text, encoding="utf-8")
        else:
            publish(name, svg_text)
        w, h = measured(svg_text)
        index[name] = {"w": w, "h": h, "title": title_of(krn),
                       "source": krn.stem}
        done += 1
        print(f"    {krn.stem:<14} {len(svg_text) // 1024:>4} KB  {name[:52]}")

    if not out_dir and done:
        write_previews(index)
        print(f"\n  published {done} band(s); previews.json now lists "
              f"{len(index)}")
    else:
        print(f"\n  wrote {done} band(s)"
              + (f" to {out_dir}" if out_dir else ""))
    if failed:
        print(f"  {failed} could not be engraved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
