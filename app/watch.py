"""What happens when a performance is found, and who is waiting for it.

A performance is the thing; how it reached us is provenance. The same
recording arrives as a pasted link, as a post in the Chopin group and as a
row in a search result, and that is three DISCOVERIES of one performance --
never three performances, and never three alignments. The database says so
with a unique index; this module is where the rest of that rule lives.

Three things are kept in one place on purpose:

  * the states a performance may be in, and which moves between them are
    legal. The specification asks for transitions that are "explicit,
    validated and logged" and warns against status strings being set from
    everywhere, which is what `advance` exists to prevent;

  * what each way of finding a video is worth. A person waiting outranks
    anything a collector turned up on its own, and a video already queued
    in the background is RAISED when somebody asks for it rather than
    queued a second time;

  * why we are not going to follow something, from a fixed vocabulary, so
    discovery stops offering the same unusable video back forever.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

from . import store, youtube

logger = logging.getLogger(__name__)

# ── the states, and the moves that are allowed between them ──────────────
DISCOVERED = "DISCOVERED"
VALIDATING = "VALIDATING"
IDENTIFYING = "IDENTIFYING"
NEEDS_WORK = "NEEDS_WORK_CONFIRMATION"
READY_FOR_SYNC = "READY_FOR_SYNC"
SYNCHRONISING = "SYNCHRONISING"
QC = "QC"
READY = "READY"
REVIEW = "REVIEW"
FAILED = "FAILED"
REJECTED = "REJECTED"
UNAVAILABLE = "UNAVAILABLE"

#: Where a performance may go from where it is. Anything else is a bug in
#: the caller, and is refused loudly rather than written to the row.
MOVES: dict[str, set[str]] = {
    DISCOVERED:     {VALIDATING, REJECTED, FAILED},
    VALIDATING:     {IDENTIFYING, REJECTED, UNAVAILABLE, FAILED},
    # REVIEW is reachable from here too: "we know the work and nobody has
    # engraved it" is a real outcome of identification, and it is a wait
    # rather than a refusal.
    IDENTIFYING:    {READY_FOR_SYNC, NEEDS_WORK, REVIEW, REJECTED, FAILED},
    NEEDS_WORK:     {READY_FOR_SYNC, REVIEW, REJECTED, FAILED},
    READY_FOR_SYNC: {SYNCHRONISING, REVIEW, FAILED, REJECTED},
    SYNCHRONISING:  {QC, FAILED},
    QC:             {READY, REVIEW, FAILED},
    REVIEW:         {READY, FAILED, REJECTED},
    # A performance that is ready can still lose its video, and a failure
    # can be tried again once the reason for it has changed.
    READY:          {UNAVAILABLE, REVIEW},
    FAILED:         {VALIDATING, READY_FOR_SYNC, REJECTED},
    UNAVAILABLE:    {VALIDATING, REJECTED},
    REJECTED:       set(),
}

#: A performance in one of these is finished with, for now.
SETTLED = {READY, REJECTED, FAILED, UNAVAILABLE}


class BadTransition(RuntimeError):
    """A move between states that the model does not allow."""


# ── what a way of finding something is worth ─────────────────────────────
USER_PASTE = "USER_PASTE"
USER_UPLOAD = "USER_UPLOAD"
FACEBOOK_GROUP = "FACEBOOK_GROUP"
TRUSTED_CHANNEL = "TRUSTED_CHANNEL"
YOUTUBE_SEARCH = "YOUTUBE_SEARCH"
CATALOGUE_GAP = "CATALOGUE_GAP"
ADMIN = "ADMIN"

#: Scale from the specification. The numbers are spaced so a source can be
#: added between two of them without renumbering the others.
PRIORITY = {
    USER_PASTE: 1000,
    USER_UPLOAD: 1000,
    ADMIN: 900,
    FACEBOOK_GROUP: 800,
    TRUSTED_CHANNEL: 600,
    YOUTUBE_SEARCH: 400,
    CATALOGUE_GAP: 200,
}

# ── why we would not follow something ────────────────────────────────────
NOT_CHOPIN = "NOT_CHOPIN"
MULTIPLE_WORKS = "MULTIPLE_WORKS"
PARTIAL_UNSUPPORTED = "PARTIAL_UNSUPPORTED"
SPEECH_DOMINATED = "SPEECH_DOMINATED"
VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
EMBED_UNAVAILABLE = "EMBED_UNAVAILABLE"
DUPLICATE = "DUPLICATE"
SYNC_UNSUPPORTED = "SYNC_UNSUPPORTED"
NOT_ENGRAVED = "NOT_ENGRAVED"          # we know the work; no score for it yet
OTHER = "OTHER"

SKIP_REASONS = {
    NOT_CHOPIN, MULTIPLE_WORKS, PARTIAL_UNSUPPORTED, SPEECH_DOMINATED,
    VIDEO_UNAVAILABLE, EMBED_UNAVAILABLE, DUPLICATE, SYNC_UNSUPPORTED,
    NOT_ENGRAVED, OTHER,
}


def advance(performance_id: str, to: str, *, reason: str | None = None,
            **fields: Any) -> sqlite3.Row:
    """Move a performance to another state, or refuse.

    The single door. Every state change goes through here so that an
    illegal move is impossible rather than merely unlikely, and so that
    there is one place that logs what happened.
    """
    row = store.performance_by_id(performance_id)
    if row is None:
        raise BadTransition(f"no performance {performance_id}")
    was = row["state"]
    if to == was:
        return row
    if to not in MOVES.get(was, set()):
        raise BadTransition(f"{was} -> {to} is not a move this model allows")
    if reason is not None and reason not in SKIP_REASONS:
        raise ValueError(f"{reason!r} is not one of the recorded reasons")

    if reason is not None:
        fields["skip_reason"] = reason
    store.set_performance(performance_id, state=to, **fields)
    logger.info("performance %s: %s -> %s%s", row["public_id"], was, to,
                f" ({reason})" if reason else "")
    return store.performance_by_id(performance_id)


def resolve(text: str, *, source_type: str = USER_PASTE,
            reference: str = "") -> tuple[sqlite3.Row, bool]:
    """Find or start the performance a link refers to.

    Returns the performance and whether this call created it.

    Every way in lands here -- a visitor pasting a link, the group
    collector, a trusted channel, a search -- and the three cases it has to
    tell apart are:

      * we have this video already: hand it back, record that it was found
        again, and raise its priority if this finding is worth more than
        whatever queued it. A background video somebody now wants moves up
        the queue instead of being queued twice;
      * we have never seen it: create the performance, the media source and
        the first discovery together;
      * two callers arrive with it at once: the unique index refuses the
        second write and we re-read rather than making a twin. The guard is
        the database, not the order the code happens to run in.
    """
    vid = youtube.video_id(text)                 # raises if it is not one
    if source_type not in PRIORITY:
        raise ValueError(f"{source_type!r} is not a way of finding things")
    weight = PRIORITY[source_type]
    url = youtube.canonical_url(vid)

    existing = store.media_by_external("youtube", vid)
    if existing is not None:
        performance = store.performance_by_id(existing["performance_id"])
        if performance is not None:
            _record(existing["id"], source_type, reference, url, weight)
            _raise_priority(performance, weight)
            return store.performance_by_id(performance["id"]), False
        # A media row whose performance vanished is not something we can
        # repair by guessing; treat it as unseen and let the insert below
        # fail loudly on the unique index if it is still really there.
        logger.warning("media %s has no performance", existing["id"])

    performance = store.new_performance(DISCOVERED, priority=weight)
    try:
        media_id = store.attach_media(
            performance["id"], "youtube", external_id=vid, url=url)
    except sqlite3.IntegrityError:
        # Somebody else got there between our look and our write. Theirs is
        # the one that exists; ours is an empty performance to let go of.
        store.drop_performance(performance["id"])
        other = store.media_by_external("youtube", vid)
        if other is None:                        # cannot happen; say so if it does
            raise
        performance = store.performance_by_id(other["performance_id"])
        _record(other["id"], source_type, reference, url, weight)
        _raise_priority(performance, weight)
        return store.performance_by_id(performance["id"]), False

    _record(media_id, source_type, reference, url, weight)
    return store.performance_by_id(performance["id"]), True


def _record(media_id: str, source_type: str, reference: str,
            url: str, weight: int) -> None:
    store.add_discovery(media_id, source_type=source_type,
                        reference=reference, url=url, priority=weight)


def _raise_priority(performance: sqlite3.Row, weight: int) -> None:
    """Demand only ever goes up.

    A video queued quietly by a collector and then asked for by a person
    must overtake the rest of the background work -- but a later background
    sighting of a video somebody is waiting for must not push it back down.
    """
    if weight > (performance["priority"] or 0):
        store.set_performance(performance["id"], priority=weight)
        logger.info("performance %s raised to priority %d",
                    performance["public_id"], weight)


def skip(performance_id: str, reason: str, *, note: str = "") -> sqlite3.Row:
    """Record that we will not follow this one, and why.

    A skipped candidate stays in the table on purpose: without it the
    collectors offer the same unusable video back every time they look.
    """
    row = store.performance_by_id(performance_id)
    if row is None:
        raise BadTransition(f"no performance {performance_id}")
    if reason in (VIDEO_UNAVAILABLE, EMBED_UNAVAILABLE):
        to = UNAVAILABLE
    elif reason == NOT_ENGRAVED:
        # Not a rejection of the video: the work is real and the performance
        # waits for somebody to engrave the score. It stays so that the
        # request counts as a vote for which score is engraved next.
        to = REVIEW
    else:
        to = REJECTED
    return advance(performance_id, to, reason=reason, error=note or None)
