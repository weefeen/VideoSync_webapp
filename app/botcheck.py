"""Cloudflare Turnstile: is there a human at the other end of this upload.

The site is free, unauthenticated, and every upload costs real work — a GPU
recognition and a multi-gigabyte render. That is exactly the shape an
automated agent abuses cheaply, and it is the earlier review's standing top
finding. Turnstile is the answer: a challenge the browser solves, producing a
token this server hands back to Cloudflare to confirm.

Configured or inert, like mail. With no keys set (`settings.bot_check`
False), `verify` returns True and the site behaves exactly as before, so this
ships dark and goes live the moment the keys are added to `.env`. The secret
never leaves the server; the site key is public and is the only half the page
sees.

No new dependency: the one outbound call uses urllib. It is given a short
timeout and, on any network trouble, FAILS OPEN — a Cloudflare outage must
not take uploads down with it, and the rate limits are still underneath. The
token is what an attacker cannot forge; a rare unverifiable token is a far
smaller risk than a site that stops accepting work whenever a third party
hiccups.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from .settings import settings

logger = logging.getLogger(__name__)

_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT = 5.0


def verify(token: str, remote_ip: str = "") -> bool:
    """True if this token is a valid, unspent Turnstile solution — or if the
    check is switched off.

    `remote_ip` is sent when known so Cloudflare can weigh it; it is optional
    and the check works without it.
    """
    if not settings.bot_check:
        return True                     # unconfigured: the site works as before
    if not token:
        return False                    # configured and no token: refuse

    fields = {"secret": settings.turnstile_secret, "response": token}
    if remote_ip:
        fields["remoteip"] = remote_ip
    data = urllib.parse.urlencode(fields).encode("ascii")

    try:
        with urllib.request.urlopen(_VERIFY_URL, data=data,
                                    timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        # FAIL OPEN. A challenge that we cannot reach Cloudflare to check is
        # not proof of a bot, and refusing every upload during a Cloudflare
        # blip would be a self-inflicted outage. Logged so a persistent
        # failure is visible; the rate limits still apply underneath.
        logger.warning("Turnstile verify unreachable, allowing through: %s",
                       exc)
        return True

    if result.get("success"):
        return True
    logger.info("Turnstile rejected a token: %s",
                ", ".join(result.get("error-codes", [])) or "no reason given")
    return False
