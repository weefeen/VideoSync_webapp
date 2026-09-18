"""Reading a score package produced by music_line_extractor.

This app does no score preparation and no synchronisation. Both happen
upstream in music_line_extractor, whose ``export_package`` workflow writes
a folder we treat as a read-only input contract:

    lines/<first_measure>.{svg,png,jpg}   one image per score band
    reference/measures.data               "<seconds> <sync key> <p> <M>", tab-separated
    export.json                           manifest: geometry, style, entries
    chroma.npy                            present, unused here (no syncing)

THREE programs read this folder -- music_line_extractor writes it,
VideoScoreSync and this app read it -- and they have already drifted
apart twice. `docs/score-package-contract.md` records where, and what
each one does differently; read it before concluding a band is broken.

The two repositories share no code. If the contract below stops matching
what the exporter writes, this is the only file that needs to change.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import posixpath
import zipfile
from typing import Iterator

logger = logging.getLogger(__name__)

# Band images are named by the first measure they cover; a band stays on
# screen until the next one's first measure is reached.
IMAGE_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg")


class PackageError(ValueError):
    """The folder isn't a usable score package. Message is user-facing."""


@dataclasses.dataclass(frozen=True)
class Band:
    """One score image and the measure at which it takes over.

    `first_measure` is the SOURCE bar the band starts at -- its file name,
    and what a person reads. `sync_key` is the aligner's index for that
    same bar, from export.json, and is what the timeline is keyed by. They
    coincide unless the piece has a pickup bar or a cadenza spread over
    extra boxes; four packages coincided and the fifth did not.
    """
    first_measure: int
    path: pathlib.Path
    sync_key: int = -1

    @property
    def key(self) -> int:
        return self.sync_key if self.sync_key >= 0 else self.first_measure

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
    def edition(self) -> str:
        """The publisher this engraving came from, as a musician would cite it.

        Musicians choose editions deliberately -- a Breitkopf Chopin and a
        Paderewski Chopin disagree about phrasing, fingering and sometimes
        notes -- so a score video that will not say which one it used is
        worth less to exactly the people who care most. The package has
        carried PPR and PPP all along and nothing ever showed them.
        """
        publisher = (self.metadata.get("PPR", "") or "").strip()
        place = (self.metadata.get("PPP", "") or "").strip()
        if publisher and place:
            return f"{publisher}, {place}"
        return publisher or place

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
    def _key_of(self, band: Band) -> int:
        """The band's sync key, from export.json; its first measure if absent."""
        if band.sync_key >= 0:
            return band.sync_key
        for entry in self.manifest.get("entries") or []:
            if entry.get("first_measure") == band.first_measure and "sync_key" in entry:
                return int(entry["sync_key"])
        return band.first_measure

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
            # BY SYNC KEY, which is the timeline's numbering. Looked up by
            # first_measure this was right only while the two coincided.
            key = self._key_of(band)
            t = when.get(key)
            if t is None:
                # The exporter can emit a band whose first measure never got
                # an alignment point (e.g. a silent pickup). Fall back to the
                # next aligned measure at or after it.
                later = [s for m, s in self.timeline if m >= key]
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


def read_measures_text(text: str, *, name: str = "measures.data") -> list[tuple[int, float]]:
    """The same parser, on text that never touched this disk.

    The web box holds no packages; an alignment fetched from the bucket has
    to be read by the SAME code that reads one on disk, or the two drift
    and a measure number gets read as a timestamp again.
    """
    return _parse_measures(text.splitlines(), name)


def _read_measures(path: pathlib.Path) -> list[tuple[int, float]]:
    """Parse a measures file into (measure, seconds) pairs.

    Two column orders exist upstream and both are accepted:

        export_package  "<measure>\\t<seconds>"                2 columns
        auto_sync V4    "<seconds>\\t<measure>\\t<n>\\t<n>"      4 columns

    The order is decided per-file rather than assumed: measure numbers are
    whole and ascend in small steps, timestamps carry a fractional part, so
    the column that is entirely integral is the measure.
    """
    return _parse_measures(path.read_text(encoding="utf-8").splitlines(), path.name)


