"""Score packages in the bucket, so a throwaway machine can get one.

WHY THIS EXISTS. Scores used to reach a compute node exactly one way: they
were rsynced onto a machine which was then captured as a Linode image, and
every node booted from that image. Installing a score therefore meant
re-capturing a multi-gigabyte image, and until somebody did, the score was
installed on the web box -- which no longer renders anything. A score could
be fully present, catalogued and advertised to a visitor, and unrenderable.

There are 373 more packages to install, at roughly 30-130 MB each. Baking
them into an image is not a slow process, it is an impossible one: the
image would be twenty gigabytes and would have to be rebuilt for every
addition, while nodes booted from the old one quietly render the wrong
library.

So the bucket is the library, one object per package:

    scores/<folder name>.tar

and a node fetches the ONE package its job names, when the job names it.
That is the same wire, and the same argument, as the input recording: the
node cannot see the web box's disk, the bucket is the only thing both can
reach, so what the node needs travels through it.

WHAT IS IN THE TAR is the INSTALLED shape -- `score/` and `reference/`,
chroma already copied to where the loader looks -- not the project shape a
music_line_extractor folder has. The shape-fixing happens once, at install,
on a host with a known shell; every node then extracts something already
correct rather than each repeating the same three corrections.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time

from . import storage
from .settings import DIGITAL, settings

logger = logging.getLogger(__name__)

PREFIX = "scores/"

# One object holding every package's metadata. ONE, not one per package: the
# web box reads this on a timer to answer "what is in the library", and 375
# separate fetches to answer one page request is not a catalogue, it is a
# fan-out. Publishing is rare and reads it first, so a single object costs
# one extra GET at install time and saves 374 on every read.
CATALOGUE = f"{PREFIX}catalogue.json"

# How long the web box trusts its copy. Long enough that a burst of visitors
# is one fetch, short enough that an operator who installs a score sees it
# without restarting anything.
CATALOGUE_SECONDS = 60.0


def key(name: str) -> str:
    """Where a package lives in the bucket.

    The folder name is the identity everywhere else in this system -- it is
    the pair_list key, the package directory, the thing a RenderTask carries
    -- so it is the key here too, and no mapping table can drift out of
    date. It is our own name, not visitor input: it comes from a folder an
    operator installed, never from an upload.
    """
    return f"{PREFIX}{name}.tar"


def local_root() -> pathlib.Path | None:
    """The score root a fetched package should be extracted into.

    The first configured DIGITAL root that exists, falling back to any
    root. A node has exactly one, so the choice only matters on a machine
    with several -- a laptop lent to the queue, typically, where the roots
    include somebody's working folders.

    The kind is the point of the ordering. This said "digital" and took the
    first root of ANY kind, which on a development box meant a fetched
    package could be unpacked into a source tree that happened to be listed
    first -- thousands of files from the server appearing inside a
    repository, looking like work somebody did. Whatever is listed first
    under SCORE_ROOT_DIGITAL is the intended destination; say so in .env
    and it is honoured.
    """
    for root in settings.score_roots:
        if root.kind == DIGITAL and root.exists:
            return root.path
    for root in settings.score_roots:
        if root.exists:
            return root.path
    return None


class _Band:
    """Enough of a band for the catalogue to be counted and described.

    The web box no longer has the band images -- that is the entire point --
    so it cannot hand out real `Band` objects. What it is asked for is how
    many there are and whether they are vector, and that is what this is.
    """

    __slots__ = ("is_vector", "first_measure")

    def __init__(self, is_vector: bool, first_measure: int = 1) -> None:
        self.is_vector = is_vector
        self.first_measure = first_measure


class Entry:
    """One package as the web box knows it: everything but the bytes.

    Deliberately the same attribute names a `ScorePackage` exposes, because
    every reader of the library -- the works listing, the edition preference
    order, the recognition answer -- was written against a package and there
    is no reason for any of them to learn that the score is somewhere else
    now. What they need is a title, an opus, a band shape and a count; none
    of that requires twenty-five megabytes of engraving on this disk.
    """

    def __init__(self, data: dict) -> None:
        self._d = data
        self.name = data["name"]
        self.title = data.get("title", "")
        self.opus = data.get("opus", "")
        self.edition = data.get("edition", "")
        self.display_name = data.get("display_name") or self.name
        self.surname = data.get("surname", "")
        self.last_measure = int(data.get("last_measure") or 0)
        self.has_vector = bool(data.get("has_vector"))
        self.band_size = tuple(data.get("band_size") or (0, 0))
        self.pages = int(data.get("pages") or 0)
        # The score's own Humdrum header. Carried whole rather than as the
        # two fields read today: it is a few hundred bytes, it is what the
        # CC BY credit is built from, and a catalogue that drops it would
        # have to be republished for all 375 packages the first time
        # anything wanted one more record out of it.
        self.metadata = dict(data.get("metadata") or {})
        self.bands = [_Band(self.has_vector, m)
                      for m in (data.get("band_measures") or [])]
        # There is no local directory. Anything that reaches for one is
        # asking for bytes this host does not have, and should say so
        # rather than read a path that happens not to exist.
        self.root = None

    def to_json(self) -> dict:
        return dict(self._d)


def describe(package) -> dict:
    """A package boiled down to what a catalogue needs."""
    w, h = package.band_size
    return {
        "name": package.name,
        "title": package.title,
        "opus": package.opus,
        "edition": package.edition,
        "display_name": package.display_name,
        "surname": package.surname,
        "last_measure": package.last_measure,
        "has_vector": package.has_vector,
        "band_size": [int(w), int(h)],
        "band_measures": [b.first_measure for b in package.bands],
        "pages": len(sorted(package.root.glob("pages/page_*.svg"))),
        "metadata": dict(package.metadata or {}),
        # What was published, as the app consumes it -- the volunteer's
        # panel compares its own copy against this (package.fingerprint).
        "fingerprint": _fingerprint_or_blank(package),
        "version": _version_or_blank(package),
    }


def _version_or_blank(package) -> dict:
    from . import package as pkgmod
    try:
        return pkgmod.version_of(package.root)
    except OSError:
        return {}


def _fingerprint_or_blank(package) -> str:
    from . import package as pkgmod
    try:
        return pkgmod.fingerprint(package.root)
    except OSError:
        return ""


def publish(folder: pathlib.Path) -> int:
    """Put an installed package in the bucket. Returns the bytes stored.

    Run on the machine that holds the installed copy -- which is the web
    box, because that is where the bucket credentials live and where the
    install just put the package in its correct shape. Tarring locally and
    uploading from an operator's laptop would mean shipping the bucket key
    to the laptop, which is not a trade worth making to save one hop.
    """
    if not folder.is_dir():
        raise FileNotFoundError(f"no such package folder: {folder}")
    # A closed project is a single .spj; what is tarred is its unpacked copy.
    from . import package as pkgmod
    folder = pkgmod.unpacked(folder) or folder
    if not storage.available():
        raise storage.StorageError(
            "no object storage configured, so there is nowhere to publish "
            "a score to and no compute node could fetch it")

    name = folder.name
    with tempfile.TemporaryDirectory() as tmp:
        archive = pathlib.Path(tmp) / f"{name}.tar"
        # Written with the system tar rather than tarfile: the packages
        # contain thousands of small SVGs and GNU tar is several times
        # faster at walking them, and this runs on a box where `tar` is
        # GNU tar by construction.
        done = subprocess.run(
            ["tar", "-cf", str(archive), "-C", str(folder.parent), name],
            capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"tar failed for {name}: {done.stderr.strip()}")
        stored = storage.put(archive, key(name))

    # THE THREE THINGS A WEB BOX NEEDS, none of which is the package. It
    # answers "what is in the library" from the catalogue and draws one
    # decorative plate; it has no reason to carry the engraving, and until
    # this existed it carried all of it -- 134 MB for two scores, and 26 GB
    # for the 375 that are coming.
    from . import fonts as fontdir
    from . import smufl
    loaded = pkgmod.load(folder)
    _put_catalogue_entry(describe(loaded))
    _put_preview(folder)
    # THE FONT THE SCORE WAS DRAWN WITH. Verovio emits a few marks -- the
    # metronome note in a tempo, and anything else it writes as `<rend>` --
    # as live text in a music font, and embeds that font in the SVG. cairosvg
    # ignores embedded fonts and reads only what fontconfig knows, so those
    # marks came out as tofu boxes. Unpacked here, at install time, because
    # fontconfig is cached at process start: a font installed while a render
    # is running does not reach it.
    made = smufl.install_from_package(folder, fontdir.font_dir() / "smufl")
    if made:
        logger.info("installed font(s) %s from %s", ", ".join(made), name)
    _cache.clear()
    return stored


# What the PREVIEW needs, published loose beside the tar. Loose, because the
# web box wants exactly one of these and must not pull a hundred megabytes
# to draw a background; and only these two, because they are all the site
# ever shows before a render: the opening band at its true proportions, and
# one engraved plate behind the page furniture.
PREVIEW_BAND = "band.svg"

# The alignment the package was prepared against: which second each bar of
# the reference recording sounds at. Published loose beside the bands
# because the WATCH page is built on it -- every installed score is a
# performance you can follow before anybody pastes a link -- and the web
# box cannot open the tar to get it.
ALIGNMENT = "alignment.data"

# Where the reference recording came from. The alignment in a package was
# made against a real performance, and for these it is a video on YouTube --
# so a package is not merely a score, it is a performance we can already
# show. Without this the web box knows the timings and not what they are the
# timings OF, and the page has nothing to play.
REFERENCE = "reference.json"


def preview_key(name: str, relative: str) -> str:
    """Where one preview asset lives in the bucket."""
    return f"{PREFIX}{name}/{relative}"


def plate_name(n: int) -> str:
    """The nth engraved plate, numbered from one.

    Renumbered on publish rather than keeping the engraver's filenames: the
    endpoint has always addressed these by position in a sorted list, and
    an index is the only thing a caller can ask for without first being
    told what the files are called.
    """
    return f"pages/p{max(1, n)}.svg"


def band_name(first_measure: int) -> str:
    """One system of a score, addressed by the measure it starts at.

    Published loose alongside the opening band because the WATCH page needs
    the whole piece, a system at a time, and the web box still holds no
    score bytes: it fetches the one it is showing and caches it.
    """
    return f"bands/{int(first_measure)}.svg"


_YT_ID = re.compile(r"([A-Za-z0-9_-]{11})\.(?:mp4|webm|mkv|m4a|wav)", re.I)


def reference_of(folder: pathlib.Path) -> dict | None:
    """Which recording this package's alignment was made against.

    The preparation tools leave a `.meta.json` beside the audio naming the
    file they started from, and for this library that file came from
    YouTube with its id in the name. Read rather than guessed: an id is
    eleven characters of URL-safe base64 and only accepted where it sits in
    front of a media extension.
    """
    for candidate in sorted(folder.glob("*.meta.json")) +             sorted(folder.glob("*/*.meta.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        source = str(data.get("source_video") or data.get("source") or "")
        found = _YT_ID.search(source) or _YT_ID.search(candidate.name)
        if found:
            return {"provider": "youtube", "external_id": found.group(1),
                    "from": pathlib.Path(source).name or candidate.name}
    return None


def _put_preview(folder: pathlib.Path) -> int:
    """The bands and the plates, as loose objects. Returns how many."""
    sent = 0
    bands = sorted((folder / "score" / "lines").glob("*.svg"),
                   key=lambda q: int(q.stem) if q.stem.isdigit() else 0)
    if bands:
        storage.put(bands[0], preview_key(folder.name, PREVIEW_BAND))
        sent += 1
    # EVERY system, addressed by its own first measure. The preview needed
    # exactly one; following a performance needs all of them, and pulling
    # the tar onto the web box to reach system forty is what this whole
    # arrangement exists to avoid.
    for band in bands:
        if band.stem.isdigit():
            storage.put(band, preview_key(folder.name, band_name(int(band.stem))))
            sent += 1
    ident = reference_of(folder)
    if ident:
        # storage.put takes a path and confirms the object afterwards; a
        # temporary file keeps that guarantee rather than adding a second
        # upload path that does not check its work.
        with tempfile.TemporaryDirectory() as hold:
            note = pathlib.Path(hold) / REFERENCE
            note.write_text(json.dumps(ident), encoding="utf-8")
            storage.put(note, preview_key(folder.name, REFERENCE))
        sent += 1
    # reference/ first: an immutable snapshot of the curated performance.
    # Never score/measures.data -- a stale location the extractor deletes
    # from its archives (PROJECT_FOLDER_SPEC.md §9).
    for measures in (folder / "reference" / "measures.data",
                     folder / "performance" / "measures.data"):
        if measures.is_file():
            storage.put(measures, preview_key(folder.name, ALIGNMENT))
            sent += 1
            break
    for n, plate in enumerate(sorted(folder.glob("pages/page_*.svg")), 1):
        storage.put(plate, preview_key(folder.name, plate_name(n)))
        sent += 1
    return sent


# The preview cache, held IN MEMORY and capped. Not on disk: the library is
# heading for 500 GB, and a disk cache is a library that fills up slowly
# instead of all at once. Capped by bytes rather than by entries because the
# entries are engravings and vary tenfold in size.
#
# 24 MB is roughly sixty plates. Past that the least recently asked-for is
# dropped and refetched if anybody wants it again, which costs one second.
PREVIEW_CACHE_BYTES = 24 * 1024 * 1024

_preview_lock = threading.Lock()
_preview_cache: "collections.OrderedDict[str, bytes]" = collections.OrderedDict()


def preview_bytes(name: str, relative: str) -> bytes | None:
    """One preview asset, from the bucket, never written to this disk.

    The web box used to carry every package -- 134 MB for two scores, and
    the library is heading for 500 GB -- in order to serve two small files
    per score that a visitor might look at. It now holds neither the
    packages nor a copy of these: they are fetched on demand and kept in a
    capped cache that dies with the process.

    None when the bucket has no such asset, which is ordinary: most
    packages have no engraved plates at all.
    """
    if not storage.available():
        return None

    key_ = preview_key(name, relative)
    with _preview_lock:
        got = _preview_cache.get(key_)
        if got is not None:
            _preview_cache.move_to_end(key_)
            return got

    if storage.head(key_) is None:
        return None
    # Downloaded THROUGH a temporary file, not into the working directory:
    # `storage.get` confirms the size on the way in, which is the check that
    # makes a truncated download an error instead of a broken image. The
    # file is gone before this returns.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scratch = pathlib.Path(tmp) / "asset"
            storage.get(key_, scratch)
            raw = scratch.read_bytes()
    except Exception:                                  # noqa: BLE001
        logger.warning("could not fetch preview %s", key_, exc_info=True)
        return None

    with _preview_lock:
        _preview_cache[key_] = raw
        _preview_cache.move_to_end(key_)
        held = sum(len(v) for v in _preview_cache.values())
        while held > PREVIEW_CACHE_BYTES and len(_preview_cache) > 1:
            _, dropped = _preview_cache.popitem(last=False)
            held -= len(dropped)
    return raw


def _put_catalogue_entry(entry: dict) -> None:
    """Add or replace one package in the catalogue, keeping the rest.

    Read, modify, write. Two installs running at once would lose one of
    them; installs are an operator running a command and do not overlap,
    and the repair is to publish the loser again.
    """
    current = {e["name"]: e for e in _read_catalogue()}
    current[entry["name"]] = entry
    body = json.dumps({"version": 1,
                       "packages": [current[k] for k in sorted(current)]},
                      ensure_ascii=False, indent=1).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "catalogue.json"
        out.write_bytes(body)
        storage.put(out, CATALOGUE)


def _read_catalogue() -> list[dict]:
    """The catalogue as published, or an empty one if there is none yet."""
    if storage.head(CATALOGUE) is None:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "catalogue.json"
        storage.get(CATALOGUE, out)
        try:
            body = json.loads(out.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.error("the published catalogue is not readable: %s", exc)
            return []
    return list(body.get("packages") or [])


class _Cache:
    """The catalogue, re-fetched on a timer.

    The fetch happens outside the lock, like the disk scan it replaces: it
    is a network round trip and holding every reader on it would make one
    slow bucket a slow site.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._entries: list[dict] = []

    def clear(self) -> None:
        with self._lock:
            self._at = 0.0

    def entries(self, fetch=None) -> list[dict]:
        """The cached list, refreshed on a timer.

        `fetch` so this can serve more than the catalogue: the preview
        index has the same shape of problem -- read on every visit, changed
        when somebody engraves something -- and a second copy of the
        timer-and-fallback dance would be a second place to get it wrong.
        """
        fetch = fetch or _read_catalogue
        with self._lock:
            fresh, at = list(self._entries), self._at
        if at and time.monotonic() - at < CATALOGUE_SECONDS:
            return fresh
        try:
            got = fetch()
        except Exception:                              # noqa: BLE001
            # A bucket that cannot be reached must not empty the library and
            # tell every visitor their score is gone. The last good answer
            # stands until it can be refreshed.
            logger.warning("could not refresh the score catalogue; keeping "
                           "the last one", exc_info=True)
            return fresh
        with self._lock:
            self._entries, self._at = got, time.monotonic()
        return list(got)


