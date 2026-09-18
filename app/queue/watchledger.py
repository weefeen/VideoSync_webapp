"""Preparing a YouTube link: the half that writes it down.

`app.prepare` runs on a volunteer and never opens the database; this runs on
the web box and is the only thing that does. It is to performances what
`app.queue.ledger` is to jobs:

    offer(...)      publish a `prepare` task for a performance
    apply(event)    write what a volunteer reported
    sweep(bus)      re-offer what was lost, resume what a score now unblocks

EVERY STATE IS WALKED, NEVER SET. `watch.advance` refuses any move the model
does not allow, so a result is applied by stepping along legal moves -- and
where the model has no move for what happened, by the nearest honest one,
with the reason kept on the row. Two of those were found by reading MOVES
rather than by running anything, and both were live bugs in the first
version of `prepare`, which set states directly:

  * REVIEW -> READY_FOR_SYNC is not a move. A resumed performance goes
    REVIEW -> READY in one step, with its bars written first.
  * SYNCHRONISING -> REJECTED is not a move. A partial recording discovered
    during alignment is FAILED with PARTIAL_UNSUPPORTED on the row, which
    says the same thing the model would not let REJECTED say from there.

EVERYTHING HERE IS IDEMPOTENT, because the queue delivers at least once and
the sweep offers duplicates on purpose. A second READY for a performance
that is READY writes nothing.
"""
from __future__ import annotations

import logging
import threading
import time

from .. import library, sharecard, store, viewer, watch
from .messages import Event, RenderTask
from .transport import PREPARE_KIND, TransportError, transport

logger = logging.getLogger(__name__)

PREFIX = "perf:"

# A performance offered and not heard from in this long is offered again,
# but only when the queue holds fewer tasks than there are performances
# waiting -- otherwise the tasks are simply sitting there, a volunteer is
# off, and a duplicate would only mean doing the same work twice later.
DISCOVERED_GRACE = 3600
IN_PROGRESS_GRACE = 2 * 3600
# A parked performance whose score is published is offered at once when the
# score first appears (see `_published_before`), and after that no more
# often than this, in case that offer was lost.
RESUME_BACKSTOP = 6 * 3600

WAITING = (watch.DISCOVERED, watch.VALIDATING, watch.IDENTIFYING,
           watch.READY_FOR_SYNC, watch.SYNCHRONISING)

# The forward path a performance walks while it is being prepared.
PATH = [watch.DISCOVERED, watch.VALIDATING, watch.IDENTIFYING,
        watch.READY_FOR_SYNC, watch.SYNCHRONISING, watch.QC, watch.READY]


def is_mine(event) -> bool:
    return str(getattr(event, "job_id", "")).startswith(PREFIX)


# ── offering ──────────────────────────────────────────────────────────────
def offer(performance_id: str, *, resume_edition: str = "") -> bool:
    """Put a `prepare` task on the queue for one performance. True if sent.

    Never raises: a lost offer is recovered by the sweep, and the caller --
    usually a visitor's request -- must not fail because the broker blinked.
    """
    row = store.performance_by_id(performance_id)
    if row is None:
        return False
    vid = _video_id(performance_id)
    if not vid:
        logger.warning("performance %s has no YouTube video to prepare",
                       performance_id)
        return False
    meta = {"video_id": vid, "public_id": row["public_id"],
            "resume_edition": resume_edition}
    if row["segment_start"] is not None and row["segment_end"] is not None:
        # One piece of the video: the volunteer hears and aligns only this
        # stretch, and reports bar times in the video's own seconds.
        meta["segment"] = [float(row["segment_start"]), float(row["segment_end"])]
    task = RenderTask(job_id=PREFIX + performance_id, upload="",
                      package=resume_edition, kind=PREPARE_KIND,
                      meta=meta, queued_at=time.time())
    try:
        transport().publish_task(task)
    except TransportError:
        logger.warning("could not offer performance %s; the sweep will",
                       performance_id, exc_info=True)
        return False
    # `updated` is the offer stamp. It is what the sweep's grace periods are
    # measured from, and no column had to be added to have one.
    store.set_performance(performance_id)
    logger.info("offered performance %s (%s)%s", row["public_id"], vid,
                f" to resume as {resume_edition}" if resume_edition else "")
    return True