def _parse_measures(lines, name: str) -> list[tuple[int, float]]:
    """(sync key, seconds) pairs -- the aligner's own numbering.

    A project's `measures.data` has four tab-separated columns:

        <seconds>  <sync key>  <source bar>  <source bar>

    TWO NUMBERINGS, AND THE APP KEYS EVERYTHING BY THE SYNC KEY. It is the
    index the aligner follows -- one per box on the page, so a pickup bar
    is key 1 for source bar 0, and a cadenza spread over extra boxes gives
    keys that no source bar owns (the source columns are empty). Every box
    in `measures-from-score.json` carries the same key, which is what lets
    a timing meet its box. The SOURCE bar is what a person reads and is
    shown as the label; see `viewer.payload`.

    Rows whose source columns are empty are kept: they have a key, a time
    and a box. Tabs are the delimiter when present so those empty columns
    survive -- `split()` would collapse them. A two-column file has one
    numbering and is read as before.
    """
    raw: list[list[str]] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        parts = (line.rstrip("\r\n").split("\t") if "\t" in line
                 else line.split())
        if len([p for p in parts if p.strip()]) < 2:
            raise PackageError(
                f"{name} line {n}: expected two columns, got {line!r}")
        raw.append([p.strip() for p in parts])

    if not raw:
        raise PackageError(f"{name} is empty — this package has no alignment.")

    four = all(len(r) >= 4 for r in raw)
    rows: list[tuple[int, float]] = []
    if four:
        for n, parts in enumerate(raw, start=1):
            try:
                rows.append((int(float(parts[1])), float(parts[0])))
            except ValueError as exc:
                raise PackageError(f"{name} line {n}: {exc}") from exc
    else:
        def integral(index: int) -> bool:
            try:
                return all(float(r[index]).is_integer() for r in raw)
            except (ValueError, IndexError):
                return False
        measure_col = 1 if (not integral(0) and integral(1)) else 0
        time_col = 1 - measure_col
        for n, parts in enumerate(raw, start=1):
            try:
                rows.append((int(float(parts[measure_col])), float(parts[time_col])))
            except (ValueError, IndexError) as exc:
                raise PackageError(f"{name} line {n}: {exc}") from exc

    rows.sort(key=lambda r: r[1])
    return rows


def sync_key_to_bar(measures: "pathlib.Path | bytes | str") -> dict[int, int]:
    """The package's own map from sync keys to the SOURCE bar they show.

    Read from the reference `measures.data`: `<seconds> <s> <p> <M>` -- the
    second column is the key and the FOURTH the source bar (the third is
    music21's own number, never a label; see PROJECT_FOLDER_SPEC.md §4). Keys with no bar -- the extra boxes a cadenza
    is spread over -- are absent, and a caller labelling boxes lets them
    inherit the bar before them. Empty for a two-column file, where the two
    numberings are one and the key IS the bar.
    """
    if isinstance(measures, pathlib.Path):
        text = measures.read_text(encoding="utf-8")
    elif isinstance(measures, bytes):
        text = measures.decode("utf-8", errors="replace")
    else:
        text = measures
    out: dict[int, int] = {}
    for line in text.splitlines():
        if "\t" not in line:
            return {}
        parts = [p.strip() for p in line.rstrip("\r\n").split("\t")]
        if len(parts) < 4 or not parts[1] or not parts[3]:
            continue
        try:
            out[int(float(parts[1]))] = int(float(parts[3]))
        except ValueError:
            continue
    return out


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
_CHROMA_AT = ("performance/chroma.npy", "reference/chroma.npy",
              "score/chroma.npy", "chroma.npy", "export/chroma.npy")
_SOURCE_AT = ("score/source.krn", "source.krn", "score/source.musicxml")

# Humdrum reference records worth surfacing. COM is the composer, OTL the
# title, OPS the opus number, AGN the genre. PPR and PPP name the first
# edition's publisher and city: the scores are CC BY 4.0 from the Fryderyk
# Chopin Institute and the licence requires that attribution, so it is read
# from the score itself rather than typed in anywhere.
_WANTED_RECORDS = ("COM", "OTL", "OPS", "ONM", "AGN", "OTP", "PDT",
                   "PPR", "PPP")


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


