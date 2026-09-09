"""How many videos this install has actually delivered.

A number on a landing page is a claim. This one counts finished renders and
nothing else — not attempts, not test runs already sitting on disk, and
certainly not a figure chosen because it looks better than the truth. It
starts at zero on a fresh install because that is how many people have used
it, and the interface simply says nothing until there is something to say.

Kept in one small file under `var/` so it survives a restart, which the
in-memory job registry does not.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading

from .settings import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _path() -> pathlib.Path:
    return settings.work_dir / "stats.json"


def _read() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def videos() -> int:
    """Finished videos so far."""
    with _lock:
        try:
            return max(0, int(_read().get("videos", 0)))
        except (TypeError, ValueError):
            return 0


def record_video() -> int:
    """Count one more. Returns the new total.

    Failing to write must never fail a render that has already succeeded —
    the video is the point, the tally is not.
    """
    with _lock:
        data = _read()
        try:
            total = max(0, int(data.get("videos", 0))) + 1
        except (TypeError, ValueError):
            total = 1
        data["videos"] = total
        try:
            path = _path()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".part")
            temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            logger.warning("could not record the video count: %s", exc)
        return total
