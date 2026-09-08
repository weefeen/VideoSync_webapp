"""Reading a score package produced by music_line_extractor.

This app does no score preparation and no synchronisation. Both happen
upstream in music_line_extractor, whose ``export_package`` workflow writes
a folder we treat as a read-only input contract:

    lines/<first_measure>.{svg,png,jpg}   one image per score band
    measures.data                         "<measure>\\t<seconds>" per line
    export.json                           manifest: geometry, style, entries
    chroma.npy                            present, unused here (no syncing)

The two repositories share no code. If the contract below stops matching
what the exporter writes, this is the only file that needs to change.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Iterator

# Band images are named by the first measure they cover; a band stays on
# screen until the next one's first measure is reached.
IMAGE_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg")


class PackageError(ValueError):
    """The folder isn't a usable score package. Message is user-facing."""


@dataclasses.dataclass(frozen=True)
class Band:
    """One score image and the measure at which it takes over."""
    first_measure: int
    path: pathlib.Path

    @property
    def is_vector(self) -> bool:
        return self.path.suffix.lower() == ".svg"


@dataclasses.dataclass
class ScorePackage:
    root: pathlib.Path
    bands: list[Band]                    # sorted by first_measure
    timeline: list[tuple[int, float]]    # (measure, seconds), sorted by time
    manifest: dict
    measures_path: pathlib.Path
    chroma_path: pathlib.Path | None = None   # reference chroma; unused here
    # Humdrum reference records from the score source: COM, OTL, OPS, AGN...
    metadata: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def composer(self) -> str:
        """'Chopin, Fryderyk' -> 'Fryderyk Chopin'."""
        raw = self.metadata.get("COM", "").strip()
        if "," in raw:
            family, given = (p.strip() for p in raw.split(",", 1))
            return f"{given} {family}".strip()
        return raw

    @property
    def surname(self) -> str:
        """The family name alone, for matching and filtering."""
        raw = self.metadata.get("COM", "").strip()
        return (raw.split(",")[0] if "," in raw else raw.split()[-1] if raw
                else "").strip()

    @property
    def title(self) -> str:
        return self.metadata.get("OTL", "").strip()

    @property
    def opus(self) -> str:
        return self.metadata.get("OPS", "").strip()

    @property
    def display_name(self) -> str:
        """Something a musician would recognise, not the folder name."""
        parts = [p for p in (self.title, self.opus) if p]
        return ", ".join(parts) if parts else self.name

    # -- manifest conveniences ------------------------------------------
    @property
    def band_size(self) -> tuple[int, int]:
        """Native band pixel size the package was exported at."""
        return int(self.manifest.get("band_w", 1306)), int(self.manifest.get("band_h", 244))

    @property
    def options(self) -> dict:
        return self.manifest.get("options", {}) or {}

    @property
    def has_vector(self) -> bool:
        return any(b.is_vector for b in self.bands)

    @property
    def name(self) -> str:
        return self.root.name

    def with_alignment(self, measures: pathlib.Path) -> "ScorePackage":
        """This score's bands, timed by a different recording's alignment.

        The package ships the alignment of its own reference performance.
        When a user brings their own recording, auto-sync produces a fresh
        measures.data for it; the bands are unchanged, only the timing is.
        """
        return dataclasses.replace(self, timeline=_read_measures(measures),
                                   measures_path=measures)

    @property
    def last_measure(self) -> int:
        return self.timeline[-1][0] if self.timeline else 0

    @property
    def duration(self) -> float:
        """Timestamp of the final aligned measure, in seconds."""
        return self.timeline[-1][1] if self.timeline else 0.0

    # -- the bit the renderer actually needs ------------------------------
    def band_schedule(self, video_duration: float) -> list[tuple[Band, float, float]]:
        """Resolve bands onto the video timeline.

        Returns (band, start_seconds, end_seconds) covering [0, video_duration],
        in order. A band's start is the timestamp of its first measure; it
        holds until the next band starts. The first band is stretched back to
        zero so the video opens on score rather than on nothing.
        """
        if not self.bands or not self.timeline:
            return []

        when = {measure: seconds for measure, seconds in self.timeline}

        starts: list[tuple[Band, float]] = []
        for band in self.bands:
            t = when.get(band.first_measure)
            if t is None:
                # The exporter can emit a band whose first measure never got
                # an alignment point (e.g. a silent pickup). Fall back to the
                # next aligned measure at or after it.
                later = [s for m, s in self.timeline if m >= band.first_measure]
                if not later:
                    continue
                t = min(later)
            starts.append((band, float(t)))

        if not starts:
            return []
        starts.sort(key=lambda pair: pair[1])
        starts[0] = (starts[0][0], 0.0)   # open on the first band

        schedule = []
        for i, (band, start) in enumerate(starts):
            end = starts[i + 1][1] if i + 1 < len(starts) else video_duration
            if end > start:
                schedule.append((band, start, min(end, video_duration)))
            if end >= video_duration:
                break
        return schedule


