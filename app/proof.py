"""Which addresses THIS browser has proved it can read.

WHY THIS EXISTS. Confirming an address used to be a property of the address
alone and it lasted for ever: once anyone had clicked the link once, every
later submission naming that address was mailed without further question.
So somebody who merely KNEW an address could make this server send its
owner "we are making your score video" for a video they never uploaded, and
spend the owner's weekly allowance doing it -- the limits are keyed on the
address, not on whoever typed it.

A confirmation click proves one thing: at that moment, someone reading that
mailbox was also using that browser. This records exactly that, in the
browser, signed so it cannot be written by anyone but us. A submission that
names a confirmed address from a browser which cannot show the proof is
treated as unproved and asks for confirmation again.

WHAT IT IS NOT. It is not a login and not an identity. A shared computer
carries the proof for whoever used it; a new phone has to confirm again.
That is the honest trade, and it is the reason the cookie is the mechanism
rather than an IP: an IP is shared by a whole office and changes when a
phone moves between cells, so it can neither confirm nor deny that the same
person is back.

THE ADDRESS IS NEVER IN THE COOKIE. Only an HMAC of it, under a key that
never leaves the server, so a stolen cookie tells its thief nothing about
whose mailbox it proves -- and cannot be replayed for a different address.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from . import store

logger = logging.getLogger(__name__)

COOKIE = "vsw_proof"

# A browser may hold proof for several addresses -- a household, somebody
# who moved from work to personal mail -- but not an unbounded list, which
# would be a cookie that grows for ever and a nice place to hide data.
MAX_ADDRESSES = 5

# How long the cookie itself survives in the browser. The server-side expiry
# in `store.may_mail` is the real one; this only stops an ancient cookie
# being presented long after it could still mean anything.
COOKIE_DAYS = 400


def _key() -> bytes:
    """The signing key, made once and kept in the database.

    Generated on first use rather than configured: another secret to place
    by hand is another thing to forget, and this one has no meaning outside
    this install -- losing it costs everybody one extra confirmation, not
    access to anything.
    """
    existing = store.get_meta("proof_key")
    if not existing:
        store.set_meta(
            "proof_key",
            base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"))
        # READ BACK, never use the one just generated. Two workers starting
        # together each make a key and only one is stored; a process that
        # trusted its own would sign cookies with a key no other process --
        # including itself after a restart -- could verify.
        existing = store.get_meta("proof_key")
        logger.info("made a signing key for browser proofs")
    if not existing:
        raise RuntimeError("could not store a signing key for browser proofs")
    return base64.urlsafe_b64decode(existing.encode("ascii"))


def tag(address: str) -> str:
    """The stable, opaque stand-in for an address inside a cookie."""
    return hmac.new(_key(), (address or "").strip().lower().encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _sign(payload: bytes) -> str:
    mac = hmac.new(_key(), payload, hashlib.sha256).digest()
    return (base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
            + "." + base64.urlsafe_b64encode(mac).decode("ascii").rstrip("="))


def _unsign(value: str) -> dict | None:
    try:
        body, mac = (value or "").split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        given = base64.urlsafe_b64decode(mac + "=" * (-len(mac) % 4))
    except Exception:                                  # noqa: BLE001
        return None
    want = hmac.new(_key(), payload, hashlib.sha256).digest()
    # compare_digest, not ==: a byte-by-byte comparison that returns early
    # leaks where the first difference is, which is enough to forge a MAC
    # one byte at a time.
    if not hmac.compare_digest(want, given):
        return None
    try:
        got = json.loads(payload.decode("utf-8"))
    except Exception:                                  # noqa: BLE001
        return None
    return got if isinstance(got, dict) else None


def proves(cookie_value: str | None, address: str) -> bool:
    """Whether this browser has proved it can read this address."""
    if not cookie_value or not address:
        return False
    got = _unsign(cookie_value)
    if not got:
        return False
    return tag(address) in (got.get("h") or [])


def add(cookie_value: str | None, address: str) -> str:
    """The cookie value that also proves `address`, keeping what it had."""
    got = _unsign(cookie_value) or {}
    held = [h for h in (got.get("h") or []) if isinstance(h, str)]
    mine = tag(address)
    held = [h for h in held if h != mine]
    held.insert(0, mine)
    payload = json.dumps({"v": 1, "h": held[:MAX_ADDRESSES],
                          "t": int(time.time())},
                         separators=(",", ":")).encode("utf-8")
    return _sign(payload)


def attach(response, address: str, request) -> None:
    """Record on this browser that `address` has just been proved.

    `secure` is not set unconditionally: the cookie would then be dropped
    on a plain-HTTP development server and the whole flow would be
    untestable there. Production is HTTPS and the request says so.
    """
    response.set_cookie(
        COOKIE,
        add(request.cookies.get(COOKIE), address),
        max_age=COOKIE_DAYS * 86400,
        httponly=True,          # no script needs it, and XSS should not get it
        samesite="Lax",         # it must survive the click from an email
        secure=request.is_secure,
        path="/",
    )
