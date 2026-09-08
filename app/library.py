"""What we can actually render, and how a recognised piece reaches it.

Recognition names a *recording*; a recording backs one or more score
*editions*; an edition may or may not be installed here. This module owns
the last two steps, because "which scores this install can serve" is a
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

# When one recording backs several editions and more than one is installed,
# something has to choose, and it must choose the same way every time —
# otherwise the edition someone gets changes on the day another is added.
# Vector bands first: they rasterise to any resolution and recolour
# faithfully, where a fixed-size bitmap cannot.
def _preference(entry: "Edition") -> tuple:
    package = entry.package
    return (
        0 if (package and package.has_vector) else 1,
        entry.name,
    )


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


class _Catalogue:
    """The installed packages, re-scanned on a short timer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._at = 0.0
        self._usable: dict[str, pkg.ScorePackage] = {}
        self._broken: dict[str, str] = {}

    def _refresh_if_stale(self) -> None:
        with self._lock:
            if time.time() - self._at < CACHE_SECONDS:
                return
            usable: dict[str, pkg.ScorePackage] = {}
            broken: dict[str, str] = {}
            for root in settings.score_roots:
                if not root.exists:
                    continue
                for path, package, problem in pkg.inspect(root.path):
                    name = path.name
                    if package is None:
                        broken.setdefault(name, problem or "not a usable package")
                        continue
                    if package.surname and not settings.allows(package.surname):
                        continue
                    usable.setdefault(name, package)
            self._usable, self._broken, self._at = usable, broken, time.time()

    def usable(self) -> dict[str, pkg.ScorePackage]:
        self._refresh_if_stale()
        return dict(self._usable)

    def broken(self) -> dict[str, str]:
        self._refresh_if_stale()
        return dict(self._broken)

    def invalidate(self) -> None:
        with self._lock:
            self._at = 0.0


_catalogue = _Catalogue()


def packages() -> list[pkg.ScorePackage]:
    """Every renderable package, in a stable order."""
    return sorted(_catalogue.usable().values(), key=lambda p: p.name)


def find(name: str) -> "pkg.ScorePackage | None":
    """A package by its exact folder name."""
    return _catalogue.usable().get(name)


def editions_for(score_names: list[str]) -> list[Edition]:
    """Resolve the editions a recording backs, best first.

    Every name is reported, installed or not — the interface needs to be
    able to say "we know this piece, we just don't have that score yet".
    """
    usable, broken = _catalogue.usable(), _catalogue.broken()
    found = [
        Edition(name=name,
                package=usable.get(name),
                present=name in usable or name in broken,
                problem=broken.get(name, ""))
        for name in score_names
    ]
    return sorted(found, key=_preference)


def best_edition(score_names: list[str]) -> "Edition | None":
    """The one edition we would render, or None if we could render none."""
    for edition in editions_for(score_names):
        if edition.renderable:
            return edition
    return None


def resolve(candidate) -> dict:
    """Attach edition information to one recognition candidate.

    `candidate` is an `identify.Candidate`; the shape returned is what the
    interface shows in its list, so it answers both "what is it" and "can
    we do anything about it".
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
    """
    for name in candidate.score_names:
        head = name.split("__", 1)[0]
        parts = [p for p in head.split("_") if p]
        if len(parts) >= 2:
            title = parts[1].split("_(")[0]
            return f"{parts[0]} · {title}".strip(" ·")
        if parts:
            return parts[0]
    return candidate.piece_id


def invalidate() -> None:
    """Forget the cached scan; the next read walks the score roots again."""
    _catalogue.invalidate()
