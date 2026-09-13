"""The music fonts a score carries inside itself.

THE BUG THIS EXISTS FOR. A tempo marking reads "Allegro. (&#x1D160; = 108)" in the
score, and came out of the renderer as "Allegro. (â–¡ = 108)" -- a tofu box
where the note should be.

Verovio draws almost everything as `<path>`, which needs no font. But a few
things it emits as LIVE TEXT in a music font:

    <tspan font-family="Leipzig" font-size="503px">&#xECA7;</tspan>

U+ECA7 is SMuFL `metNote8thUp`. The glyph is in the Leipzig font, and
Verovio helpfully embeds that font in the SVG as an `@font-face` data URI --
which is why the file looks right in any browser. cairosvg does not honour
`@font-face`: it resolves system fonts through fontconfig and nothing else.
With no Leipzig installed, the character falls back to a font that has
nothing at that codepoint, and you get the box.

Measured on the render host: the real glyph is 386 dark pixels, the tofu
box is 608.

SO THE FONT IS TAKEN OUT OF THE SCORE AND INSTALLED. Not a table of
substitutions -- music_line_extractor has one of those for its own GUI, and
it is right for a desktop app that must not touch a stranger's operating
system, but it covers eight metronome glyphs and this font carries 642. A
dynamic, an ornament, a pedal mark or a fermata emitted as text would each
need another row. Installing the font the score was drawn with covers every
one of them at once, and covers the next score's font too.

WHERE THE BYTES COME FROM MATTERS. They are lifted from the package the
operator installed, never shipped by us, so nothing here redistributes a
font. Whatever a score embeds is what gets installed for it.

AND IT MUST BE IN PLACE BEFORE THE PROCESS STARTS. fontconfig is consulted
once and cached: installing a font halfway through a render changes nothing
for that process -- measured, and the reason this runs at install time on
the web box and at boot on a compute node, rather than when a job arrives.
"""
from __future__ import annotations

import base64
import io
import logging
import pathlib
import re

logger = logging.getLogger(__name__)

# `@font-face { font-family: 'X'; src: url(data:application/font-woff2;...
# ...base64,AAA) format('woff2'); }` as Verovio writes it. Tolerant about
# the order and the exact media type, because this is somebody else's
# output and it has changed shape before.
_FACE = re.compile(
    r"@font-face\s*\{[^}]*?font-family:\s*['\"]?(?P<name>[^'\";}]+)['\"]?"
    r"[^}]*?url\(\s*data:(?P<mime>[^;,]+)[^,]*base64,\s*(?P<data>[A-Za-z0-9+/=\s]+?)\s*\)",
    re.S | re.I)

# A face name we will not write to disk under any circumstances. The name
# reaches a filesystem path, and it comes from a file we did not write.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")


def available() -> bool:
    """Whether the conversion can be done at all."""
    return _fonttools() is not None


def why_unavailable() -> str:
    if _fonttools() is not None:
        return ""
    return ("fontTools is not installed, so a font embedded in a score "
            "cannot be unpacked (pip install 'fonttools[woff]')")


def _fonttools():
    """Import fontTools once. None when it or brotli is missing.

    woff2 is brotli-compressed and fontTools needs the `brotli` package to
    open one -- installed but brotli-less is a real state, and it fails at
    load rather than at import, so both are checked here.
    """
    global _TT
    try:
        return _TT
    except NameError:
        pass
    try:
        from fontTools.ttLib import TTFont  # noqa: PLC0415
        import brotli  # noqa: F401,PLC0415
        _TT = TTFont
    except Exception:                                  # noqa: BLE001
        _TT = None
    return _TT


def faces(svg_text: str) -> dict[str, bytes]:
    """Every font embedded in this SVG, as raw bytes, by family name."""
    out: dict[str, bytes] = {}
    for m in _FACE.finditer(svg_text or ""):
        name = (m.group("name") or "").strip()
        if not _SAFE_NAME.match(name):
            logger.warning("ignoring an embedded font with an unusable "
                           "name: %r", name[:40])
            continue
        try:
            out.setdefault(name, base64.b64decode(
                re.sub(r"\s+", "", m.group("data"))))
        except Exception:                              # noqa: BLE001
            logger.warning("an embedded font in this score could not be "
                           "decoded; it will not be installed", exc_info=True)
    return out