def _video_id(performance_id: str) -> str:
    for media in store.media_for(performance_id):
        if media["provider"] == "youtube" and media["external_id"]:
            return media["external_id"]
    return ""


# ── applying ──────────────────────────────────────────────────────────────
def apply(event: Event) -> bool:
    """Write one report from a volunteer. True if a row changed."""
    performance_id = event.job_id[len(PREFIX):]
    row = store.performance_by_id(performance_id)
    if row is None:
        logger.warning("%s for a performance that no longer exists: %s",
                       event.type, performance_id)
        return False
    data = dict(getattr(event, "data", None) or {})
    _remember_what_youtube_said(row, data)

    if event.type == "prepare_stage":
        return _stage(row, event.stage or "")
    if event.type == "prepared":
        return _prepared(row, data)
    if event.type in ("heartbeat", "pong"):
        return False
    logger.warning("unknown event %r for performance %s", event.type,
                   performance_id)
    return False


def _remember_what_youtube_said(row, data: dict) -> None:
    """Title and pianist, the first time they are known.

    Only into EMPTY fields. A title an operator corrected, or a pianist
    `make_performance_cards --meta` already wrote, is not overwritten by
    whatever the next preparation of the same video happens to read.
    """
    fields = {}
    title = (data.get("title") or "").strip()
    if title and not (row["title"] or "").strip():
        fields["title"] = title
    if not (row["performer"] or "").strip():
        # The PIANIST, not the channel: "Chopin Institute" under a portrait
        # of Kate Liu is the wrong name. The title is asked first.
        name = sharecard._pianist(title) or (data.get("uploader") or "").strip()  # noqa: SLF001
        if name:
            fields["performer"] = name
    if fields:
        store.set_performance(row["id"], **fields)


def _stage(row, stage: str) -> bool:
    """Move forward to a stage the volunteer has reached, if the model allows.

    Only FORWARD. A duplicate task re-prepares a performance that is already
    further on, and its early stages must not drag the row back.
    """
    if stage not in PATH:
        logger.warning("unknown stage %r for performance %s", stage, row["id"])
        return False
    state = row["state"]
    # A failure that is being tried again starts over; the model allows
    # FAILED and UNAVAILABLE back to VALIDATING for exactly this.
    if state in (watch.FAILED, watch.UNAVAILABLE) and stage == watch.VALIDATING:
        return _try(row["id"], watch.VALIDATING)
    if state not in PATH:
        return False           # REVIEW, REJECTED, ...: a stage says nothing
    return _walk(row["id"], stage)


def _prepared(row, data: dict) -> bool:
    outcome = (data.get("outcome") or "").strip()
    note = (data.get("note") or "").strip()
    reason = (data.get("skip_reason") or "").strip() or None
    if reason is not None and reason not in watch.SKIP_REASONS:
        logger.warning("performance %s: reason %r is not in the vocabulary; "
                       "recorded as OTHER", row["id"], reason)
        reason = watch.OTHER
    performance_id = row["id"]

    if outcome == watch.READY:
        return _ready(row, data)

    if outcome == SEGMENTS:
        return _segments(row, data)

    if outcome == watch.REVIEW:
        edition = (data.get("edition") or "").strip()
        if edition:
            store.set_performance(performance_id, edition=edition)
        return _settle(performance_id, watch.REVIEW, reason=reason, note="")

    if outcome in (watch.REJECTED, watch.UNAVAILABLE, watch.FAILED):
        return _settle(performance_id, outcome, reason=reason, note=note)

    logger.warning("performance %s: unknown outcome %r", performance_id, outcome)
    return False


SEGMENTS = "SEGMENTS"          # prepare.SEGMENTS: several pieces heard


