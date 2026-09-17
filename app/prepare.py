"""Taking a discovered performance all the way to something watchable.

    a link  ->  DISCOVERED
                VALIDATING     what YouTube says about it, before any bytes
                IDENTIFYING    which piece is being played
                READY_FOR_SYNC / SYNCHRONISING   the score against the sound
                QC / READY     bars, timed, on /p/<id>

Almost none of this is new. `identify` and `align` already do the work and
already take a plain local file; `watch.advance` already polices the moves
between states; `store.put_sync` already records bars. What was missing was
the thing that walks a performance through them, and the audio to walk it
with. This is that walk, and nothing else belongs in it.

WHERE IT RUNS. On a machine with a residential address -- in practice
`tools/volunteer.py` on a desktop, the same machine that already renders so
a Linode node is not rented. YouTube scores datacentre ranges as bots and
refuses them every format, so the web box must never be the one asking.
That is a deployment fact, not a preference, and `worker.py` enforces it by
refusing a `prepare` task unless `place()` says `local`.

THE PARKED CASE IS THE COMMON ONE AND IS NOT A FAILURE. Four scores are
engraved of roughly two hundred and thirty-five, so most recognised pieces
have no score to align against yet. Such a performance goes to REVIEW with
its audio KEPT, and the work it wants is recorded where an operator can see
it. When that score is published, `resume` finishes the job from the file
already on disk -- no second download, and nobody has to remember.
"""
from __future__ import annotations

import logging
import pathlib

from . import fetchaudio, identify as ident, library, store, sync as syncmod
from . import package as pkg
from . import viewer, watch, youtube
from . import settings as settings_mod
from .settings import max_upload_minutes, settings

logger = logging.getLogger(__name__)


class NotPreparable(RuntimeError):
    """This performance is not something `prepare` can act on."""


class TooBigHere(RuntimeError):
    """This recording needs more memory than this machine has free.

    NOT a verdict on the video. It is a statement about the host, and the
    performance is left exactly where it was so a machine with more room
    takes it. `worker.py` requeues the task rather than failing it, the same
    way `tools/volunteer.py` already hands back a render it cannot finish.
    """


def _longest_here() -> float:
    """The longest recording THIS machine could align right now, in minutes.

    Free memory, not total: a desktop with 64 GB may have 15 free while it
    is being used, and the DTW matrix grows with the square of the duration.
    Zero when the machine will not say, which disables the gate rather than
    refusing everything.
    """
    free = settings_mod.free_memory_bytes()
    if free <= 0:
        return 0.0
    # A third is left for the rest of the machine: the matrix is the biggest
    # allocation but it is not the only one, and an align that just fits is
    # an align that dies when something else opens a file.
    return settings_mod.safe_duration_minutes(free / 1024 ** 3 * 0.66)


def audio_root() -> pathlib.Path:
    """Where fetched sound lives, keyed by video id.

    Beside the uploads rather than in a job folder: this file outlives any
    one attempt at preparing it. A performance parked for a month waiting
    for an engraving still has its audio when the engraving lands, and that
    is the whole reason the parked case is cheap.
    """
    return settings.upload_dir.parent / "youtube"


