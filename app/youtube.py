"""Reading a YouTube link, and nothing else.

The identity of a performance on YouTube is its video id, never the URL it
arrived in: the same recording reaches us as `youtube.com/watch?v=ID`, as
`youtu.be/ID`, inside a playlist, with a start time, with tracking
parameters a share button added, and from the Facebook group wrapped in
that site's own redirect. Deduplicating on the URL would make each of those
a different performance and align the same recording five times.

No network here. This module decides what a string refers to; whether that
video exists, is embeddable or is Chopin at all is somebody else's question.
"""
from __future__ import annotations

import re
import urllib.parse

# 11 characters of URL-safe base64. YouTube has used this shape since 2007
# and every id we accept must match it, so a path fragment that merely sits
# where an id would go cannot be mistaken for one.
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com",
    "youtu.be", "www.youtu.be",
}

# /embed/ID, /shorts/ID, /live/ID, /v/ID -- the id is the segment after
_PATH_PREFIXES = ("embed", "shorts", "live", "v")


class NotYouTube(ValueError):
    """The string is not a link to a single YouTube video."""


def video_id(text: str) -> str:
    """The video id a link refers to, or raise.

    Accepts a bare id as well, because an operator pasting one into an
    admin field means the same thing as pasting its watch URL.
    """
    text = (text or "").strip()
    if not text:
        raise NotYouTube("nothing was pasted")
    if _ID.match(text):
        return text

    # A link copied out of the group may be wrapped in Facebook's redirect,
    # which carries the real one in ?u=
    try:
        parsed = urllib.parse.urlsplit(text if "//" in text else "https://" + text)
    except ValueError as exc:
        # urlsplit raises on things like "http://[abc". NotYouTube subclasses
        # ValueError, so a caller catching that would have swallowed this and
        # read a malformed address as merely "not YouTube".
        raise NotYouTube("that is not a usable address") from exc
    host = (parsed.hostname or "").lower()
    if host.endswith("facebook.com") or host.endswith("l.facebook.com"):
        inner = urllib.parse.parse_qs(parsed.query).get("u")
        if inner:
            return video_id(urllib.parse.unquote(inner[0]))
        raise NotYouTube("that is a Facebook link with no video in it")

    if host not in _HOSTS:
        raise NotYouTube("that is not a YouTube link")

    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
        return _checked(candidate)

    query = urllib.parse.parse_qs(parsed.query)
    if "v" in query and query["v"]:
        return _checked(query["v"][0])

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
        return _checked(parts[1])
    # NOT a bare single segment: `youtube.com/abcdefghijk` is a channel or a
    # handle, and eleven characters of one look exactly like a video id. That
    # fallback turned a channel address into a video we would try to align.

    raise NotYouTube("that YouTube link does not name a single video")


def _checked(candidate: str) -> str:
    candidate = (candidate or "").strip()
    if not _ID.match(candidate):
        raise NotYouTube("that does not look like a video id")
    return candidate


def canonical_url(vid: str) -> str:
    """The one URL we store for a video, whatever form it arrived in."""
    return f"https://www.youtube.com/watch?v={_checked(vid)}"


def embed_url(vid: str, origin: str = "") -> str:
    """The official player, which is how a YouTube performance is watched.

    Never a download and never a re-render: the pianist's own video plays
    from YouTube, and the score is put beside it.
    """
    url = (f"https://www.youtube.com/embed/{_checked(vid)}"
           "?enablejsapi=1&playsinline=1&rel=0&modestbranding=1")
    if origin:
        url += "&origin=" + urllib.parse.quote(origin, safe="")
    return url


def looks_like_a_link(text: str) -> bool:
    """Whether to treat what was typed as a link at all."""
    try:
        video_id(text)
    except NotYouTube:
        return False
    return True
