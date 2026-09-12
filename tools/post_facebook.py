"""Put a finished video on Facebook, where it plays in the feed.

    python tools/post_facebook.py <job_id>
    python tools/post_facebook.py <job_id> --caption "your own words"
    python tools/post_facebook.py            # lists what could be posted

Run on the web box as vsw, like retrigger.py. Not an HTTP endpoint on
purpose: the site has no login, and anything that can post as the Page must
not be reachable by whoever finds the URL.

ONE-TIME SETUP, which needs your Facebook login and so cannot be done here:

  1. developers.facebook.com -> My Apps -> Create App (type "Business").
     The app can stay in Development mode: it may publish to Pages that
     you administer without App Review. Review is only needed to let
     OTHER people's Pages use it.
  2. Tools -> Graph API Explorer. Pick the app. Under "User or Page", pick
     the Page. Add permissions pages_manage_posts, pages_read_engagement,
     pages_show_list. Generate Access Token and grant them.
  3. Make it long-lived, or it dies in an hour: Tools -> Access Token
     Debugger -> paste it -> "Extend Access Token". A Page token derived
     from an extended user token does not expire.
  4. The Page id: same Explorer, query `me?fields=id,name` with the Page
     selected.
  5. On the server, WITHOUT the token ever passing through a chat or a
     shell history:
         sudo bash deploy/set-secret.sh FACEBOOK_PAGE_ID     (then type it)
         sudo bash deploy/set-secret.sh FACEBOOK_PAGE_TOKEN  (then paste it)

Facebook fetches the file itself from a link that expires in half an hour;
the bucket stays private and nothing about the video becomes public.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import social, store  # noqa: E402
from app.settings import settings  # noqa: E402


def show() -> None:
    rows = store.query(
        "SELECT id, score, finished FROM jobs WHERE state = ? AND object_key "
        "IS NOT NULL ORDER BY finished DESC LIMIT 15", (store.DONE,))
    if not rows:
        print("nothing finished to post")
        return
    print("finished videos, newest first:\n")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["finished"] or 0))
        print(f"  {r['id']}  {when}  {social.caption_for(r['score'] or '')}")
    print("\npost one with:  python tools/post_facebook.py <job_id>")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish a finished video to the Facebook Page.")
    parser.add_argument("job_id", nargs="?", help="the finished job to post")
    parser.add_argument("--caption", help="override the composed caption")
    parser.add_argument("--title", help="a title for the video on Facebook")
    args = parser.parse_args()

    if not args.job_id:
        show()
        return 0

    if not (settings.facebook_page_id and settings.facebook_page_token):
        print("FACEBOOK_PAGE_ID / FACEBOOK_PAGE_TOKEN are not set on this "
              "box. The setup steps are at the top of this file.",
              file=sys.stderr)
        return 2

    print(f"posting {args.job_id} to Page {settings.facebook_page_id} ...")
    try:
        answer = social.post_video(args.job_id, caption=args.caption,
                                   title=args.title)
    except social.SocialError as exc:
        print(f"\n  FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"\n  posted. Facebook video id {answer['id']}")
    print(f"  {answer['permalink']}")
    print("\n  Facebook processes the upload for a minute or two before it "
          "appears on the Page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