def prepare(performance_id: str, *, force: bool = False) -> str:
    """Walk one performance as far as it can go. Returns the state it reached.

    Never raises for an ordinary bad outcome -- a dead video, an
    unrecognisable recording, a piece nobody has engraved are all answers,
    and each is written to the performance rather than thrown. It raises
    only when asked to do something it cannot: no such performance, or one
    whose media is not a YouTube video.
    """
    row = store.performance_by_id(performance_id)
    if row is None:
        raise NotPreparable(f"no performance {performance_id}")

    media = (store.media_for(performance_id) or [None])[0]
    if media is None or media["provider"] != "youtube":
        raise NotPreparable(
            f"{performance_id} is not a YouTube performance; "
            "prepare has nothing to fetch")
    video_id = media["external_id"]

    if row["state"] == watch.READY and not force:
        logger.info("%s is already READY", performance_id)
        return watch.READY

    # ── what YouTube says, before a single byte is downloaded ──────────
    if row["state"] == watch.DISCOVERED:
        watch.advance(performance_id, watch.VALIDATING)
    try:
        meta = fetchaudio.probe(video_id)
    except fetchaudio.FetchUnavailable:
        # This machine cannot fetch at all. That is OUR fault, not the
        # video's, so the performance is left exactly where it is for a
        # machine that can -- marking it FAILED would bury a good video
        # because the wrong host picked up the task.
        raise
    except fetchaudio.VideoUnusable as exc:
        return _stop(performance_id, watch.UNAVAILABLE, str(exc))
    except fetchaudio.FetchError as exc:
        return _stop(performance_id, watch.FAILED, str(exc))

    if meta["title"]:
        store.set_performance(performance_id, title=meta["title"],
                              performer=meta["uploader"])

    # Asked BEFORE a byte is downloaded: refusing a two-hour recital costs
    # one metadata request here, and the whole download anywhere later.
    #
    # THIS MACHINE'S CAP, NOT THE SITE'S. The aligner allocates the whole
    # N x M matrix, so memory grows with the SQUARE of the duration and the
    # only question that matters is what the box doing the aligning can hold.
    #
    # `max_upload_minutes` is deliberately NOT that number. It answers for
    # the SITE -- `renderer_memory_gb` returns the web box's measured 3.9 GB
    # rather than reading /proc, precisely so the figure shown to a visitor
    # does not change with whoever is running the code. Using it here would
    # have a 64 GB desktop refuse a ten-minute performance on a 3.9 GB box's
    # behalf: on this machine it says 7.0, and the Op.39 already published on
    # the site is 7.07.
    #
    # Too long FOR THIS MACHINE is also not the same as too long for us, so a
    # recording over the local ceiling but under the site's is left alone for
    # a bigger machine rather than refused.
    longest_min = _longest_here()
    minutes = meta["duration"] / 60
    if longest_min and minutes > longest_min:
        if minutes > max_upload_minutes():
            return _stop(performance_id, watch.UNAVAILABLE,
                         f"{minutes:.0f} minutes long; longer than anything "
                         f"here can align",
                         skip_reason=watch.SYNC_UNSUPPORTED)
        raise TooBigHere(
            f"{minutes:.1f} minutes needs more memory than this machine has "
            f"free ({longest_min:.1f} minutes' worth); another will take it")

    # ── the sound ──────────────────────────────────────────────────────
    try:
        media_path = fetchaudio.audio(video_id, audio_root())
    except fetchaudio.FetchUnavailable:
        raise
    except fetchaudio.VideoUnusable as exc:
        return _stop(performance_id, watch.UNAVAILABLE, str(exc))
    except fetchaudio.FetchError as exc:
        return _stop(performance_id, watch.FAILED, str(exc))

    # ── which piece ────────────────────────────────────────────────────
    if store.performance_by_id(performance_id)["state"] == watch.VALIDATING:
        watch.advance(performance_id, watch.IDENTIFYING)
    try:
        heard = ident.identify(media_path, duration=meta["duration"] or None)
    except ident.IdentifyUnavailable:
        raise                      # a deployment fault again; leave it be
    except ident.IdentifyError as exc:
        return _stop(performance_id, watch.FAILED, str(exc))

    if heard.outcome != ident.MATCHED:
        # Not "we failed" -- we listened and it is not something we know.
        # REJECTED with a reason from the fixed vocabulary, so discovery
        # stops offering the same video back forever.
        return _stop(performance_id, watch.REJECTED, "not a piece we know",
                     skip_reason=watch.NOT_CHOPIN)

    editions = _editions(heard)
    published = [name for name in editions if library.find(name) is not None]
    if not published:
        # THE COMMON CASE. The piece is known and nobody has engraved it.
        # Parked, audio kept, and recorded where it will be seen.
        # NOT `store.put_recognition`: that table is keyed by job id and
        # belongs to the upload path. A performance records what it wants on
        # its own row.
        #
        # The edition it WANTS is written even though no such package is
        # published here. That is what `resume` reads when the engraving
        # lands, and it is what makes "has this score arrived yet" a
        # question about a folder name rather than one that needs the
        # recording listened to a second time.
        if editions:
            store.set_performance(performance_id, edition=editions[0])
        return _stop(performance_id, watch.REVIEW,
                     f"{heard.winner}: no engraving published",
                     skip_reason=watch.NOT_ENGRAVED)

    edition = published[0]
    return _align(performance_id, edition, media_path)


