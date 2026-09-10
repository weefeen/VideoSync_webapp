"""Who has used this, from where, and what they played.

Three questions, one table each, and they are deliberately not joined into
one:

    visitors()   one row per address — how many uploads, how much video,
                 how much of it succeeded, and where in the world it was.
    pieces()     one row per work the recogniser has ever named — how often,
                 by how many different people, and how long it runs.
    recent()     the individual answers, newest first, for reading the two
                 above when a number looks wrong.

WHY THE ADDRESS IS KEPT AT ALL. There is no login here and there is not going
to be one, so the address is the only thing that distinguishes one visitor
from another: the limits that stop a single person occupying the machine all
afternoon are counted against it. Beyond that, where uploads actually come
from is what decides where the servers should sit — a European audience
served from Newark is a slower site for no reason, and that question cannot
be answered from an empty table a year from now.

WHY THE LOOKUP IS LOCAL. Country and city come from a MaxMind-format database
file on this disk. Sending visitors' addresses to a geolocation API would
hand a third party a list of who watched what and when, in exchange for a
column — so the alternative was not considered. If the file is absent the
addresses are simply listed unresolved.

NOT FOR THE PUBLIC. Same rule as `/metrics` and `/api/failures`: the endpoint
that serves this is loopback-only and whatever fronts the site must keep it
off the internet.
"""
from __future__ import annotations

import ipaddress
import logging
import statistics
from typing import Any

from . import store
from .settings import settings

logger = logging.getLogger(__name__)

# Resolved once. The database is a memory-mapped file of a few tens of
# megabytes and reopening it per row would dominate the cost of the page.
_reader = None
_reader_tried = False
_reader_problem = ""


def _open_reader():
    """The geolocation database, or None with a reason recorded.

    Both failures are ordinary and neither is fatal: the reader library is an
    optional dependency, and the database is a file somebody has to download.
    Missing either means a list without country and city, which is still the
    list that was asked for.
    """
    global _reader, _reader_tried, _reader_problem
    if _reader_tried:
        return _reader
    _reader_tried = True

    path = settings.geoip_db
    if not path:
        _reader_problem = ("GEOIP_DB is not set, so addresses are not "
                           "resolved to a country or city.")
        return None
    if not path.exists():
        _reader_problem = f"GEOIP_DB points at {path}, which is not there."
        return None
    try:
        import maxminddb                            # noqa: PLC0415
    except ImportError:
        _reader_problem = ("The `maxminddb` package is not installed, so the "
                           "geolocation database cannot be read. "
                           "`pip install maxminddb`.")
        return None
    try:
        _reader = maxminddb.open_database(str(path))
    except Exception as exc:                        # noqa: BLE001
        _reader_problem = f"{path} could not be opened: {exc}"
        logger.warning("geolocation unavailable: %s", _reader_problem)
    return _reader


def _english(node: Any) -> str:
    """A name out of a MaxMind record, which stores them per language."""
    if isinstance(node, dict):
        names = node.get("names") or {}
        return names.get("en") or next(iter(names.values()), "") or ""
    return ""


def locate(address: str) -> dict[str, str]:
    """Country and city for one address. Never raises, never asks anybody.

    Private and loopback addresses are answered without a lookup: they are
    this machine talking to itself, or a reverse proxy that TRUST_PROXY has
    not been told about — and a database will report neither.
    """
    blank = {"country": "", "country_code": "", "city": "", "region": ""}
    if not address:
        return blank
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return blank
    if ip.is_loopback:
        return {**blank, "country": "this machine", "country_code": "--"}
    if ip.is_private:
        return {**blank, "country": "private network", "country_code": "--"}

    reader = _open_reader()
    if reader is None:
        return blank
    try:
        record = reader.get(address)
    except Exception:                               # noqa: BLE001
        logger.debug("geolocation failed for one address", exc_info=True)
        return blank
    if not record:
        return blank

    subdivisions = record.get("subdivisions") or []
    return {
        "country": _english(record.get("country")),
        "country_code": (record.get("country") or {}).get("iso_code", ""),
        "city": _english(record.get("city")),
        "region": _english(subdivisions[0]) if subdivisions else "",
    }


def geolocation_status() -> dict[str, Any]:
    """Whether the lookup works, and why not when it does not.

    Reported alongside the list rather than logged, because "every city is
    blank" and "the database is missing" look identical in a table and only
    one of them is worth acting on.
    """
    reader = _open_reader()
    return {"available": reader is not None,
            "database": str(settings.geoip_db) if settings.geoip_db else "",
            "problem": "" if reader is not None else _reader_problem}


