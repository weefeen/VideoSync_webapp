"""Resolving Futura LT Pro faces for the title panel.

Futura LT Pro's filenames are not uniformly constructed — the upright
oblique is "MediumOblique" while the condensed oblique is "MediumCondObl" —
so weight/italic/condensed is mapped to a filename rather than assembled
naively. Missing cuts fall back to the upright face: a missing italic
should soften the design, not abort a render.

Fonts are located via FONT_DIR in .env, defaulting to the assets folder
this repo ships. ffmpeg's drawtext parses the path itself, so paths are
returned with forward slashes on every platform.
"""

from __future__ import annotations

import os
import pathlib

_PREFIX = "Linotype - FuturaLTPro-"

WEIGHTS = {
    "light": "Light",
    "book": "Book",
    "medium": "Medium",
    "bold": "Bold",
    "xbold": "XBold",
    "heavy": "Heavy",
    "black": "Black",
}


def font_dir() -> pathlib.Path:
    configured = os.getenv("FONT_DIR", "").strip().strip('"')
    if configured:
        return pathlib.Path(configured)
    return pathlib.Path(__file__).resolve().parent.parent / "assets" / "font" / "Futura"


def _filename(weight: str, italic: bool, condensed: bool) -> str:
    stem = WEIGHTS[weight]
    if condensed:
        return f"{_PREFIX}{stem}Cond{'Obl' if italic else ''}.otf"
    return f"{_PREFIX}{stem}{'Oblique' if italic else ''}.otf"


class FontError(RuntimeError):
    """No usable font file. Message names where we looked."""


def futura(weight: str = "book", italic: bool = False,
           condensed: bool = False, size_px: int = 32) -> tuple[str, int]:
    """Return (font path for ffmpeg, pixel size) for a Futura LT Pro cut."""
    key = weight.lower().strip()
    if key not in WEIGHTS:
        raise FontError(f"Unknown Futura weight {weight!r}. "
                        f"Known: {', '.join(sorted(WEIGHTS))}")

    directory = font_dir()
    candidates = [
        _filename(key, italic, condensed),
        _filename(key, italic, False),    # drop condensed
        _filename(key, False, False),     # drop oblique too
        f"{_PREFIX}Book.otf",             # last resort
    ]
    for name in candidates:
        path = directory / name
        if path.is_file():
            return path.as_posix(), int(size_px)

    raise FontError(
        f"No Futura LT Pro font in {directory} for weight={weight!r} "
        f"italic={italic} condensed={condensed}. Set FONT_DIR in .env to "
        f"the folder holding the .otf files.")


def available() -> bool:
    """Whether any Futura face can be found — panels need one."""
    try:
        futura()
        return True
    except FontError:
        return False