def resume(performance_id: str) -> str:
    """Finish a parked performance now that its score exists.

    The audio is already on this disk, so this is the alignment and nothing
    else. Safe to call on one that is not parked: it says so and stops.
    """
    row = store.performance_by_id(performance_id)
    if row is None:
        raise NotPreparable(f"no performance {performance_id}")
    if row["state"] != watch.REVIEW:
        logger.info("%s is %s, not parked", performance_id, row["state"])
        return row["state"]

    media = (store.media_for(performance_id) or [None])[0]
    if media is None or media["provider"] != "youtube":
        raise NotPreparable(f"{performance_id} has no YouTube media")

    audio = fetchaudio._already(audio_root(), media["external_id"])  # noqa: SLF001
    if audio is None:
        # The file was reclaimed. Not an error: prepare will fetch it again.
        logger.info("%s: the audio is gone; preparing from the start",
                    performance_id)
        return prepare(performance_id, force=True)

    edition = _parked_edition(performance_id)
    if edition is None:
        return prepare(performance_id, force=True)
    return _align(performance_id, edition, audio)


def _align(performance_id: str, edition: str, media_path: pathlib.Path) -> str:
    """The score against the sound, and the bars that come out of it."""
    package = library.find(edition)
    if package is None:
        return _stop(performance_id, watch.REVIEW,
                     f"{edition} is not published here")

    state = store.performance_by_id(performance_id)["state"]
    if state in (watch.IDENTIFYING, watch.REVIEW):
        watch.advance(performance_id, watch.READY_FOR_SYNC)
    watch.advance(performance_id, watch.SYNCHRONISING)

    job_dir = audio_root() / "work" / performance_id
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        alignment = syncmod.align(package.root, media_path, job_dir)
    except syncmod.SyncUnavailable:
        raise                      # deployment fault; leave the performance
    except syncmod.PartialRecording as exc:
        return _stop(performance_id, watch.REJECTED, str(exc),
                     skip_reason=watch.PARTIAL_UNSUPPORTED)
    except syncmod.SyncError as exc:
        return _stop(performance_id, watch.FAILED, str(exc))

    timeline = pkg._read_measures(alignment.measures_path)       # noqa: SLF001
    if not timeline:
        return _stop(performance_id, watch.FAILED,
                     "the alignment produced no measures")

    store.put_sync(performance_id, edition, state="READY", method="align",
                   confidence=alignment.span_ratio, timeline=timeline)
    store.set_performance(performance_id, edition=edition)
    watch.advance(performance_id, watch.QC)
    watch.advance(performance_id, watch.READY)

    # Measure the engraving now rather than on somebody's page load: it is
    # a third of a megabyte of SVG parsed once, and the answer never changes.
    try:
        viewer.geometry(edition)
    except Exception:                                            # noqa: BLE001
        logger.warning("%s: could not pre-measure %s", performance_id, edition,
                       exc_info=True)

    logger.info("%s: READY, %d bars of %s", performance_id, len(timeline), edition)
    return watch.READY


def _editions(heard) -> list[str]:
    """Every edition the recogniser offered, best candidate first."""
    names: list[str] = []
    for candidate in heard.candidates:
        if heard.winner and candidate.piece_id != heard.winner:
            continue
        for name in candidate.score_names:
            if name not in names:
                names.append(name)
    if not names:
        for candidate in heard.candidates:
            for name in candidate.score_names:
                if name not in names:
                    names.append(name)
    return names


def _parked_edition(performance_id: str) -> str | None:
    """Which edition a parked performance was waiting for, if it is here now."""
    row = store.performance_by_id(performance_id)
    if row and row["edition"] and library.find(row["edition"]) is not None:
        return row["edition"]
    return None


def _stop(performance_id: str, state: str, reason: str,
          *, skip_reason: str | None = None) -> str:
    """Record an outcome and return the state reached.

    Everything that is not READY comes through here, so there is one place
    that writes a reason and one place to read when asking why a link did
    nothing.
    """
    extra = {}
    if skip_reason:
        extra["skip_reason"] = skip_reason
    if state in (watch.FAILED,):
        extra["error"] = reason
    try:
        watch.advance(performance_id, state, reason=reason, **extra)
    except Exception:                                            # noqa: BLE001
        # An illegal move is a bug in this module, not a reason to lose the
        # log line that says what happened.
        logger.warning("%s: could not move to %s (%s)", performance_id, state,
                       reason, exc_info=True)
    logger.info("%s -> %s: %s", performance_id, state, reason)
    return state