def visitors() -> list[dict[str, Any]]:
    """Every address that has uploaded here, most recent first.

    Uploads and recognitions are counted in two separate queries and matched
    by address afterwards, never in one join: an address that listened four
    times to a single upload would otherwise multiply that upload by four and
    be reported as having sent four videos.

    The location here is resolved live from the address, so it is as good as
    today's database. `countries()` uses what was recorded at the time
    instead — see there for why the two can differ.
    """
    heard = {row["client"]: row for row in store.listens_by_client()}

    out = []
    for row in store.visitors():
        address = row["client"]
        listened = heard.get(address)
        out.append({
            "address": address,
            **locate(address),
            "uploads": row["uploads"],
            "delivered": row["delivered"] or 0,
            "failed": row["failed"] or 0,
            "identifications": listened["listens"] if listened else 0,
            "recognised": (listened["matched"] or 0) if listened else 0,
            "pieces": (listened["pieces"] or 0) if listened else 0,
            "minutes": round((row["seconds"] or 0) / 60.0, 1),
            "megabytes": round((row["bytes"] or 0) / (1024 * 1024), 1),
            # Milliseconds, which is what Grafana reads as a time axis.
            "first_seen": int((row["first_seen"] or 0) * 1000),
            "last_seen": int((row["last_seen"] or 0) * 1000),
        })
    return out


def countries() -> list[dict[str, Any]]:
    """Uploads grouped by country and city — the server-location answer.

    Built from what was recorded at the time of each upload, NOT from
    re-resolving the addresses now. Two reasons, and both matter:

      * these rows outlive the recording. An address is deleted when somebody
        asks us to delete their video; the country it came from is not
        personal data and stays, so this stays answerable.
      * addresses move. A block reassigned from Lisbon to Frankfurt would
        silently rewrite last year's history if the lookup ran now.

    Sorted by minutes of video rather than by number of people: it is the
    volume that has to cross an ocean, and that is what a server's location
    is chosen against.
    """
    totals: dict[str, dict[str, Any]] = {}
    for row in store.places():
        into = totals.setdefault(row["country"], {
            "country": row["country"], "uploads": 0,
            "minutes": 0.0, "cities": {}})
        into["uploads"] += row["listens"]
        into["minutes"] += (row["seconds"] or 0) / 60.0
        if row["city"]:
            into["cities"][row["city"]] = (
                into["cities"].get(row["city"], 0) + row["listens"])

    out = []
    for entry in totals.values():
        # Cities by how much came from each, so the first few named are the
        # ones a location decision would actually turn on.
        ranked = sorted(entry.pop("cities").items(), key=lambda kv: -kv[1])
        out.append({**entry,
                    "minutes": round(entry["minutes"], 1),
                    "city_count": len(ranked),
                    "cities": ", ".join(name for name, _ in ranked[:6])})
    return sorted(out, key=lambda r: (-r["minutes"], r["country"]))


def pieces() -> list[dict[str, Any]]:
    """Every work the recogniser has named here, with how long it runs.

    Length is given as a median with a range rather than a mean. A piece
    uploaded twice — once whole, once as thirty seconds somebody used to test
    the site — has no meaningful average, and the shortest/longest pair says
    so where a single number would quietly average the two into a lie.
    """
    out = []
    for row in store.pieces():
        lengths = store.piece_durations(row["piece_id"])
        out.append({
            "piece_id": row["piece_id"],
            "title": row["title"] or row["piece_id"],
            # `times` counts answers, `uploads` counts recordings: a visitor
            # who asked twice about one video is 2 and 1. Never "people" —
            # the recognition row carries no address to count them by.
            "times": row["times"],
            "uploads": row["uploads"],
            "confidence": round(row["confidence"] or 0, 1),
            "lengths": row["lengths"],
            "median_minutes": (round(statistics.median(lengths) / 60.0, 1)
                               if lengths else None),
            "shortest_minutes": (round((row["shortest"] or 0) / 60.0, 1)
                                 if lengths else None),
            "longest_minutes": (round((row["longest"] or 0) / 60.0, 1)
                                if lengths else None),
            "first_at": int((row["first_at"] or 0) * 1000),
            "last_at": int((row["last_at"] or 0) * 1000),
        })
    return out


def recent(limit: int = 200) -> list[dict[str, Any]]:
    """The individual recognitions, newest first.

    The row the other two are built from. When a piece shows an implausible
    median or an address shows more identifications than uploads, this is
    where the reason is. The job id is here for that; the address is not, and
    is reached by looking the job up while it still exists.
    """
    out = []
    for row in store.recognitions(limit):
        out.append({
            "time": int((row["at"] or 0) * 1000),
            "job": row["job_id"],
            # The place as it was recorded. No address here — this row does
            # not carry one, on purpose.
            "country": row["country"],
            "city": row["city"],
            "outcome": row["outcome"],
            "piece": row["title"] or row["piece_id"] or "",
            "confidence": round(row["confidence"], 1) if row["confidence"] is not None else None,
            "consensus": round(row["consensus"], 3) if row["consensus"] is not None else None,
            "windows": row["windows"],
            "minutes": (round(row["duration"] / 60.0, 1)
                        if row["duration"] else None),
        })
    return out


def everything() -> dict[str, Any]:
    """All four, in one read. What the endpoint and the CLI both return."""
    return {
        "geolocation": geolocation_status(),
        "visitors": visitors(),
        "countries": countries(),
        "pieces": pieces(),
        "recent": recent(),
    }
