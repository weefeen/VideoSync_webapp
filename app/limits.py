"""Keep one visitor from becoming a workload.

Every expensive thing here is free to ask for and costly to answer:
identifying a piece is about forty-five seconds of GPU, a render is minutes
of CPU and a hundred megabytes of disk, and finishing one sends mail to
whatever address was typed. Without limits, a short script is a denial of
service and a mail cannon at the same time.

Counters are sliding windows kept in memory and written to disk, because an
abuse counter that resets when the process restarts is an abuse counter a
script can reset. They are keyed by client address and by email address —
neither is proof of identity, and neither is meant to be. This raises the
cost of automating against the service; it does not make it impossible, and
a bot check at submit is still the right next step.

Limits are deliberately generous for a person and obstructive for a loop.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
import time

from .settings import settings

logger = logging.getLogger(__name__)

HOUR = 3600
DAY = 86400
WEEK = 604800


@dataclasses.dataclass(frozen=True)
class Rule:
    limit: int
    window: int
    message: str


# What a person plausibly does in an hour or a day, and what a script does
# in a second. Overridable from the environment for a busier install.
def _n(var: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(var, "").strip() or default))
    except ValueError:
        return default


RULES = {
    # Cheap, but the doorway to everything else.
    "upload_ip": Rule(_n("LIMIT_UPLOADS_PER_HOUR", 8), HOUR,
                      "That is a lot of uploads in a short time. "
                      "Try again a little later."),
    # The expensive one: a GPU is busy for about forty-five seconds.
    "identify_ip": Rule(_n("LIMIT_IDENTIFY_PER_HOUR", 8), HOUR,
                        "That is a lot of recognitions in a short time. "
                        "Try again a little later."),
    # Minutes of work and a file that has to be kept. There is no sign-in,
    # so the address someone came from is the only thing that persists
    # between visits — a weak identity, and the reason the allowance is
    # counted over a week rather than a day: a coarse window is harder to
    # wait out than a fine one.
    "render_ip": Rule(_n("LIMIT_RENDERS_PER_WEEK", 3), WEEK,
                      "That is as many videos as one visitor can make this "
                      "week. It resets a week after the first one."),
    "render_email": Rule(_n("LIMIT_RENDERS_PER_EMAIL_WEEK", 3), WEEK,
                         "That address has made as many videos as it can "
                         "this week."),
    # Mail goes to an address someone typed, so it is capped hardest.
    #
    # TWO BUCKETS, NOT ONE, and the reason matters. The "your video is ready"
    # mail carries the promise: somebody waited up to an hour for it. The
    # "you are queued" mail is a courtesy. With one shared allowance the
    # courtesy spends the promise's budget — at 3 renders and 3 mails a week,
    # adding a second mail per render meant the ready mail for renders 2 and
    # 3 was refused, silently, and the person who waited longest got nothing.
    # Separate buckets make that impossible rather than merely unlikely.
    "mail_email": Rule(_n("LIMIT_MAIL_PER_EMAIL_WEEK", 4), WEEK, "mail cap reached"),
    "mail_queued_email": Rule(_n("LIMIT_QUEUED_MAIL_PER_EMAIL_WEEK", 4), WEEK,
                              "queued-notice cap reached"),
    # Operational mail, in its OWN bucket. A scaler that flaps would
    # otherwise spend the allowance belonging to the message that tells
    # somebody their video is ready — the same mistake the queued notice
    # made, and the reason that one has its own bucket too.
    "mail_compute": Rule(_n("LIMIT_COMPUTE_MAIL_PER_DAY", 40), DAY,
                         "compute-notice cap reached"),
    "mail_total": Rule(_n("LIMIT_MAIL_PER_DAY", 200), DAY, "daily mail cap reached"),
}


class _Counters:
    """Sliding windows, persisted so a restart is not a reset."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}
        self._loaded = False
        self._dirty = False

    def _file(self):
        return settings.work_dir / "limits.json"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8"))
            self._hits = {k: [float(t) for t in v] for k, v in raw.items()}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            self._hits = {}

    def _save(self) -> None:
        if not self._dirty:
            return
        try:
            path = self._file()
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".part")
            temp.write_text(json.dumps(self._hits), encoding="utf-8")
            temp.replace(path)
            self._dirty = False
        except OSError as exc:
            logger.warning("could not persist rate limits: %s", exc)

    def check(self, bucket: str, key: str) -> tuple[bool, int]:
        """(allowed, seconds until it would be) without recording a hit."""
        rule = RULES[bucket]
        now = time.time()
        with self._lock:
            self._load()
            hits = [t for t in self._hits.get(f"{bucket}:{key}", [])
                    if now - t < rule.window]
            if len(hits) < rule.limit:
                return True, 0
            return False, int(rule.window - (now - min(hits))) + 1

    def record(self, bucket: str, key: str) -> None:
        now = time.time()
        with self._lock:
            self._load()
            name = f"{bucket}:{key}"
            window = RULES[bucket].window
            hits = [t for t in self._hits.get(name, []) if now - t < window]
            hits.append(now)
            self._hits[name] = hits
            # Forget everything that can no longer deny anything.
            if len(self._hits) > 500:
                self._hits = {k: v for k, v in self._hits.items() if v}
            self._dirty = True
            self._save()


_counters = _Counters()


class Refused(Exception):
    """The request is over a limit. `retry_after` is in seconds."""

    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


def guard(bucket: str, key: str) -> None:
    """Raise `Refused` if this would exceed the rule; otherwise count it."""
    if not key:
        key = "unknown"
    allowed, wait = _counters.check(bucket, key)
    if not allowed:
        logger.info("refused %s for %s (%ds)", bucket, key, wait)
        raise Refused(RULES[bucket].message, wait)
    _counters.record(bucket, key)


def allowed(bucket: str, key: str) -> bool:
    """Check and count, without raising. For mail, where the send is
    optional and a refusal is not the caller's failure."""
    try:
        guard(bucket, key)
        return True
    except Refused:
        return False


def client_key(request) -> str:
    """Who is asking, as well as can be told.

    `remote_addr` is used unless TRUST_PROXY says a reverse proxy sits in
    front, because a forwarded header is written by the client and trusting
    it by default would let anyone spend everyone else's quota.

    THE LAST ENTRY, NOT THE FIRST. `X-Forwarded-For` is a list a client can
    seed with anything: send `X-Forwarded-For: 1.2.3.4` and the leftmost
    entry is 1.2.3.4, a value the attacker chose. Reading `forwarded[0]` made
    every limit in this app per-request-resettable — rotate the header and
    the 8/hour and 3/week caps never trip. Our Apache is the single edge
    proxy and forwards with `ProxyAddHeaders` on, so it APPENDS the real peer
    it saw as the last element; whatever the client put earlier in the list,
    the true client is `forwarded[-1]`. An attacker cannot get past our own
    proxy's append, so they cannot forge the last entry.

    This holds for exactly one trusted hop, which is the deployment. If a CDN
    is ever put in front of Apache, this becomes "the Nth from the end" and
    must be revisited — hence TRUST_PROXY being an explicit switch rather
    than a guess. `deploy/install-web.sh` also has Apache overwrite the
    header from the resolved peer as a second line of defence.
    """
    if os.getenv("TRUST_PROXY", "").strip().lower() in ("1", "true", "yes"):
        forwarded = [p.strip() for p in
                     (request.headers.get("X-Forwarded-For") or "").split(",")
                     if p.strip()]
        if forwarded:
            return forwarded[-1]
    return request.remote_addr or "unknown"
