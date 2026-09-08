"""What we can actually render, and how a recognised piece reaches it.

Recognition names a *recording*; a recording backs one or more score
*editions*; an edition may or may not be installed here. This module owns
the last two steps, because which scores an install can serve is a
property of the deployment, not of the recogniser.

The names line up exactly — a pair_list entry like

    Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH

is character-for-character the package's folder name — so resolution is a
dictionary lookup and never a fuzzy match. A package whose folder name
differs by one character is reported as missing rather than guessed at,
because quietly rendering the wrong edition is worse than saying no.

Installed is not the same as renderable: a package needs `score/lines`,
which is where the band images live. Both states are reported, because
"it is there but unusable" and "it is not there" need different answers.

The catalogue is re-read on a short timer, so dropping a new package on
disk makes it available without restarting anything.
"""

from __future__ import annotations

import dataclasses
import threading
import time

from . import package as pkg
from .settings import settings

# How long a scan of the score roots stays good. Short enough that adding a
# package feels immediate, long enough that a burst of requests does not
# re-walk the disk each time.
CACHE_SECONDS = 10.0


@dataclasses.dataclass(frozen=True)
class Edition:
    """One score edition a recognised recording could be rendered against."""
    name: str                                  # the pair_list / folder name
    package: "pkg.ScorePackage | None" = None  # loaded, when it is usable
    present: bool = False                      # a folder of that name exists
    problem: str = ""                          # why it is present but unusable

    @property
    def renderable(self) -> bool:
        return self.package is not None

    def public(self) -> dict:
        out = {"name": self.name, "renderable": self.renderable,
               "present": self.present}
        if self.package is not None:
            out.update(label=self.package.display_name,
                       bands=len(self.package.bands),
                       vector=self.package.has_vector,
                       measures=self.package.last_measure)
        elif self.problem:
            out["problem"] = self.problem
        return out


def _preference(entry: Edition) -> tuple:
    """The order editions are offered in, best first.

    Renderable first, then vector — svg bands rasterise to any resolution
    and recolour faithfully where a fixed-size bitmap cannot — then name,
    so the result never depends on the order the disk was walked in.

    This is deterministic for a given set of installed packages, but it is
    NOT stable as packages are added: Op.31 backs both `Op.31_Scherzo Pour
    Piano_(M. Schlesinger)` and `Op.31_Scherzo_(Breitkopf)`, and a space
    sorts before an underscore, so installing the first would displace the
    second. Until an explicit publisher order is configured, the interface
    must show which edition it chose rather than leave it implied.
    """
    return (
        0 if entry.renderable else 1,
        0 if (entry.package and entry.package.has_vector) else 1,
        entry.name,
    )


@dataclasses.dataclass(frozen=True)
class _Snapshot:
    usable: dict           # folder name -> ScorePackage
    broken: dict           # folder name -> why it cannot be used
    at: float


class _Catalogue:
    """The installed packages, re-scanned on a short timer.

    The scan happens outside the lock — it walks the disk and parses every
    package, which is far too long to hold readers on — and only the swap
    is guarded. A scan racing another wastes a little work and no
    correctness: both produce the same answer and the later one wins.
    """

    _EMPTY = _Snapshot({}, {}, 0.0)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = self._EMPTY

    def snapshot(self) -> _Snapshot:
        """The current catalogue, rescanned if it has gone stale."""
        with self._lock:
            current = self._snapshot
        # monotonic, so a clock adjustment cannot freeze or expire the cache
        if time.monotonic() - current.at < CACHE_SECONDS and current.at:
            return current

        fresh = self._scan()
        with self._lock:
            self._snapshot = fresh
        return fresh

    @staticmethod
    def _scan() -> _Snapshot:
        usable: dict = {}
        broken: dict = {}
        for root in settings.score_roots:
            if not root.exists:
                continue
            try:
                found = list(pkg.inspect(root.path))
            except Exception as exc:  # noqa: BLE001 - one bad root must not
                # take the library down; an unreadable folder or a file with
                # an unexpected encoding is the operator's to fix, meanwhile
                # every other root still works.
                broken.setdefault(str(root.path), f"could not be read: {exc}")
                continue
            for path, package, problem in found:
                name = path.name
                if package is None:
                    broken.setdefault(name, problem or "not a usable package")
                    continue
                if package.surname and not settings.allows(package.surname):
                    continue
                usable.setdefault(name, package)
        # A package that is usable under one root is not broken just because
        # an unusable copy of it sits under another.
        for name in usable:
            broken.pop(name, None)
        return _Snapshot(usable, broken, time.monotonic())


_catalogue = _Catalogue()


def packages() -> list[pkg.ScorePackage]:
    """Every renderable package, in a stable order."""
    return sorted(_catalogue.snapshot().usable.values(), key=lambda p: p.name)


def find(name: str) -> "pkg.ScorePackage | None":
    """A package by its exact folder name."""
    return _catalogue.snapshot().usable.get(name)


def editions_for(score_names: list[str]) -> list[Edition]:
    """Resolve the editions a recording backs, best first.

    Every name is reported, installed or not — the interface needs to be
    able to say "we know this piece, we just don't have that score yet".
    """
    state = _catalogue.snapshot()          # one read: never mix two scans
    found = [
        Edition(name=name,
                package=state.usable.get(name),
                present=name in state.usable or name in state.broken,
                problem=state.broken.get(name, ""))
        for name in score_names
    ]
    return sorted(found, key=_preference)


def resolve(candidate) -> dict:
    """Attach edition information to one recognition candidate.

    `candidate` is an `identify.Candidate`; the shape returned is what the
    interface shows, so it answers both "what is it" and "can we do
    anything about it".
    """
    editions = editions_for(candidate.score_names)
    chosen = next((e for e in editions if e.renderable), None)
    return {
        **candidate.public(),
        "renderable": chosen is not None,
        "package": chosen.name if chosen else None,
        "label": chosen.package.display_name if chosen else _readable(candidate),
        "editions": [e.public() for e in editions],
    }


def _readable(candidate) -> str:
    """A name to show for a piece we cannot render, so it is still named.

    Derived from the edition name's own structure — `Op.39_3ème Scherzo
    pour le Piano_(Breitkopf)__039-1-BH` gives up its opus and title
    without anything having to parse the recording's filename, which is
    unreliable: `work_op_72_no_2_*` names two unrelated works.

    Rough by design. Real names include `[Op. 32]_[Nocturne No. 2.]` and
    `Waltz__070-1-Sam-003`, which carries its opus only in the tail, so
    this is a label to recognise a piece by, not a citation.
    """
    for name in candidate.score_names:
        parts = [p for p in name.split("__", 1)[0].split("_") if p]
        if len(parts) >= 2:
            return f"{parts[0]} · {parts[1]}"
        if parts:
            return parts[0]
    return candidate.piece_id