def bundle_of(root: pathlib.Path) -> pathlib.Path | None:
    """The project's `.spj`, if the folder carries one under its own name."""
    root = pathlib.Path(root).resolve()
    spj = root / f"{root.name}.spj"
    return spj if spj.is_file() else None


_BUNDLE_BANDS: dict[str, tuple[tuple, bool]] = {}


def bundle_has_bands(spj: pathlib.Path) -> bool:
    """Does the bundle hold a `lines/` folder? Read from its directory only.

    A project that was never exported still closes into a `.spj`; listing
    it as a package would only make the loader say "not exported yet" on
    every look. Cached by size and mtime -- the central directory is cheap,
    but the panel asks every second.
    """
    try:
        st = spj.stat()
    except OSError:
        return False
    sig = (st.st_size, st.st_mtime_ns)
    hit = _BUNDLE_BANDS.get(str(spj))
    if hit is not None and hit[0] == sig:
        return hit[1]
    try:
        with zipfile.ZipFile(spj) as z:
            has = any(n.startswith(("score/lines/", "lines/", "export/lines/"))
                      and not n.endswith("/") for n in z.namelist())
    except (OSError, zipfile.BadZipFile):
        has = False
    _BUNDLE_BANDS[str(spj)] = (sig, has)
    return has


def is_open(root: pathlib.Path) -> bool:
    """Loose `score/lines` on disk -- the project is open, or installed."""
    return _first(root, _LINES_AT, want_dir=True) is not None


