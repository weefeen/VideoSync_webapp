"""Preparing a YouTube link: the half that does the work.

    a link  ->  VALIDATING     what YouTube says about it, before any bytes
                IDENTIFYING    which piece is being played
                SYNCHRONISING  the score against the sound
                READY          bars, timed, on /p/<id>
            or  REVIEW         the piece is known and nobody has engraved it

THIS RUNS ON A VOLUNTEER AND NEVER OPENS THE DATABASE. That is the whole
shape of the module, and it was learned the hard way: the first version
walked a performance through its states by calling `store` and
`watch.advance` directly, which passes every test on one machine and, on a
volunteer, quietly moves rows in that machine's own SQLite while production
never hears a thing. A task arrives as a `RenderTask`; everything found
leaves as events; `app.queue.watchledger` on the web box is the only thing
that writes. Same rule as `app.queue.worker`, for the same reason.

So nothing here imports `store`, `watch`, `viewer` or `sharecard` -- each of
those reaches the database. States and reasons are plain strings, and the
ledger checks every one against `watch`'s vocabulary before writing it.

WHERE IT RUNS. On a residential address: `tools/volunteer.py`, between
renders. YouTube scores datacentre ranges as bots and refuses them every
format, which is why these tasks have their own queue that only a volunteer
reads (see `transport.PREPARE_QUEUE`).

HOST FAULTS ARE NOT OUTCOMES. yt-dlp missing, cairosvg missing so the score
looks absent, too little memory free: each raises `HandBack`, the task goes
back on the queue untouched, and the performance is never written off
because the wrong machine picked it up.

THE PARKED CASE IS THE COMMON ONE. Four scores are engraved of roughly two
hundred and thirty-five. A recognised piece with no engraving is reported
as REVIEW / NOT_ENGRAVED with the edition it wants, and the audio stays on
this disk. When the score is published the web box offers the task again
with that edition named, and this finds the audio already here and only
aligns.
"""
from __future__ import annotations

import logging
import pathlib
from typing import Callable

from . import fetchaudio, identify as ident, library, sync as syncmod
from . import package as pkg
from . import pipeline, scorestore
from . import settings as settings_mod
from .settings import max_upload_minutes, settings

logger = logging.getLogger(__name__)

# The outcomes this reports. Plain strings on purpose -- see the module
# docstring -- and checked against `watch` by the ledger and by selftest, so
# a spelling that drifts from the state machine fails loudly there.
READY = "READY"
REVIEW = "REVIEW"
REJECTED = "REJECTED"
UNAVAILABLE = "UNAVAILABLE"
FAILED = "FAILED"
STAGES = ("VALIDATING", "IDENTIFYING", "SYNCHRONISING")

NOT_CHOPIN = "NOT_CHOPIN"
NOT_ENGRAVED = "NOT_ENGRAVED"
VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
PARTIAL_UNSUPPORTED = "PARTIAL_UNSUPPORTED"
SYNC_UNSUPPORTED = "SYNC_UNSUPPORTED"

#: say(event_type, stage=None, detail="", data=None) -- publishes one event.
Say = Callable[..., None]


class HandBack(RuntimeError):
    """This machine cannot do this task. The task goes back on the queue.

    A statement about the HOST, never about the video. Raising it leaves the
    performance exactly where it is for a machine that can do the work.
    """


class NotUsableHere(HandBack):
    """The score exists but this machine cannot read it.

    Three of the four packages installed on this project ship .svg bands
    only, and an interpreter without cairosvg calls every one of them
    broken. `library.find` then answers None for a score that exists, and
    reporting that as NOT_ENGRAVED would park a performance whose score is
    right there -- the first parked test did exactly that with Op.23.
    """


class TooBigHere(HandBack):
    """This recording needs more memory than this machine has free."""


def audio_root() -> pathlib.Path:
    """Where fetched sound lives on THIS machine, keyed by video id.

    Beside the uploads rather than in a job folder: the file outlives any
    one attempt. A performance parked for a month still has its audio here
    when its engraving lands, which is what makes resuming only an
    alignment.
    """
    return settings.upload_dir.parent / "youtube"


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
    # A third left for the rest of the machine: the matrix is the biggest
    # allocation, not the only one, and an align that just fits is one that
    # dies when something else opens a file.
    return settings_mod.safe_duration_minutes(free / 1024 ** 3 * 0.66)


