"""Where each bar sits across an engraved band.

The package says which measure a band *starts* at and nothing about where the
bars fall inside it, so a player can highlight a system but not a bar. That
position is recoverable: Verovio keeps its `<g class="measure">` groups and
their barlines in the exported band, and a barline's x is the right edge of
the measure it closes.

Reading those numbers straight out of the path data is wrong, and quietly so.
Verovio nests coordinate systems -- a `page-margin` group translated inside a
root viewBox the band exporter then re-crops -- so `d="M6338 ..."` is not in
the same space as the viewBox and every bar comes out shifted by the margin.
The transform chain is walked here instead, which is why this module exists
rather than a regex at the call site.

A band SVG holds the whole page; only the systems inside the cropped viewBox
are on this band, and the rest are discarded by their y.

Positions come back as fractions of the band's width, so they survive
rasterising to any size and can be laid over the image as percentages.
"""
from __future__ import annotations

import dataclasses
import logging
import pathlib
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

SVG_NS = "http://www.w3.org/2000/svg"

# translate(a) | translate(a, b) | scale(s) | scale(sx, sy) | matrix(...)
_TRANSFORM = re.compile(r"(translate|scale|matrix)\s*\(([^)]*)\)")
_NUMBERS = re.compile(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?")
# "M6338 1075 L6338 1795" -- only the endpoints of straight barline strokes
_MOVE_LINE = re.compile(
    r"M\s*([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)\s*L\s*([-+0-9.eE]+)[\s,]+([-+0-9.eE]+)")


class BandGeometryError(ValueError):
    """The band is not shaped the way bar positions are read from."""


@dataclasses.dataclass(frozen=True)
class Bar:
    """One bar of music, as a span across the band.

    `x0` and `x1` are fractions of the band's width, left to right. The first
    bar of a system starts at 0, which includes that system's clef and key
    signature -- they belong to the system rather than to the bar, but they
    sit before the first barline and there is nowhere else to put them.
    """
    measure: int
    x0: float
    x1: float


@dataclasses.dataclass(frozen=True)
class _Frame:
    """A point in the transform chain: scale then translate, in x only."""
    sx: float = 1.0
    tx: float = 0.0
    sy: float = 1.0
    ty: float = 0.0

    def then(self, transform: str | None) -> "_Frame":
        """This frame with `transform` applied inside it."""
        if not transform:
            return self
        sx, tx, sy, ty = self.sx, self.tx, self.sy, self.ty
        for kind, raw in _TRANSFORM.findall(transform):
            n = [float(v) for v in _NUMBERS.findall(raw)]
            if kind == "translate" and n:
                tx += sx * n[0]
                ty += sy * (n[1] if len(n) > 1 else 0.0)
            elif kind == "scale" and n:
                sx *= n[0]
                sy *= n[1] if len(n) > 1 else n[0]
            elif kind == "matrix" and len(n) >= 6:
                # a c e / b d f: only the axis-aligned part is meaningful for
                # a barline, and a rotated score is not something we produce.
                if n[1] or n[2]:
                    raise BandGeometryError(
                        "a rotated or skewed transform is on the barlines")
                tx += sx * n[4]
                ty += sy * n[5]
                sx *= n[0]
                sy *= n[3]
        return _Frame(sx, tx, sy, ty)

    def x(self, value: float) -> float:
        return self.tx + self.sx * value

    def y(self, value: float) -> float:
        return self.ty + self.sy * value

    def viewport(self, element: ET.Element) -> "_Frame":
        """This frame as seen inside a nested <svg> that re-maps the space.

        Verovio wraps its engraving in `<svg class="definition-scale">`, whose
        viewBox happens to match its width and height -- so the mapping is the
        identity and it would be tempting to skip. It is computed anyway: a
        band exported at a different scale would move every bar, and an
        identity that is calculated cannot quietly stop being one.
        """
        box = (element.get("viewBox") or "").split()
        if len(box) != 4:
            return self.then(element.get("transform"))
        vbx, vby, vbw, vbh = (float(v) for v in box)
        if vbw <= 0 or vbh <= 0:
            raise BandGeometryError("a nested <svg> has an empty viewBox")

        def length(name: str) -> float:
            raw = (element.get(name) or "").strip()
            if not raw or raw.endswith("%"):
                # Without the parent's pixel size a percentage cannot be
                # resolved here, and guessing moves every bar.
                raise BandGeometryError(
                    f"a nested <svg> sizes its {name} as {raw!r}")
            return float(re.sub(r"[a-z]+$", "", raw))

        vpw, vph = length("width"), length("height")
        vpx = float(element.get("x") or 0.0)
        vpy = float(element.get("y") or 0.0)

        fit = (element.get("preserveAspectRatio") or "xMidYMid meet").split()
        align = fit[0]
        if align == "none":
            sx, sy = vpw / vbw, vph / vbh
            dx = dy = 0.0
        else:
            if len(fit) > 1 and fit[1] == "slice":
                scale = max(vpw / vbw, vph / vbh)
            else:
                scale = min(vpw / vbw, vph / vbh)
            sx = sy = scale
            dx = {"xMin": 0.0, "xMid": 0.5, "xMax": 1.0}.get(
                align[:4], 0.5) * (vpw - vbw * scale)
            dy = {"YMin": 0.0, "YMid": 0.5, "YMax": 1.0}.get(
                align[4:8], 0.5) * (vph - vbh * scale)

        inner = _Frame(
            sx=self.sx * sx,
            tx=self.tx + self.sx * (vpx + dx - vbx * sx),
            sy=self.sy * sy,
            ty=self.ty + self.sy * (vpy + dy - vby * sy),
        )
        return inner.then(element.get("transform"))


def _tag(element: ET.Element) -> str:
    return element.tag.split("}", 1)[-1]


def _classes(element: ET.Element) -> set[str]:
    return set((element.get("class") or "").split())


def _barline_x(group: ET.Element, frame: _Frame) -> tuple[float, float, float] | None:
    """The x of a barline group, and the y range it covers, in root space."""
    xs: list[float] = []
    ys: list[float] = []
    for path in group.iter():
        if _tag(path) != "path":
            continue
        for x0, y0, x1, y1 in _MOVE_LINE.findall(path.get("d") or ""):
            xs += [frame.x(float(x0)), frame.x(float(x1))]
            ys += [frame.y(float(y0)), frame.y(float(y1))]
    if not xs:
        return None
    return sum(xs) / len(xs), min(ys), max(ys)


def _walk(element: ET.Element, frame: _Frame,
          found: list[tuple[float, float, float]]) -> None:
    """Collect every barline's (x, y_top, y_bottom) in root coordinates."""
    # A nested <svg> re-maps the coordinate space; `viewport` works out how.
    here = (frame.viewport(element) if _tag(element) == "svg"
            else frame.then(element.get("transform")))
    if "barLine" in _classes(element):
        got = _barline_x(element, here)
        if got:
            found.append(got)
        return                                  # nothing nested inside matters
    for child in element:
        _walk(child, here, found)


def bars_for(path: str | pathlib.Path, first_measure: int) -> list[Bar]:
    """The bars visible on one band, left to right, numbered from its first.

    Bars are numbered by counting: a band that starts at measure 9 and shows
    nine barlines holds measures 9 to 17. `check_score.py` cross-checks that
    against the next band's own first measure, which is the only independent
    statement of where a band ends.
    """
    path = pathlib.Path(path)
    root = ET.parse(path).getroot()
    box = (root.get("viewBox") or "").split()
    if len(box) != 4:
        raise BandGeometryError(f"{path.name} has no usable viewBox")
    vx, vy, vw, vh = (float(v) for v in box)
    if vw <= 0 or vh <= 0:
        raise BandGeometryError(f"{path.name} has an empty viewBox")

    found: list[tuple[float, float, float]] = []
    for child in root:                          # the root's own box is the frame
        _walk(child, _Frame(), found)

    # A barline whose stroke lies outside the crop belongs to another system.
    on_band = [b for b in found if b[2] >= vy and b[1] <= vy + vh]
    on_band.sort(key=lambda b: b[0])

    bars: list[Bar] = []
    left = vx
    for x, _, _ in on_band:
        bars.append(Bar(measure=first_measure + len(bars),
                        x0=(left - vx) / vw,
                        x1=(x - vx) / vw))
        left = x
    if not bars:
        logger.warning("%s: no barlines found on the band", path.name)
    return bars