def _segments(row, data: dict) -> bool:
    """A video of several pieces: one performance per piece.

    The volunteer heard the whole recording and reports where each work
    is. This row becomes the FIRST piece we know (its bounds recorded);
    every further known piece becomes a sibling performance -- same video
    through parent_id, its own bounds, its own page -- and each is offered
    to resume at the alignment of its stretch, or parked as not engraved
    exactly like a single recording would be. Spans the recogniser could
    not name are logged, not stored: there is nothing to follow there.
    """
    found = [s for s in (data.get("segments") or [])
             if s.get("outcome") in ("matched", "uncertain") and s.get("score_names")]
    unknown = [s for s in (data.get("segments") or []) if s not in found]
    for s in unknown:
        logger.info("performance %s: %s-%s heard as %s, not a piece we know",
                    row["public_id"], _mmss(s.get("start")), _mmss(s.get("end")),
                    s.get("winner") or "nothing")
    if not found:
        return _settle(row["id"], watch.REJECTED, reason=watch.NOT_CHOPIN,
                       note=f"{len(unknown)} piece(s) heard, none we know")

    changed = False
    for i, seg in enumerate(found):
        start, end = float(seg["start"]), float(seg["end"])
        names = list(seg.get("score_names") or [])
        published = next((n for n in names if library.find(n) is not None), "")
        edition = published or names[0]
        if i == 0:
            perf_id = row["id"]
            store.set_performance(perf_id, edition=edition, error=None,
                                  skip_reason=None, segment_start=start,
                                  segment_end=end)
        else:
            sibling = store.new_performance(
                watch.DISCOVERED, edition=edition, title=row["title"] or "",
                performer=row["performer"] or "", priority=row["priority"] or 0,
                parent_id=row["id"], segment=(start, end))
            perf_id = sibling["id"]
            logger.info("performance %s: piece %d/%d at %s-%s is now %s",
                        row["public_id"], i + 1, len(found), _mmss(start),
                        _mmss(end), sibling["public_id"])
        if published:
            # Back to the volunteer for the alignment of this stretch only;
            # its stage reports walk the row forward from wherever it is.
            changed |= offer(perf_id, resume_edition=published)
        else:
            store.set_performance(perf_id, edition=edition)
            changed |= _settle(perf_id, watch.REVIEW, reason=watch.NOT_ENGRAVED, note="")
    return changed


def _mmss(seconds) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    return f"{s // 60}:{s % 60:02d}"


def _ready(row, data: dict) -> bool:
    performance_id = row["id"]
    if row["state"] == watch.READY:
        logger.info("performance %s is already READY; duplicate ignored",
                    row["public_id"])
        return False
    edition = (data.get("edition") or "").strip()
    timeline = [(int(m), float(t)) for m, t in (data.get("timeline") or [])]
    if not edition or not timeline:
        return _settle(performance_id, watch.FAILED, reason=None,
                       note="reported READY with no edition or no bars")

    # The bars FIRST, then the state: a performance must never be READY with
    # nothing to follow, not even for the moment between two writes.
    store.put_sync(performance_id, edition, state="READY", method="align",
                   confidence=data.get("confidence"), timeline=timeline)
    store.set_performance(performance_id, edition=edition, error=None,
                          skip_reason=None)
    if row["state"] == watch.REVIEW:
        changed = _try(performance_id, watch.READY)
    else:
        changed = _walk(performance_id, watch.READY)

    # Measured and drawn now rather than on somebody's page load or a
    # crawler's fetch -- and off this thread, because every other volunteer
    # report waits behind it and drawing a card is seconds.
    public_id = row["public_id"]
    threading.Thread(target=_finish_ready, args=(edition, public_id),
                     name=f"card-{public_id}", daemon=True).start()
    logger.info("performance %s READY: %d bars of %s", public_id,
                len(timeline), edition)
    return changed


def _finish_ready(edition: str, public_id: str) -> None:
    try:
        viewer.geometry(edition)
    except Exception:                                  # noqa: BLE001
        logger.warning("could not pre-measure %s", edition, exc_info=True)
    try:
        current = store.performance(public_id)
        if current is not None:
            sharecard.build(viewer.payload(current), public_id)
    except Exception:                                  # noqa: BLE001
        # The card route falls back to YouTube's own still, so this is a
        # plainer card rather than no card.
        logger.warning("could not draw the share card for %s", public_id,
                       exc_info=True)