def unpacked(root: str | pathlib.Path) -> pathlib.Path | None:
    """Where a CLOSED project's files can be read, or None if it is open.

    OPEN OR CLOSED -- CHECK FIRST. The extractor keeps `score/`,
    `performance/` and `reference/` loose only while a project is open; on
    close it packs them into `<piece_id>.spj` and deletes the loose copies,
    so the same folder is sometimes a tree and sometimes a single zip
    (PROJECT_FOLDER_SPEC.md §1). The loose files win whenever they exist:
    they are the live state, and a stale bundle often sits beside them.

    A closed project is unpacked ONCE into this app's own work directory,
    never back into the project folder -- that folder belongs to the
    extractor, which would pack whatever it finds there. The copy is
    refreshed when the bundle's size or mtime changes, and its folder keeps
    the piece's name, which is the join key everywhere.
    """
    root = pathlib.Path(root).resolve()
    if is_open(root):
        return None
    spj = bundle_of(root)
    if spj is None:
        return None
    try:
        from .settings import settings                      # noqa: PLC0415
        home = settings.work_dir / "unpacked"
    except Exception:                                       # noqa: BLE001
        import tempfile                                     # noqa: PLC0415
        home = pathlib.Path(tempfile.gettempdir()) / "vsw-unpacked"
    target = home / root.name
    stat = spj.stat()
    stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    marker = target / ".from-spj"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == stamp:
        return target
    import shutil                                           # noqa: PLC0415
    fresh = target.with_name(target.name + ".unpacking")
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir(parents=True)
    with zipfile.ZipFile(spj) as z:
        for info in z.infolist():
            rel = pathlib.PurePosixPath(info.filename)
            if info.is_dir() or rel.is_absolute() or ".." in rel.parts:
                continue
            out = fresh.joinpath(*rel.parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    (fresh / ".from-spj").write_text(stamp, encoding="utf-8")
    # Swap, not delete-then-rename: on Windows a folder whose files were
    # just read can refuse to go, and a rename onto it is "access denied".
    # The old copy is moved aside first and removed afterwards, best effort.
    old = target.with_name(target.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    fresh.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    logger.info("%s is closed; read from its .spj (%d MB) into %s",
                root.name, stat.st_size >> 20, target)
    return target


_FINGERPRINTS: dict[str, tuple[tuple, str]] = {}


def _consumed_files(root: pathlib.Path) -> list[tuple[str, pathlib.Path]]:
    """The files this app reads from a package, under names both sides share.

    The alignment is `performance/measures.data` in a project and
    `reference/measures.data` once installed; it is listed as
    `alignment/measures.data` so the two copies compare equal. Everything
    else -- audio, chroma, pickles, plates, the bundle itself -- is not
    consumed and does not count as a change.
    """
    out: list[tuple[str, pathlib.Path]] = []
    score = root / "score"
    for rel in ("export.json", "measures-from-score.json", "source.krn"):
        if (score / rel).is_file():
            out.append((f"score/{rel}", score / rel))
    lines = _first(root, _LINES_AT, want_dir=True)
    if lines is not None:
        for p in sorted(lines.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                out.append((f"lines/{p.name}", p))
    measures = _first(root, _MEASURES_AT)
    if measures is not None:
        out.append(("alignment/measures.data", measures))
    return out


VERSION_FILE = "version.json"      # an installed copy's note: what was sent
_IDS: dict[str, tuple[tuple, str]] = {}
_VERSIONS: dict[str, tuple[tuple, dict]] = {}

# PROJECT_FOLDER_SPEC.md §7.1 -- the content id an external service computes
# for a project. Their recipe, kept in step with their test
# (tests/test_project_folder_contract_snippet.py); do not improve it here.
_SKIP_EXACT = {"chroma.npy", "score/chroma.npy", "score/measures.data"}
_BASENAME_FIELDS = ("video_file", "audio_file", "source_pdf_path",
                    "video_file_path")


def _included(arc: str) -> bool:
    return not (arc.endswith("/") or arc in _SKIP_EXACT
                or arc.startswith("score/prepared/")
                or arc.endswith(".meta.json")
                or posixpath.basename(arc) == "audio.wav")


def _canonical(arc: str, data: bytes) -> bytes:
    if not arc.endswith(".json"):
        return data
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception:                                       # noqa: BLE001
        return data
    if isinstance(obj, dict):
        obj.pop("version", None)                  # future self-reference
        for f in _BASENAME_FIELDS:                # machine-local paths
            v = obj.get(f)
            if isinstance(v, str) and v:
                obj[f] = posixpath.basename(v.replace("\\", "/"))
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _loose_entries(root: pathlib.Path, spj: "pathlib.Path | None"
                   ) -> list[tuple[str, pathlib.Path]]:
    out: list[tuple[str, pathlib.Path]] = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            fp = pathlib.Path(dirpath) / f
            if fp == spj or fp.suffix.lower() == ".mp4":
                continue
            rel = fp.relative_to(root).as_posix()
            if _included(rel):
                out.append((rel, fp))
    return sorted(out)


def content_id(root: str | pathlib.Path) -> str:
    """THE PROJECT'S IDENTITY, as PROJECT_FOLDER_SPEC.md §7.1 defines it.

    "sha256:<hex>" over the contents of the meaningful entries -- every
    entry of the bundle, overlaid by the loose files at the same path
    (loose wins: the live state), minus audio, provenance, pickles and the
    stale locations -- each JSON entry canonicalised so machine-local
    paths and formatting do not count. Same project, same id, on any
    machine, open or closed. What the volunteer's panel compares against
    the id the site was sent, and what the extractor's own `version`
    block will carry once it mints one.

    Not computable on an installed copy: the installer ships neither the
    bundle nor project.json, and renames performance/ to reference/. The
    server keeps the id it was SENT (see `installed_note`). Cached by the
    stat of every contributing file; the panel asks every second.
    """
    root = pathlib.Path(root).resolve()
    spj = bundle_of(root)
    if spj is None:
        spj = next(iter(sorted(root.glob("*.spj"))), None)
    loose = _loose_entries(root, spj)
    sig: list = [("spj", spj.stat().st_size, spj.stat().st_mtime_ns)] if spj else []
    sig += [(rel, fp.stat().st_size, fp.stat().st_mtime_ns) for rel, fp in loose]
    signature = tuple(sig)
    hit = _IDS.get(str(root))
    if hit is not None and hit[0] == signature:
        return hit[1]
    entries: dict[str, bytes] = {}
    if spj is not None:
        with zipfile.ZipFile(spj) as zf:
            entries = {n: zf.read(n) for n in zf.namelist() if _included(n)}
    for rel, fp in loose:                                   # loose overrides
        entries[rel] = fp.read_bytes()
    h = hashlib.sha256()
    for arc in sorted(entries):
        blob = _canonical(arc, entries[arc])
        h.update(arc.encode()); h.update(b"\0")
        h.update(str(len(blob)).encode()); h.update(b"\0")
        h.update(blob); h.update(b"\0")
    digest = "sha256:" + h.hexdigest()
    _IDS[str(root)] = (signature, digest)
    return digest


def installed_note(root: str | pathlib.Path) -> dict:
    """What an installed copy was sent as: {"content_id", "version"}, or {}.

    Left beside `score/` by the installer (`tools/check_score.py`), because
    the copy on the server cannot compute its own id (see `content_id`).
    """
    note = pathlib.Path(root) / VERSION_FILE
    try:
        if not note.is_file():
            return {}
        data = json.loads(note.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {"content_id": str(data.get("content_id") or ""),
            "version": data.get("version") if isinstance(data.get("version"), dict) else {}}


def version_of(root: str | pathlib.Path) -> dict:
    """The extractor's minted version block for this project, or {}.

    PROJECT_VERSION_ID_SPEC.md §4.5: `project.json::version`
    {content_id, parent_id, revision, minted_at_utc, device_id}, minted at
    export -- a label, not a live checksum: a project edited since is
    "dirty" (§6), which `content_id` != version.content_id shows. Not
    implemented in the extractor yet, so every project returns {} today;
    read from the bundle's project.json (authoritative, spec §2), then a
    loose one, then an installed copy's note.
    """
    root = pathlib.Path(root).resolve()
    note = root / VERSION_FILE
    spj = bundle_of(root)
    data = None
    try:
        if spj is not None:
            st = spj.stat()
            sig = ("spj", st.st_size, st.st_mtime_ns)
            hit = _VERSIONS.get(str(root))
            if hit is not None and hit[0] == sig:
                return dict(hit[1])
            with zipfile.ZipFile(spj) as z:
                if "project.json" in z.namelist():
                    data = json.loads(z.read("project.json").decode("utf-8"))
        elif (root / "project.json").is_file():
            st = (root / "project.json").stat()
            sig = ("loose", st.st_size, st.st_mtime_ns)
            data = json.loads((root / "project.json").read_text(encoding="utf-8"))
        elif note.is_file():
            return installed_note(root).get("version") or {}
        else:
            return {}
    except (OSError, ValueError, zipfile.BadZipFile):
        return {}
    block = (data or {}).get("version")
    if not isinstance(block, dict):
        flat = (data or {}).get("project_content_id") or (data or {}).get("content_id")
        block = {"content_id": str(flat)} if flat else {}
    out = {k: block[k] for k in ("content_id", "parent_id", "revision",
                                 "minted_at_utc", "saved_at_utc", "device_id")
           if k in block}
    _VERSIONS[str(root)] = (sig, out)
    return dict(out)


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

    # Open or closed? Loose files first; a closed project is read from its
    # bundle. The package then knows the unpacked copy as its root, and its
    # name -- the piece id -- is the same either way.
    try:
        root = unpacked(root) or root
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageError(f"{root.name}.spj could not be read: {exc}") from exc

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
    """Cheap check, matching what load() will accept: open, or closed."""
    if is_open(path):
        return True
    spj = bundle_of(path)
    return spj is not None and bundle_has_bands(spj)


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
    # Dot-directories are never packages. `.fetching` is where a compute
    # node builds a package it is downloading from the bucket, so during a
    # fetch this directory holds a half-extracted tree -- and without this
    # line every catalogue listing on that node would carry a broken entry
    # that comes and goes on its own.
    out += [p for p in sorted(corpus_root.iterdir())
            if p.is_dir() and not p.name.startswith(".")]
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
