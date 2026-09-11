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
import socket
import ssl
import unicodedata
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from . import retention
from .settings import settings

logger = logging.getLogger(__name__)


class MailError(RuntimeError):
    """The message could not be sent."""


def _link(job_id: str) -> str:
    """The one address a visitor is ever given for their job.

    The page, not the file. `/api/jobs/<id>/download` answers 404 until the
    render finishes and a raw JSON 410 after the retention window, so a link
    straight to it is broken at both ends of the job's life — exactly when
    somebody is most likely to click one out of an old mail. The page is
    meaningful at every point: queued, rendering with its stage, ready with a
    button, or expired with an explanation.

    It is also the same address the browser puts in the bar while rendering,
    so the mail and the bookmark agree.
    """
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/app/#job={job_id}"


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


def _compose(subject: str, address: str, body: str) -> EmailMessage:
    """One message, with the headers a receiver expects to find.

    Written because Yahoo filed the first real message as spam, and three of
    the reasons were ours:

      * NO `Date`. RFC 5322 makes it mandatory and `smtplib.send_message`
        does not add it — a widespread and costly assumption. SpamAssassin
        scores `MISSING_DATE` on its own.
      * NO `Message-ID`. Same omission, scored as `MISSING_MID`, and without
        it a mail client cannot thread or de-duplicate either.
      * BASE64 BODY. A single em-dash in "— Weefeen" pushed the whole
        message to base64, because that is what `set_content` picks for
        non-ASCII. A short plain-text message that arrives entirely base64
        is what obfuscators send to hide wording from filters, and it is
        scored as such. The body is now 7-bit and hard-wrapped — see
        `_plain`, which also explains why quoted-printable was not enough.

    The domain in the Message-ID is taken from the sending address, so it
    aligns with From and SPF instead of leaking the machine's hostname.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = address
    message["Date"] = formatdate(localtime=True)
    sender = settings.smtp_from
    domain = sender.rsplit("@", 1)[-1].strip("> ").strip() or "localhost"
    message["Message-ID"] = make_msgid(domain=domain)
    # RFC 3834: this is machine-generated and nobody should auto-reply to it.
    # It also tells a receiver the mail is transactional rather than bulk.
    message["Auto-Submitted"] = "auto-generated"
    # Absent, and the relay's scanner fires MISSING_XM_UA: every real mail
    # client identifies itself, so a message from none looks machine-made in
    # the way that matters.
    # With a version: without one the scanner fires XM_UA_NO_VERSION, since
    # a mailer that will not say which build it is looks like software
    # pretending to be a mail client.
    message["X-Mailer"] = "VideoSync 1.0"
    message.set_content(_plain(body), cte="7bit")
    return message


def _plain(body: str) -> str:
    """Plain ASCII, hard-wrapped, so nothing downstream needs to re-encode it.

    A2 signs our mail and then relays it through MailChannels, and Yahoo
    reports the signature as broken — with the published key verified
    byte-for-byte against the one A2 generated. So something alters the
    message between the signature and the recipient, and the body hash is
    the fragile part.

    One class of that is ours to remove. A single em-dash forced the whole
    message to quoted-printable, which soft-wraps long lines with `=`
    continuations; a relay that re-wraps them differently changes the body
    and breaks the hash. Pure 7-bit ASCII, hard-wrapped short, gives a relay
    nothing to rewrite.

    Typography lost: an em-dash becomes a hyphen. A dash is a small price for
    a message that arrives.
    """
    swaps = {"—": "-", "–": "-", "’": "'", "‘": "'",
             "“": '"', "”": '"', "…": "...", " ": " "}
    for bad, good in swaps.items():
        body = body.replace(bad, good)
    # Anything still outside ASCII — a piece title with an accent, and this
    # library is full of French ones — would put the message back on
    # quoted-printable. Decomposed and stripped of its accents rather than
    # replaced: "3ème Scherzo" becomes "3eme Scherzo", which is readable,
    # where the obvious `errors="replace"` gives "3?me Scherzo", which looks
    # like the mail is broken.
    body = unicodedata.normalize("NFKD", body)
    body = body.encode("ascii", "ignore").decode("ascii")

    out = []
    for para in body.split("\n"):
        if len(para) <= 72:
            out.append(para)
            continue
        line = ""
        for word in para.split(" "):
            if line and len(line) + 1 + len(word) > 72:
                out.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        if line:
            out.append(line)
    return "\n".join(out)


def _helo_name() -> str:
    """What this machine calls itself in the SMTP greeting.

    Python defaults to the system hostname, and this box's is literally
    `localhost` — so every message arrived announcing `HELO [127.0.0.1]`, and
    the relay's own scanner fired IE_MM_RCVD_HELO_LOOPBACK_IP on it. A
    loopback address in a greeting from a public host is a thing only
    misconfigured senders and spam software do.

    Taken from PUBLIC_BASE_URL, which is the one name this install is already
    known by and already holds a certificate for.
    """
    base = (settings.public_base_url or "").strip()
    host = base.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host and "." in host and not host.startswith("127."):
        return host
    name = socket.getfqdn()
    return name if "." in name and name != "localhost" else ""


def _send(message, address: str) -> None:
    """Hand one message to the relay. Raises MailError if it cannot.

    One copy, used by both messages. Two copies of this drift: the day
    somebody fixes a TLS detail in one and not the other is the day half the
    mail stops arriving and the half that still works hides it.
    """
    try:
        helo = _helo_name() or None
        if settings.smtp_ssl:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                      timeout=30, local_hostname=helo,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port,
                                  timeout=30, local_hostname=helo)
        with server:
            if settings.smtp_starttls and not settings.smtp_ssl:
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            # Recipients named explicitly rather than read back out of the
            # header, so the envelope cannot grow past what was validated
            # even if the header is later built differently.
            server.send_message(message, to_addrs=[address])
    except (OSError, smtplib.SMTPException) as exc:
        raise MailError(f"{type(exc).__name__}: {exc}") from exc


def send_alarm(kind: str, detail: str) -> None:
    """Tell the operator the scaler has stopped and will not act.

    Sent when it finds something it refuses to resolve by guessing — two
    machines where there should be one, a create that failed, the hourly
    ceiling reached. All of those leave the system in a state that is safe
    but stuck, and stuck is invisible: the queue simply stops moving and
    nobody is told.

    Goes to ALERT_EMAIL, not to a visitor.
    """
    to = (settings.alert_email or "").strip()
    if not to or not settings.can_email:
        return
    to = one_address(to)
    message = _compose(
        f"Compute scaler stopped: {kind}", to,
        f"The scaler has found something it will not resolve on its own, "
        f"and has stopped acting until somebody looks.\n\n"
        f"{detail}\n\n"
        f"Nothing has been created or deleted. Renders already running are "
        f"unaffected; new ones will queue until this is cleared.\n\n"
        f"- VideoSync\n")
    _send(message, to)
    logger.info("told the operator the scaler is stuck: %s", kind)


def send_compute(action: str, reason: str, *, ready: int = 0,
                 unacked: int = 0, shadow: bool = True,
                 hours: float | None = None,
                 cost: float | None = None) -> None:
    """Tell the operator a compute machine came up or went away.

    Goes to ALERT_EMAIL, never to a visitor: this is an operational fact and
    the address is the operator's own mailbox.

    `shadow` says whether a machine really was created. While the scaler is
    in shadow nothing is created at all, and a message that said "created" of
    a machine that does not exist would be a lie that costs trust in every
    later message. It says "would have" until the scaler is real.

    Its own subject prefix so these thread separately from visitor mail, and
    its own rate-limit bucket at the caller, so a flapping scaler cannot
    spend the allowance of the mail that tells somebody their video is ready.
    """
    to = (settings.alert_email or "").strip()
    if not to or not settings.can_email:
        return
    to = one_address(to)

    verb = {"would-create": "would have been created",
            "would-destroy": "would have been destroyed",
            "created": "was created",
            "destroyed": "was destroyed"}.get(action, action)
    mark = "[shadow] " if shadow else ""

    lines = [f"A compute machine {verb}.", "", f"Why: {reason}", "",
             f"Queue at that moment: {ready} waiting, {unacked} being rendered."]
    if hours is not None:
        money = f" (about ${cost:.2f})" if cost is not None else ""
        lines += ["", f"Machine-hours so far: {hours:.2f}{money}."]
    if shadow:
        lines += ["", "NOTHING WAS ACTUALLY CREATED OR DESTROYED. The scaler "
                      "is recording what it would do, so its decisions can be "
                      "judged against real traffic before it is given the "
                      "power to spend money."]
    lines += ["", "— VideoSync"]

    message = _compose(f"{mark}Compute: {verb}", to, "\n".join(lines) + "\n")
    _send(message, to)
    logger.info("told the operator a machine %s", verb)


def send_queued(job_id: str, address: str, piece: str = "",
                ahead: int = 0, minutes: float | None = None) -> None:
    """Send one "we have it, here is roughly how long" message.

    Sent because a render can take the better part of an hour and silence
    for that long reads as failure — at which point people upload the same
    recording again, which costs another render and lengthens the queue that
    caused it.

    Carries the SAME link as the ready mail, deliberately. One address that
    works from the moment of submission is a page somebody can bookmark, keep
    open, or come back to — so a mail that goes to spam costs a notification
    rather than the video.

    Deliberately vague about time. `minutes` comes from measurements that
    move, and a promise of "12 minutes" that becomes 40 is worse than "about
    quarter of an hour". Rounded outward for that reason.
    """
    if not settings.can_email:
        raise MailError(settings.why_cannot_email())
    address = one_address(address)

    if minutes is None:
        when = "We will email you as soon as it is finished."
    elif minutes < 12:
        when = ("It should be ready in about ten minutes. We will email you "
                "when it is.")
    elif minutes < 40:
        when = (f"It should be ready in about half an hour. We will email you "
                f"when it is.")
    else:
        when = (f"It should take about {round(minutes / 30) / 2:.1f} hours. "
                f"We will email you when it is finished.")

    # "3 ahead of you" is honest and useless; what a person wants to know is
    # whether anything is wrong. Position is mentioned only when there IS a
    # queue, because "you are first" invites the question why it is not done.
    line = ""
    if ahead == 1:
        line = "There is one video ahead of yours. "
    elif ahead > 1:
        line = f"There are {ahead} videos ahead of yours. "

    named = f" of {piece}" if piece else ""
    message = _compose(
        "We are making your score video", address,
        f"We have your recording{named} and it is being made into a video.\n\n"
        f"{line}{when}\n\n"
        f"You can also follow it here, or come back to it later:\n\n"
        f"{_link(job_id)}\n\n"
        f"You do not need to keep the page open.\n\n"
        f"— Weefeen\n")
    _send(message, address)
    logger.info("told %s that job %s is queued (%d ahead)",
                address, job_id, ahead)


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
    message = _compose(
        "Your score video is ready", address,
        f"Your video{named} has finished rendering.\n\n"
        f"{_link(job_id)}\n\n"
        f"{window}\n\n"
        f"— Weefeen\n")

    _send(message, address)
    logger.info("told %s that job %s is ready", address, job_id)