def _walk(performance_id: str, target: str) -> bool:
    """Step forward along PATH to `target`. True if the state changed."""
    row = store.performance_by_id(performance_id)
    if row is None or row["state"] not in PATH:
        return False
    here, there = PATH.index(row["state"]), PATH.index(target)
    changed = False
    for step in PATH[here + 1:there + 1]:
        if not _try(performance_id, step):
            break
        changed = True
    return changed


def _settle(performance_id: str, state: str, *, reason: str | None,
            note: str) -> bool:
    """Move to a final state, or the nearest one the model allows from here."""
    extra = {"error": note} if note else {}
    if _try(performance_id, state, reason=reason, **extra):
        return True
    # No such move from where the row is (SYNCHRONISING -> REJECTED, or
    # REVIEW -> UNAVAILABLE). FAILED is reachable from every working state,
    # and the reason on the row still says what really happened.
    if state != watch.FAILED and _try(performance_id, watch.FAILED,
                                      reason=reason, **extra):
        logger.info("performance %s: %s is not a move from here; recorded as "
                    "FAILED with reason %s", performance_id, state, reason)
        return True
    return False


def _try(performance_id: str, state: str, **fields) -> bool:
    try:
        watch.advance(performance_id, state, **fields)
        return True
    except (watch.BadTransition, ValueError) as exc:
        logger.info("performance %s: %s", performance_id, exc)
        return False


# ── the sweep ─────────────────────────────────────────────────────────────
_published_before: "set[str] | None" = None


def sweep(bus) -> int:
    """One pass. Returns how many performances it offered."""
    global _published_before
    offered = 0
    now = time.time()

    # PARKED PERFORMANCES WHOSE SCORE HAS ARRIVED. Offered the moment an
    # edition first appears in the catalogue -- which is what "as soon as the
    # score is there, it continues" means -- and after that only as a
    # backstop. The first pass after a restart sees every edition as new and
    # offers every parked performance whose score exists, which is exactly
    # what should happen after a restart anyway.
    try:
        published = {p.name for p in library.packages()}
    except Exception:                                  # noqa: BLE001
        logger.warning("could not read the catalogue; skipping resumes",
                       exc_info=True)
        published = None
    if published is not None:
        arrived = published - (_published_before or set())
        _published_before = published
        parked = store.query(
            "SELECT * FROM performances WHERE state = ? AND skip_reason = ?"
            " AND edition IS NOT NULL AND edition != ''",
            (watch.REVIEW, watch.NOT_ENGRAVED))
        for row in parked:
            edition = row["edition"]
            if edition not in published:
                continue
            if edition in arrived or now - (row["updated"] or 0) > RESUME_BACKSTOP:
                logger.info("the score %s is published; resuming %s",
                            edition, row["public_id"])
                offered += bool(offer(row["id"], resume_edition=edition))

    # LOST OFFERS. Only when the queue plainly holds fewer tasks than there
    # are performances waiting; a volunteer that is simply off leaves the
    # tasks sitting there, and duplicates would be work done twice later.
    depth = bus.prepare_depth() if hasattr(bus, "prepare_depth") else None
    marks = ",".join("?" * len(WAITING))
    waiting = store.query(
        f"SELECT * FROM performances WHERE state IN ({marks})", WAITING)
    waiting = [r for r in waiting if _video_id(r["id"])]
    if depth is not None and waiting and depth < len(waiting):
        for row in waiting:
            grace = (DISCOVERED_GRACE if row["state"] == watch.DISCOVERED
                     else IN_PROGRESS_GRACE)
            if now - (row["updated"] or 0) > grace:
                logger.warning("performance %s has waited %.0f min in %s with "
                               "the queue short; offering it again",
                               row["public_id"], (now - row["updated"]) / 60,
                               row["state"])
                offered += bool(offer(row["id"]))
    return offered
