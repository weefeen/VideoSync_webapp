"""The title panel: performer and work metadata drawn beside the video.

Optional. With no panel the video and band use the full canvas; with one,
a column is reserved down the left and this module fills it.

The field list, sizes and vertical rhythm follow the layout VideoScoreSync
established, but the geometry is relative to the panel rectangle rather
than hardcoded to a 1920x1080 frame, so the same panel works at 1:1 and
9:16. Text is written to temp files rather than inlined, because ffmpeg's
drawtext escaping cannot survive apostrophes and accents in composer names.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import tempfile
import textwrap

from . import fonts

# Field -> (weight, italic, size at 1080p, wrap width in chars, max lines).
# Sizes scale with canvas height so a 9:16 panel stays legible.
FIELDS = (
    ("round_name", "bold", False, 32, None, 1),
    ("subtitle", "bold", False, 30, None, 1),
    ("name", "bold", False, 36, None, 1),
    ("country_age", "medium", True, 35, None, 1),
    ("composer", "book", False, 33, 18, 2),
    ("composition", "medium", True, 34, 24, 4),
)

# Baseline offsets from the top of the text block, at 1080p.
OFFSETS = {
    "round_name": 0, "subtitle": 35, "name": 126,
    "country_age": 180, "composer": 261, "composition": 308,
}

REFERENCE_HEIGHT = 1080        # canvas height the sizes above were designed at
REFERENCE_PANEL_WIDTH = 578    # and the panel column width they assumed


@dataclasses.dataclass(frozen=True)
class PanelStyle:
    color: str = "white"
    accent: str = "#cc237e"
    accent_size: tuple[int, int] = (133, 4)
    # Baseline-to-baseline distance as a multiple of the font size. Applied
    # by us rather than by drawtext, whose multi-line spacing follows the
    # font's own metrics and can only be widened.
    line_height: float = 1.16
    text_top: float = 0.302      # 326/1080 — where the text block starts
    # The rule separating the round block from the performer, as an offset
    # from the top of the text block. Anchoring it to the canvas instead
    # lets it drift away from the text whenever the two scale differently.
    accent_offset: int = 92      # 418 - 326, in reference pixels
    left_pad: float = 0.049      # 94/1920 of canvas width


def wrap_composer(name: str, max_chars: int = 18, max_lines: int = 2) -> str:
    """Break a composer name, preferring a dash as the split point."""
    if len(name) <= max_chars:
        return name
    dashes = [i for i, ch in enumerate(name) if ch in ("-", "\u2013", "\u2014")]
    if dashes:
        mid = dashes[len(dashes) // 2]
        first, second = name[:mid + 1], name[mid + 1:]
        if len(second) > max_chars:
            second = " ".join(textwrap.wrap(second, width=max_chars,
                                            break_long_words=True,
                                            break_on_hyphens=False))
        return f"{first}\n{second}"
    lines = textwrap.wrap(name, width=max_chars, break_long_words=True,
                          break_on_hyphens=False)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return "\n".join(lines)


def wrap_composition(text: str, max_chars: int = 24, max_lines: int = 4) -> str:
    """Wrap a work title without splitting opus or key designations."""
    protected = ("\u2060")   # word joiner: keeps a token on one line
    patterns = (
        r"op\.\s*\w+(?=$|\s|[.,;:])",
        r"BWV\s*\w+(?=$|\s|[.,;:])",
        r"no\.\s*\w+(?=$|\s|[.,;:])",
        r"K\.\s*\d+(?=$|\s|[.,;:])",
        r"[A-G](?:-flat|-sharp)?\s+(?:Major|major|Minor|minor)(?=$|\s|[.,;:])",
    )
    for pattern in patterns:
        text = re.sub(pattern, lambda m: m.group(0).replace(" ", protected), text)

    lines = textwrap.wrap(text, width=max_chars, break_long_words=True,
                          break_on_hyphens=False)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return "\n".join(line.replace(protected, " ") for line in lines)


def _textfile(content: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, mode="w",
                                      encoding="utf-8", suffix=".txt")
    tmp.write(content)
    tmp.close()
    return pathlib.Path(tmp.name).as_posix()


def values(meta: dict) -> dict[str, str]:
    """Assemble the displayed strings from raw job metadata."""
    name = " ".join(p for p in (meta.get("first_name", ""),
                                meta.get("last_name", "")) if p).strip()
    country_age = ", ".join(p for p in (meta.get("country", ""),
                                        str(meta.get("age", "") or "")) if p)
    return {
        "round_name": meta.get("round_name", ""),
        "subtitle": meta.get("subtitle", ""),
        "name": name,
        "country_age": country_age,
        "composer": meta.get("composer", ""),
        "composition": meta.get("composition", ""),
    }


def build_chain(input_label: str, out_label: str, panel_rect, canvas,
                meta: dict, style: PanelStyle | None = None) -> tuple[str, list[str]]:
    """Return (filter chain, temp files to clean up).

    Empty fields are skipped entirely rather than drawn blank, so a panel
    with only composer and work is laid out sensibly.
    """
    style = style or PanelStyle()
    text = values(meta)
    if not any(text.values()):
        return "", []

    canvas_w, canvas_h = canvas
    # Scale by whichever dimension constrains the panel more. Using height
    # alone enlarges the text on a tall canvas while the panel column stays
    # narrow, so the longest lines overflow it and run across the video.
    scale = min(canvas_h / REFERENCE_HEIGHT,
                panel_rect.w / (REFERENCE_PANEL_WIDTH or panel_rect.w))
    x = panel_rect.x + round(style.left_pad * canvas_w)
    top = round(style.text_top * canvas_h)

    filters: list[str] = []
    label = input_label
    temps: list[str] = []

    # The accent rule sits above the block; drawn first so text overlays it.
    aw, ah = (max(1, round(v * scale)) for v in style.accent_size)
    accent_y = top + round(style.accent_offset * scale)
    filters.append(
        f"[{label}]drawbox=x={x + round(4 * scale)}:y={accent_y}:"
        f"w={aw}:h={ah}:color={_ffcolor(style.accent)}:t=fill[pnl0]")
    label = "pnl0"

    # Long names shift left slightly, matching the original layout's
    # compensation so they don't collide with the video edge.
    shift = round(25 * scale) if len(text["name"]) >= 18 else 0

    drawn = [(f, w, i, s, wrap, lines) for f, w, i, s, wrap, lines in FIELDS
             if text.get(f)]
    if not drawn:
        return "", []

    # Each line is drawn separately at an explicit baseline. Handing ffmpeg a
    # multi-line textfile instead makes it use the font's own line height,
    # which for Futura is far looser than the rest of the panel's rhythm and
    # cannot be tightened (line_spacing only ever adds). One draw per line
    # also keeps every string short, which suits per-line temp files.
    extra = 0
    specs: list[tuple] = []
    for field, weight, italic, size, wrap, max_lines in drawn:
        content = text[field]
        lines = [content]
        if wrap:
            wrapped = (wrap_composer if field == "composer" else wrap_composition)(
                content, max_chars=wrap, max_lines=max_lines)
            lines = wrapped.split("\n")

        line_h = round(size * style.line_height * scale)
        base = top + round(OFFSETS[field] * scale) + extra
        for n, line in enumerate(lines):
            if not line.strip():
                continue
            # A per-line file sidesteps drawtext escaping entirely, which
            # matters for apostrophes and accents in work titles.
            path = _textfile(line)
            temps.append(path)
            specs.append((path, True, weight, italic, size, base + n * line_h))
        extra += line_h * (len(lines) - 1)

    for idx, (content, is_file, weight, italic, size, y) in enumerate(specs):
        font_path, px = fonts.futura(weight=weight, italic=italic,
                                     size_px=max(8, round(size * scale)))
        target = out_label if idx == len(specs) - 1 else f"pnl{idx + 1}"
        spec = (f"textfile='{_ffpath(content)}':" if is_file
                else f"text='{_escape(content)}':")
        filters.append(
            f"[{label}]drawtext=fontfile='{_ffpath(font_path)}':fontsize={px}:{spec}"
            f"x={x - shift}:y={y}:fontcolor={_ffcolor(style.color)}[{target}]")
        label = target

    return ";".join(filters), temps


def _escape(text: str) -> str:
    """Escape a literal for ffmpeg drawtext."""
    return (text.replace("\\", "\\\\").replace("'", "\\'")
                .replace(":", "\\:").replace("\n", "\\n"))


def _ffpath(path: str) -> str:
    """Make a filesystem path safe inside an ffmpeg filter description.

    ffmpeg splits filter options on ':' before it resolves quoting, so a
    Windows drive letter ("C:/fonts/...") terminates the option early and
    the rest is read as a bogus option name. Escaping the colon is the only
    thing that works; quoting alone does not.
    """
    return path.replace("\\", "/").replace(":", "\\:")


def _ffcolor(value: str) -> str:
    """'#rrggbb' -> ffmpeg's 0xRRGGBB; named colours pass through."""
    value = value.strip()
    return f"0x{value[1:]}" if value.startswith("#") else value