def install_from(svg_path: pathlib.Path, into: pathlib.Path) -> list[str]:
    """Unpack the fonts one SVG embeds into `into`. Returns what it wrote.

    Idempotent by family name: the same font arrives in every band of every
    Verovio package, so this is called once per package and skips what is
    already there.
    """
    TTFont = _fonttools()
    if TTFont is None:
        logger.warning("%s", why_unavailable())
        return []

    try:
        text = svg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    written: list[str] = []
    for name, raw in faces(text).items():
        target = into / f"{name}.ttf"
        if target.is_file():
            continue
        try:
            font = TTFont(io.BytesIO(raw))
            # A woff/woff2 carries its compression in `flavor`; clearing it
            # writes a plain TrueType, which is what fontconfig wants.
            font.flavor = None
            into.mkdir(parents=True, exist_ok=True)
            # Written aside and moved, so a reader never meets a half-file.
            part = target.with_suffix(".part")
            font.save(str(part))
            part.replace(target)
            written.append(name)
            logger.info("unpacked the %s font from %s", name, svg_path.name)
        except Exception:                              # noqa: BLE001
            logger.warning("could not unpack the %s font from %s", name,
                           svg_path.name, exc_info=True)
    return written


def install_from_package(root: pathlib.Path, into: pathlib.Path) -> list[str]:
    """Unpack the fonts a score package carries. Returns what it wrote.

    Reads ONE band. Every band of a Verovio package embeds the same face,
    and a package has dozens of bands at half a megabyte each.
    """
    bands = sorted((root / "score" / "lines").glob("*.svg"))
    return install_from(bands[0], into) if bands else []

# Things a band should not still contain by the time it reaches us. Each was
# solved upstream in music_line_extractor -- but in the copy of `_fix_svg`
# that renders PAGES, not the one that exports BANDS, and the two have
# already drifted apart once. Detected rather than silently repaired: a band
# carrying one of these means the package was made by a pipeline that no
# longer matches, and the operator should know at install time instead of
# finding out in a rendered video.
# Extend this table, not the algorithm. `docs/score-package-contract.md`
# records which of music_line_extractor's fixes arrive baked into the
# bands and which do not.
KNOWN_ARTEFACTS = (
    # (needle, fatal, what it means)
    ("xlink:href", True,
     "uses the deprecated xlink:href for glyph references; cairosvg resolves "
     "href, so every notehead would be missing"),
    ('class="dir problem"', False,
     "carries Verovio's diagnostic 'problem' markers, which are meant for "
     "the engraver and not for a viewer"),
    ("magenta", False,
     "colours editor-marked notes magenta, which shouts over the music; "
     "app/svg.py recolours these grey at render time"),
)


def artefacts(svg_text: str) -> list[tuple[bool, str]]:
    """Upstream problems still present in this SVG, fatal ones flagged."""
    return [(fatal, why) for needle, fatal, why in KNOWN_ARTEFACTS
            if needle in svg_text]


def undrawable(svg_text: str) -> list[str]:
    """Live text this host has no way to draw.

    THE GENERAL FORM of the tofu bug, rather than a list of the codepoints
    that happened to break once. Verovio draws most things as `<path>`, but
    whatever it writes as text needs a real font -- and the only font we can
    guarantee is one the SVG carries with it, because that is the one we
    unpack and install. A private-use character in a font the file does not
    embed cannot be drawn by anything on this machine, and will be a box.
    """
    import re as _re

    embedded = set(faces(svg_text))
    trouble: list[str] = []
    # Each <tspan> that names a font and holds private-use characters.
    for m in _re.finditer(
            r"<tspan[^>]*font-family=\"([^\"]+)\"[^>]*>([^<]*)</tspan>",
            svg_text):
        family = m.group(1).split(",")[0].strip().strip("'\"")
        text = m.group(2)
        pua = sorted({ord(c) for c in text if 0xE000 <= ord(c) <= 0xF8FF})
        if not pua or family in embedded:
            continue
        trouble.append(
            "%s in font %r, which this file does not embed -- it would "
            "render as empty boxes"
            % (", ".join("U+%04X" % c for c in pua), family))
    return sorted(set(trouble))