def _read_measures(path: pathlib.Path) -> list[tuple[int, float]]:
    """Parse a measures file into (measure, seconds) pairs.

    Two column orders exist upstream and both are accepted:

        export_package  "<measure>\\t<seconds>"                2 columns
        auto_sync V4    "<seconds>\\t<measure>\\t<n>\\t<n>"      4 columns

    The order is decided per-file rather than assumed: measure numbers are
    whole and ascend in small steps, timestamps carry a fractional part, so
    the column that is entirely integral is the measure.
    """
    raw: list[list[str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = line.split()          # tolerate tabs or spaces
        if len(parts) < 2:
            raise PackageError(
                f"{path.name} line {n}: expected two columns, got {line!r}")
        raw.append(parts)

    if not raw:
        raise PackageError(f"{path.name} is empty — this package has no alignment.")

    def integral(index: int) -> bool:
        try:
            return all(float(r[index]).is_integer() for r in raw)
        except ValueError:
            return False

    # 4-column V4 output is always seconds-first; for 2 columns, look.
    measure_col = 1 if (len(raw[0]) >= 4 or (not integral(0) and integral(1))) else 0
    time_col = 1 - measure_col if len(raw[0]) < 4 else 0

    rows: list[tuple[int, float]] = []
    for n, parts in enumerate(raw, start=1):
        try:
            rows.append((int(float(parts[measure_col])), float(parts[time_col])))
        except (ValueError, IndexError) as exc:
            raise PackageError(f"{path.name} line {n}: {exc}") from exc

    rows.sort(key=lambda r: r[1])
    return rows


def can_rasterize_svg() -> bool:
    """Whether .svg bands can actually be turned into pixels here."""
    from . import svg
    return svg.available()


def _read_bands(lines_dir: pathlib.Path) -> list[Band]:
    """Collect lines/<measure>.<ext>, one image per measure.

    Vector is preferred — it rasterises at whatever size the render needs
    instead of resampling a fixed bitmap — but only when a rasteriser is
    installed. Otherwise the raster twin the exporter writes alongside it
    is used, so a package still renders on a machine without cairosvg.
    """
    prefer_vector = can_rasterize_svg()
    by_measure: dict[int, pathlib.Path] = {}
    for path in lines_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if not path.stem.isdigit():
            continue
        measure = int(path.stem)
        current = by_measure.get(measure)
        if current is None:
            by_measure[measure] = path
            continue
        is_svg, had_svg = path.suffix.lower() == ".svg", current.suffix.lower() == ".svg"
        if is_svg != had_svg and is_svg == prefer_vector:
            by_measure[measure] = path

    usable = {m: p for m, p in by_measure.items()
              if prefer_vector or p.suffix.lower() != ".svg"}
    if not usable:
        raise PackageError(
            f"{lines_dir.parent.name} has only .svg bands, and cairosvg isn't "
            f"installed to rasterise them. Either `pip install cairosvg` or "
            f"re-export the package with --fmt-png.")
    return [Band(m, usable[m]) for m in sorted(usable)]


# The artefacts this app reuses, all written by music_line_extractor. They
# split by what they are a property OF:
#
#   score/        properties of the score itself
#     lines/          band images
#     chroma.npy      reference chroma (not needed to render)
#     export.json     manifest: geometry and style the bands were cut at
#
#   reference/    properties of the curated reference performance
#     audio.wav       the reference recording
#     measures.data   the SYNC RESULT — measure to timestamp
#
# measures.data belongs to a performance, not to the score: it is the output
# of aligning one recording, so a different recording has a different one.
# `score/measures.data` is created empty by the exporter and is not the
# alignment; do not read it.
#
# `reference/` is written by the save_as_reference workflow. Before that
# step a project keeps the same files in `performance/`, so both are
# accepted. Flat and `export/` shapes are accepted too, since a fully
# exported package bundles everything into one folder.
_LINES_AT = ("score/lines", "lines", "export/lines")
_MEASURES_AT = ("reference/measures.data", "performance/measures.data",
                "measures.data", "export/measures.data")
_MANIFEST_AT = ("score/export.json", "export.json", "export/export.json")
_CHROMA_AT = ("score/chroma.npy", "chroma.npy", "export/chroma.npy")
_SOURCE_AT = ("score/source.krn", "source.krn", "score/source.musicxml")

# Humdrum reference records worth surfacing. COM is the composer, OTL the
# title, OPS the opus number, AGN the genre.
_WANTED_RECORDS = ("COM", "OTL", "OPS", "ONM", "AGN", "OTP", "PDT")


def _read_score_metadata(source: pathlib.Path) -> dict[str, str]:
    """Pull the reference records from a score source's header.

    Humdrum puts these as `!!!KEY: value` lines. Only the header is scanned —
    the notation itself is of no interest here, and these files are large.
    """
    found: dict[str, str] = {}
    try:
        with source.open("r", encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh):
                if n > 400:
                    break
                if not line.startswith("!!!"):
                    continue
                key, _, value = line[3:].partition(":")
                key = key.strip().upper()
                if key in _WANTED_RECORDS and value.strip():
                    found.setdefault(key, value.strip())
    except OSError:
        pass
    return found


def _first(root: pathlib.Path, candidates: tuple[str, ...],
           *, want_dir: bool = False) -> pathlib.Path | None:
    for rel in candidates:
        path = root / rel
        if path.is_dir() if want_dir else path.is_file():
            # An empty measures.data exists in some projects; treat as absent.
            if want_dir or path.stat().st_size > 0:
                return path
    return None


def load(root: str | pathlib.Path) -> ScorePackage:
    """Load and validate a package folder. Raises PackageError."""
    root = pathlib.Path(root)
    if not root.is_dir():
        raise PackageError(f"{root} is not a folder.")

    lines = _first(root, _LINES_AT, want_dir=True)
    if lines is None:
        raise PackageError("not exported yet — missing a lines/ folder")

    # The alignment is optional. Every job computes its own for the recording
    # it was given, so a package needs only its bands to be renderable. When
    # a reference alignment is present it is loaded for reference/validation
    # purposes, never as the timeline a job renders with.
    measures = _first(root, _MEASURES_AT)

    manifest = {}
    manifest_path = _first(root, _MANIFEST_AT)
    if manifest_path is not None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PackageError(f"{manifest_path.name} is not valid JSON: {exc}") from exc

    source = _first(root, _SOURCE_AT)
    return ScorePackage(root=root, bands=_read_bands(lines),
                        timeline=_read_measures(measures) if measures else [],
                        manifest=manifest, measures_path=measures,
                        chroma_path=_first(root, _CHROMA_AT),
                        metadata=_read_score_metadata(source) if source else {})


def is_package(path: pathlib.Path) -> bool:
    """Cheap check, matching what load() will accept."""
    return _first(path, _LINES_AT, want_dir=True) is not None


def discover(corpus_root: str | pathlib.Path) -> Iterator[ScorePackage]:
    """Yield every loadable package under corpus_root.

    Two layouts are accepted, because the exporter's output sits inside
    each piece's own folder rather than in a shared corpus directory:

        <root>/<piece>/                 flat — root is a corpus of packages
        <root>/<piece>/export/          nested — root is a project folder

    The root itself is also checked, so a single package path works too.
    """
    corpus_root = pathlib.Path(corpus_root)
    if not corpus_root.is_dir():
        return

    for path, loaded, _ in inspect(corpus_root):
        if loaded is not None:
            yield loaded


def candidates(corpus_root: pathlib.Path) -> list[pathlib.Path]:
    """Folders under corpus_root that could be packages."""
    if not corpus_root.is_dir():
        return []
    out = [corpus_root] if is_package(corpus_root) else []
    out += [p for p in sorted(corpus_root.iterdir()) if p.is_dir()]
    return out


def inspect(corpus_root: str | pathlib.Path
            ) -> Iterator[tuple[pathlib.Path, "ScorePackage | None", str]]:
    """Yield (path, package, reason) for each candidate folder.

    `package` is None when the folder can't be used, and `reason` says why —
    which is what the operator needs while populating a corpus.
    """
    corpus_root = pathlib.Path(corpus_root)
    for path in candidates(corpus_root):
        if path == corpus_root and not is_package(path):
            continue
        try:
            yield path, load(path), ""
        except PackageError as exc:
            yield path, None, str(exc)
