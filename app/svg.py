"""Rasterising vector score bands.

Isolated here because getting cairosvg to load on Windows is fiddly and
the fix belongs in one place rather than at every call site.

conda-forge ships the native library as `cairo.dll`, while cairocffi looks
only for `libcairo-2.dll` (and the Linux/macOS equivalents), so an
otherwise correct install fails to import. On top of that, a conda
environment's `Library\\bin` is only on PATH when the environment has been
activated — running its python.exe directly is not enough. Both are fixed
below before the import is attempted.

If it still cannot load, `available()` returns False and the caller falls
back to the PNG bands that packages ship alongside their SVGs.
"""

from __future__ import annotations

import functools
import io
import os
import pathlib
import shutil
import sys

from PIL import Image

# conda-forge's name -> the name cairocffi searches for.
_ALIASES = {"cairo.dll": "libcairo-2.dll"}


def _library_dirs() -> list[pathlib.Path]:
    """Places a conda environment keeps its native DLLs."""
    root = pathlib.Path(sys.executable).parent
    return [root / "Library" / "bin", root / "Library" / "lib", root / "DLLs"]


def _prepare_windows() -> None:
    """Put the native cairo library where cairocffi will find it."""
    if os.name != "nt":
        return
    for directory in _library_dirs():
        if not directory.is_dir():
            continue
        # Provide the alias cairocffi expects, if only the short name exists.
        for actual, expected in _ALIASES.items():
            src, dst = directory / actual, directory / expected
            if src.is_file() and not dst.is_file():
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass        # read-only env; the PATH entry may still work
        # find_library() searches PATH; LoadLibrary uses the DLL directories.
        os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
        try:
            os.add_dll_directory(str(directory))
        except (OSError, AttributeError):
            pass


@functools.lru_cache(maxsize=1)
def _cairosvg():
    """Import cairosvg once, after fixing the library path. None if absent."""
    _prepare_windows()
    try:
        import cairosvg
    except Exception:      # noqa: BLE001 - ImportError or OSError from cffi
        return None
    return cairosvg


def available() -> bool:
    """Whether .svg bands can actually be turned into pixels here."""
    return _cairosvg() is not None


def why_unavailable() -> str:
    """A diagnostic for the doctor, when rasterising isn't possible."""
    _prepare_windows()
    try:
        import cairosvg  # noqa: F401
        return ""
    except ImportError:
        return "cairosvg is not installed (pip install cairosvg)"
    except Exception as exc:  # noqa: BLE001
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc
        return f"cairosvg is installed but its native library won't load: {first}"


def rasterize(path: pathlib.Path, width: int, height: int,
              background: str = "white") -> Image.Image:
    """Render an SVG at exactly width x height.

    Rendered onto an opaque background so the recolouring step downstream
    sees the same ink-on-paper signal a PNG gives it; transparency is
    decided there, not inherited from the SVG.
    """
    cairosvg = _cairosvg()
    if cairosvg is None:
        raise RuntimeError(why_unavailable())
    png = cairosvg.svg2png(bytestring=adapt(path.read_bytes()),
                           output_width=width,
                           output_height=height, background_color=background)
    return Image.open(io.BytesIO(png)).convert("RGB")


# What music_line_extractor's own `_fix_svg` does before handing a Verovio
# SVG to cairosvg. Kept here because THE TWO PIPELINES DIVERGED: MLE has two
# copies of that function, and the one that exports the bands we consume is
# the smaller one. Its page renderer also recolours editor marks and
# substitutes music glyphs; its band exporter does neither, so a band
# arrives here carrying things that were already solved upstream for a
# different output.
#
# Three of MLE's fixes are baked into the bands before we see them --
# `xlink:href` rewritten to `href`, `<g class="dir problem">` removed, the
# red diagnostic colour stripped from turns -- and are asserted rather than
# repeated, so a package that regresses on one is caught by
# tools/check_score.py rather than rendered wrong.
_MAGENTA = (
    # Verovio's default RDF declaration marks editor-added notes magenta and
    # labels them "extra)". On the Breitkopf editions of the NIFC corpus that
    # is a shout in the middle of the music. MLE recolours it grey for its
    # own pages; a band exported for video never passed through that, so a
    # marked note would arrive at full magenta in somebody's finished film.
    (b'fill="magenta"', b'fill="grey"'),
    (b'color="magenta"', b'color="grey"'),
)


def adapt(svg: bytes) -> bytes:
    """A Verovio SVG, made ready for cairosvg. Cheap and idempotent."""
    for old_bytes, new_bytes in _MAGENTA:
        if old_bytes in svg:
            svg = svg.replace(old_bytes, new_bytes)
    return svg
