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
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import getaddresses

from . import retention
from .settings import settings

logger = logging.getLogger(__name__)


class MailError(RuntimeError):
    """The message could not be sent."""


def _link(job_id: str) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/api/jobs/{job_id}/download"


# Deliberately strict rather than clever. Anything unusual but valid gets
# refused, which costs one person an email; anything permissive gets this
# server used to deliver mail to strangers, which costs the domain its
# reputation. RFC 5321 caps the whole address at 254 characters.
_ADDRESS = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}"
                      r"@[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def one_address(raw: str) -> str:
    """Exactly one deliverable address, or raise.

    `"@" in address` was not validation, and the gap it left was not
    theoretical: "a@x.com, b@y.com" satisfied it, `send_message` reads its
    recipients from the To header, and so one submission delivered to every
    address in the string — counted once against the per-address cap. That
    is a mail relay with our domain on the envelope.

    So the string must parse to a single address, and it must look like an
    address rather than merely contain an @.
    """
    raw = (raw or "").strip()
    if not raw or len(raw) > 254:
        raise MailError("That is not an address we can send to.")
    # Rejected before parsing: a header is one line, and a comma or a
    # semicolon is how a second recipient gets in.
    if any(c in raw for c in ",;\r\n\t<>\"") :
        raise MailError("That is not an address we can send to.")
    found = [addr for _, addr in getaddresses([raw]) if addr]
    if len(found) != 1 or found[0] != raw:
        raise MailError("That is not an address we can send to.")
    if not _ADDRESS.match(raw):
        raise MailError("That is not an address we can send to.")
    return raw


def send_ready(job_id: str, address: str, piece: str = "",
               finished: float | None = None) -> None:
    """Send one "it's ready" message. Raises MailError if it cannot."""
    if not settings.can_email:
        raise MailError(settings.why_cannot_email())
    address = one_address(address)

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
            # Recipients named explicitly rather than read back out of
            # the header, so the envelope cannot grow past what was
            # validated even if the header is later built differently.
            server.send_message(message, to_addrs=[address])
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc

    logger.info("told %s that job %s is ready", address, job_id)
