"""Tell someone their video is ready.

One message, sent once, when a render finishes. Deliberately plain.

Nothing the uploader typed goes into the mail. The design's own handover
note asks for this and it is worth stating why: a form that puts arbitrary
text into an outgoing message turns this server into somebody else's
delivery van. The performer's name, the title panel, the file name — none
of it appears. What does appear is the piece as *our* library names it,
and a link.

Failing to send never fails a render. The video exists either way, and the
page shows the link itself; the mail is a convenience on top of that.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from . import retention
from .settings import settings

logger = logging.getLogger(__name__)


class MailError(RuntimeError):
    """The message could not be sent."""


def _link(job_id: str) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/api/jobs/{job_id}/download"


def send_ready(job_id: str, address: str, piece: str = "",
               finished: float | None = None) -> None:
    """Send one "it's ready" message. Raises MailError if it cannot."""
    if not settings.can_email:
        raise MailError(settings.why_cannot_email())
    address = (address or "").strip()
    if "@" not in address:
        raise MailError(f"Not an address: {address!r}")

    # The deadline goes in the message that carries the link, with the date
    # spelled out. "As long as the file is kept" told the reader nothing and
    # is about to become false: the link now stops working at a known hour.
    if finished:
        window = (f"You can download it until {retention.deadline_text(finished)} "
                  f"— {retention.hours_phrase()} from now. After that it moves to "
                  f"long-term storage; it is not deleted, and we can restore it "
                  f"if you ask.")
    else:
        window = (f"The link works for {retention.hours_phrase()} after the "
                  f"video is made.")

    # `piece` comes from our own library, never from the uploader.
    named = f" of {piece}" if piece else ""
    message = EmailMessage()
    message["Subject"] = "Your score video is ready"
    message["From"] = settings.smtp_from
    message["To"] = address
    message.set_content(
        f"Your video{named} has finished rendering.\n\n"
        f"{_link(job_id)}\n\n"
        f"{window}\n\n"
        f"— Weefeen\n"
    )

    try:
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
        with server:
            if settings.smtp_starttls and not settings.smtp_ssl:
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc

    logger.info("told %s that job %s is ready", address, job_id)
