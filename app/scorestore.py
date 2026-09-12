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

import logging
import os
import pathlib
import shutil
import subprocess
import tarfile
import tempfile

from . import storage
from .settings import settings

logger = logging.getLogger(__name__)

PREFIX = "scores/"


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

    The first configured digital root that exists. A node has exactly one;
    the choice only matters on a development box with several, and there
    the first is the one the operator listed first.
    """
    for root in settings.score_roots:
        if root.exists:
            return root.path
    return None


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
        return storage.put(archive, key(name))


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
