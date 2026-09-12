"""Publish a finished video to a Facebook Page, where it plays natively.

A LINK never plays on Facebook. Whatever the Open Graph tags say, a shared
URL becomes a card with a picture and a click-through; Facebook only plays
video it hosts itself. So the video has to be put ON Facebook, and this is
the one way to do that from software: the Graph API's video edge on a Page.

The mechanism is `file_url`. Facebook is handed a URL and fetches the file
itself, which fits this system exactly: the bucket stays private, and the
URL is a presigned one that expires shortly after Facebook has pulled it.
Nothing about the video becomes permanently public.

WHAT THIS CANNOT DO, so nobody spends an afternoon trying: post to a
personal timeline. Facebook removed that from the API in 2018. It publishes
to a PAGE the token belongs to, which is what a business or a project has.

The token is a credential that can post as the Page. It lives in `.env`
only, is never sent to a compute node, and is never printed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from . import notify, storage, store
from .settings import settings

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"

# Facebook downloads the file itself, so the link only has to outlive that
# download. Half an hour is generous for a 150 MB file and still short.
LINK_SECONDS = 1800


class SocialError(RuntimeError):
    """The post did not happen. The message says why, without the token."""


def caption_for(score: str) -> str:
    """The owner's wording, per piece, from the library's own name."""
    piece = notify._pretty(score) if score else ""
    what = f"Chopin {piece} score-video" if piece else "Chopin score-video"
    return f"{what} by chopin.weefeen.com #Chopin #Piano #ScoreVideo #weefeen"


def post_video(job_id: str, caption: str | None = None,
               title: str | None = None) -> dict:
    """Publish one finished job's video to the configured Page.

    Returns Facebook's answer, which carries the video id; the video is then
    reachable at https://www.facebook.com/<id> and plays in the feed.
    """
    if not settings.facebook_page_id or not settings.facebook_page_token:
        raise SocialError(
            "FACEBOOK_PAGE_ID and FACEBOOK_PAGE_TOKEN are not both set. See "
            "tools/post_facebook.py for how to get them.")

    row = store.get_job(job_id)
    if row is None:
        raise SocialError(f"no job {job_id}")
    if row["state"] != store.DONE or not row["object_key"]:
        raise SocialError(f"job {job_id} is {row['state']!r} and has no "
                          f"stored video to publish")

    url = storage.presigned_get(row["object_key"], seconds=LINK_SECONDS)
    if not url:
        raise SocialError("could not sign a link to the video for Facebook "
                          "to fetch")

    text = caption if caption is not None else caption_for(row["score"] or "")
    fields = {
        "file_url": url,
        "description": text,
        "access_token": settings.facebook_page_token,
    }
    if title:
        fields["title"] = title

    endpoint = f"{GRAPH}/{settings.facebook_page_id}/videos"
    body = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "VideoSync/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # Facebook's error is JSON with a human message. Surface that, and
        # nothing else: the request carried the token and must not be echoed.
        try:
            message = json.loads(detail)["error"]["message"]
        except Exception:                            # noqa: BLE001
            message = detail[:300]
        raise SocialError(f"Facebook refused the video: {message}") from exc
    except OSError as exc:
        raise SocialError(f"could not reach Facebook: {exc}") from exc

    video_id = answer.get("id")
    if not video_id:
        raise SocialError(f"Facebook answered without a video id: {answer}")
    logger.info("job %s published to Facebook as video %s", job_id, video_id)
    answer["permalink"] = f"https://www.facebook.com/{video_id}"
    return answer
