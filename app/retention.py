"""How long a finished video stays downloadable, and what to say when it stops.

The link in the email works for a fixed window — 48 hours by default — and
then the video moves to cold storage. It is not deleted; it is archived, and
that difference is the whole of what the expired page has to communicate. A
404 says "we lost it". A dead link says nothing at all. Neither is true, and
either would make someone think their video was thrown away.

Two rules follow from that, and both are about honesty rather than mechanism:

The deadline is stated before it matters. The mail says the date and time the
link stops working, in the message that carries the link, not in terms of
service nobody reads.

The expiry is decided here, not by the storage lifecycle rule. Those run in
whole days and asynchronously, so a rule aimed at the same instant would
sometimes fire first, and a link we still called valid would point at an
object that takes hours to retrieve. Ours fires at the stated hour; the
transition is set a day behind it. The overlap costs almost nothing and it
guarantees that a link which works points at something that is actually
there.
"""

from __future__ import annotations

import datetime as dt

from .settings import settings


def expires_at(finished: float) -> dt.datetime:
    """When the link stops working, as an aware UTC datetime."""
    ended = dt.datetime.fromtimestamp(finished, tz=dt.timezone.utc)
    return ended + dt.timedelta(hours=settings.retention_hot_hours)


def is_live(finished: float | None) -> bool:
    """Whether the video can still be downloaded."""
    if not finished:
        return False
    return dt.datetime.now(tz=dt.timezone.utc) < expires_at(finished)


def seconds_left(finished: float | None) -> int:
    if not finished:
        return 0
    delta = expires_at(finished) - dt.datetime.now(tz=dt.timezone.utc)
    return max(0, int(delta.total_seconds()))


def deadline_text(finished: float) -> str:
    """The deadline as a person would write it: 'Thursday 11 September, 14:30 UTC'.

    UTC named explicitly because we do not know where the reader is, and a
    time without a zone is a time someone will get wrong by an hour.
    """
    when = expires_at(finished)
    return when.strftime("%A %-d %B, %H:%M UTC") if _supports_dash() \
        else when.strftime("%A %d %B, %H:%M UTC")


def _supports_dash() -> bool:
    """`%-d` drops the leading zero on glibc and is rejected on Windows."""
    try:
        dt.datetime(2026, 1, 5).strftime("%-d")
        return True
    except ValueError:
        return False


def hours_phrase() -> str:
    """'48 hours', or '3 days' if someone configures a long window."""
    hours = settings.retention_hot_hours
    if hours >= 48 and hours % 24 == 0:
        days = int(hours // 24)
        return f"{days} days"
    return f"{hours:.0f} hours"


GONE = ("This link has expired. The video was kept for {window} after it was "
        "made, and has since been moved to long-term storage — it has not "
        "been deleted. If you still need it, reply to the email you were "
        "sent and we can restore it.")


def gone_message() -> str:
    return GONE.format(window=hours_phrase())