def compute(task, say: Say) -> None:
    """Prepare one link and report what happened. Raises only `HandBack`.

    Every ordinary result -- a dead video, a recording that is not a piece
    we know, a piece nobody has engraved, a finished alignment -- is one
    `prepared` event. Nothing is thrown for those.
    """
    meta = dict(task.meta or {})
    vid = (meta.get("video_id") or "").strip()
    resume = (meta.get("resume_edition") or "").strip()
    if not vid:
        _finish(say, FAILED, "the task named no video")
        return

    # ── what YouTube says, before a single byte is downloaded ──────────
    # The stage is announced AFTER this machine has shown it can ask. Said
    # first, a host without yt-dlp moved the row to VALIDATING and then
    # handed the task back -- leaving a performance claiming to be looked
    # at by nobody.
    try:
        info = fetchaudio.probe(vid)
    except fetchaudio.FetchUnavailable as exc:
        raise HandBack(str(exc)) from exc
    except fetchaudio.VideoUnusable as exc:
        _finish(say, UNAVAILABLE, str(exc), skip_reason=VIDEO_UNAVAILABLE)
        return
    except fetchaudio.FetchError as exc:
        _finish(say, FAILED, str(exc))
        return
    found = {"title": info["title"], "uploader": info["uploader"],
             "duration": info["duration"]}
    say("prepare_stage", stage="VALIDATING", data=found)

    # THIS MACHINE'S CAP, NOT THE SITE'S. `max_upload_minutes` answers for
    # the site -- it reports the web box's measured 3.9 GB so a visitor's
    # figure does not change with whoever runs the code -- and used alone it
    # had a 64 GB desktop refuse a ten-minute performance. Too long for THIS
    # host is handed back for a bigger one; only too long for the site is a
    # verdict on the recording.
    longest = _longest_here()
    minutes = (info["duration"] or 0) / 60
    if longest and minutes > longest:
        if minutes > max_upload_minutes():
            _finish(say, UNAVAILABLE,
                    f"{minutes:.0f} minutes long; longer than anything here "
                    f"can align", skip_reason=SYNC_UNSUPPORTED, found=found)
            return
        raise TooBigHere(f"{minutes:.1f} minutes needs more memory than this "
                         f"machine has free ({longest:.1f} minutes' worth)")

    # ── the sound ──────────────────────────────────────────────────────
    try:
        media = fetchaudio.audio(vid, audio_root())
    except fetchaudio.FetchUnavailable as exc:
        raise HandBack(str(exc)) from exc
    except fetchaudio.VideoUnusable as exc:
        _finish(say, UNAVAILABLE, str(exc), skip_reason=VIDEO_UNAVAILABLE,
                found=found)
        return
    except fetchaudio.FetchError as exc:
        _finish(say, FAILED, str(exc), found=found)
        return

    # ── which piece, unless the web box already told us ────────────────
    # A RESUME NAMES ITS EDITION. The piece was identified when the link was
    # first prepared, and nothing about the recording has changed since; the
    # web box offers it again only because that edition is now published.
    # Listening a second time would spend forty seconds of GPU on an answer
    # already written on the row.
    edition = ""
    if resume:
        if library.find(resume) is not None:
            edition = resume
        elif library.unusable_here(resume):
            raise NotUsableHere(f"{resume} cannot be read on this machine: "
                                f"{library.unusable_here(resume)}")
        # Otherwise the score the web box saw is not here yet (a volunteer
        # whose library is behind): identify afresh rather than guess.

    winner = ""
    if not edition:
        say("prepare_stage", stage="IDENTIFYING", data=found)
        try:
            heard = ident.identify(media, duration=info["duration"] or None)
        except ident.IdentifyUnavailable as exc:
            raise HandBack(str(exc)) from exc
        except ident.IdentifyError as exc:
            _finish(say, FAILED, str(exc), found=found)
            return
        winner = heard.winner or ""
        if heard.outcome != ident.MATCHED:
            # Not "we failed": we listened, and it is not something we know.
            # A reason from the fixed vocabulary, so discovery stops offering
            # the same video back for ever.
            _finish(say, REJECTED, "not a piece we know",
                    skip_reason=NOT_CHOPIN, found=found)
            return

        editions = _editions(heard)
        published = [n for n in editions if library.find(n) is not None]
        if not published:
            blocked = [(n, library.unusable_here(n)) for n in editions]
            blocked = [(n, why) for n, why in blocked if why]
            if blocked:
                name, why = blocked[0]
                raise NotUsableHere(f"{name} cannot be read on this machine: "
                                    f"{why}")
            # THE COMMON CASE. Known, and nobody has engraved it. The edition
            # it WANTS travels with the verdict: it is what the web box
            # watches for, and what it names when it offers this again.
            _finish(say, REVIEW, f"{winner}: no engraving published",
                    skip_reason=NOT_ENGRAVED, found=found,
                    edition=editions[0] if editions else "", winner=winner)
            return
        edition = published[0]

    # ── the score against the sound ────────────────────────────────────
    say("prepare_stage", stage="SYNCHRONISING", data={**found, "edition": edition})
    package = _on_this_disk(edition)
    if package is None:
        _finish(say, FAILED, f"{edition} is in the catalogue but could not be "
                f"fetched from the bucket", found=found, edition=edition,
                winner=winner)
        return
    work = audio_root() / "work" / task.job_id.replace(":", "_")
    work.mkdir(parents=True, exist_ok=True)
    try:
        alignment = syncmod.align(package.root, media, work)
    except syncmod.SyncUnavailable as exc:
        raise HandBack(str(exc)) from exc
    except syncmod.PartialRecording as exc:
        _finish(say, REJECTED, str(exc), skip_reason=PARTIAL_UNSUPPORTED,
                found=found, edition=edition, winner=winner)
        return
    except syncmod.SyncError as exc:
        _finish(say, FAILED, str(exc), found=found, edition=edition,
                winner=winner)
        return

    timeline = pkg._read_measures(alignment.measures_path)       # noqa: SLF001
    if not timeline:
        _finish(say, FAILED, "the alignment produced no measures",
                found=found, edition=edition, winner=winner)
        return
    _finish(say, READY, f"{len(timeline)} bars of {edition}", found=found,
            edition=edition, winner=winner,
            timeline=[[int(m), float(t)] for m, t in timeline],
            confidence=alignment.span_ratio)