_cache = _Cache()


def catalogue_entries() -> list[dict]:
    """The catalogue as published, raw, through the same cache."""
    return _cache.entries()


def entries() -> list[Entry]:
    """Every package in the published library, as the web box sees it."""
    return [Entry(e) for e in _cache.entries()]


PREVIEWS = f"{PREFIX}previews.json"

# The preview index, on the same timer as the catalogue and for the same
# reason: a page asks for it on every visit and it changes when somebody
# engraves something, which is rare.
_previews = _Cache()


def _read_previews() -> list[dict]:
    """The bands engraved for works nobody has packaged yet.

    One object listing every work that has a `band.svg` without having a
    package -- written by `tools/make_preview_bands.py`. Shaped as a list
    of entries so it can share `_Cache` with the catalogue; the caller
    wants a mapping and gets one from `previews()`.
    """
    if storage.head(PREVIEWS) is None:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "previews.json"
        storage.get(PREVIEWS, out)
        try:
            body = json.loads(out.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            logger.error("the published previews are not readable: %s", exc)
            return []
    return [{"name": k, **v} for k, v in body.items()] if isinstance(body, dict) else []


def previews() -> dict[str, dict]:
    """name -> {w, h, title} for every work with a preview band."""
    return {e["name"]: e for e in _previews.entries(_read_previews)}


def published(name: str) -> int | None:
    """The size of the package in the bucket, or None if it is not there."""
    return storage.head(key(name))


def catalogue() -> list[str]:
    """Every package name the bucket holds.

    What a node COULD render, as opposed to what any particular disk
    happens to have on it. The distinction is the whole point of moving the
    library here.
    """
    return sorted(k[len(PREFIX):-len(".tar")]
                  for k in storage.list_keys(PREFIX) if k.endswith(".tar"))


def fetch(name: str, root: pathlib.Path | None = None) -> pathlib.Path | None:
    """Bring one package down from the bucket. Returns where it landed.

    None when the bucket has no such package -- which is a real answer, not
    an error: it means nobody published it, and the caller should say so
    rather than retry.

    EXTRACTED ASIDE AND MOVED INTO PLACE. A package is thousands of files
    and the fetch takes tens of seconds; a second job for the same score
    arriving mid-extraction must not find a half-populated directory and
    conclude the package is installed. So it is built under a scratch name
    and renamed in one step, which on a single filesystem is atomic. If two
    workers race, the loser's rename fails against the winner's finished
    directory and it simply uses that -- the bytes are identical.
    """
    root = root or local_root()
    if root is None:
        raise RuntimeError(
            "no score root exists on this host, so there is nowhere to put "
            "a fetched package")

    final = root / name
    if (final / "score").is_dir():
        return final

    size = published(name)
    if size is None:
        return None

    staging = root / ".fetching"
    staging.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"{os.getpid()}-", dir=staging))
    archive = work / "package.tar"
    try:
        logger.info("fetching score %r from the bucket (%.1f MB)",
                    name, size / 1e6)
        storage.get(key(name), archive)

        unpacked = work / "unpacked"
        unpacked.mkdir()
        with tarfile.open(archive, "r") as tar:
            # `data` refuses absolute paths, `..` segments, links pointing
            # outside the tree and device nodes. These archives are ours,
            # written by our own installer -- but "ours" is a property of
            # the bucket's access control, and an extractor that only works
            # while that holds is one credential leak away from writing
            # anywhere on the node as root.
            tar.extractall(unpacked, filter="data")
        archive.unlink()

        # The tar holds one top-level directory, named for the package.
        inner = unpacked / name
        made = inner if inner.is_dir() else None
        if made is None:
            entries = [p for p in unpacked.iterdir() if p.is_dir()]
            made = entries[0] if len(entries) == 1 else unpacked
        if not (made / "score").is_dir():
            raise RuntimeError(
                f"the published {name!r} has no score/ directory in it")

        try:
            os.replace(made, final)
        except OSError:
            # Another worker finished first. Its copy is the same bytes.
            if (final / "score").is_dir():
                logger.info("score %r arrived from another worker", name)
                return final
            raise
        logger.info("score %r is now on this host", name)
        return final
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ensure(name: str, say=None) -> pathlib.Path | None:
    """The package on this host's disk, fetching it if it is not yet.

    `say` is called once, with a short line, only when a fetch actually
    starts -- a visitor watching a progress bar should be told why it is
    pausing for thirty seconds, and told nothing at all when it is not.
    """
    root = local_root()
    if root is not None and (root / name / "score").is_dir():
        return root / name
    if not storage.available():
        return None
    if published(name) is None:
        return None
    if say:
        say("fetching the score")
    return fetch(name, root)