def _on_this_disk(edition: str):
    """The score package on THIS machine's disk, fetched if it is not.

    `library.find` says whether a score is PUBLISHED. Where the bucket is
    reachable it answers from the catalogue -- entries that name a package
    without holding a folder of it -- so its `root` is None and cannot be
    aligned against. The first end-to-end run failed exactly there, on the
    one link that should have worked: the volunteer runs in the interpreter
    that has a bucket client, and every engraved link would have ended
    FAILED with "'NoneType' object has no attribute 'is_dir'". An earlier
    test had passed only because it ran where no bucket client is installed
    and the library fell back to the disk.

    The render worker already solved this, and this is its answer: look on
    disk, fetch the one package from the bucket if it is missing, look
    again. EXACT NAME ONLY -- `pipeline.find_package` also accepts a
    fragment, and aligning a performance against the wrong score is worse
    than not aligning it.

    Fetched but still unreadable means this interpreter cannot load it (an
    .svg-only package without cairosvg): a host fault, handed back.
    """
    def here():
        found = pipeline.find_package(edition)
        return found if found is not None and found.name == edition else None

    package = here()
    if package is not None:
        return package
    try:
        landed = scorestore.ensure(edition)
    except Exception as exc:                                     # noqa: BLE001
        # The bucket blinked. Not the link's fault, and not final.
        raise HandBack(f"could not fetch {edition}: {exc}") from exc
    if landed is None:
        return None
    package = here()
    if package is None:
        why = (library.unusable_here(edition)
               or "it is on disk but this machine cannot load it")
        raise NotUsableHere(f"{edition}: {why}")
    return package


def _finish(say: Say, outcome: str, note: str, *, skip_reason: str = "",
            found: dict | None = None, **extra) -> None:
    """The one `prepared` event a task ends with, whatever the outcome."""
    data = {"outcome": outcome, "note": note, "skip_reason": skip_reason,
            **(found or {}), **extra}
    logger.info("prepared -> %s: %s", outcome, note)
    say("prepared", detail=note, data=data)


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
