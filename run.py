"""Start the web app.

    python run.py [--host 127.0.0.1] [--port 5000]

Rendering runs on background threads inside this process, so the server is
threaded and single-instance by design. Run `python tools/doctor.py` first
if scores or ffmpeg aren't being found.
"""

import argparse

from app.routes import create_app
from app.settings import settings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    problems = settings.problems()
    for issue in problems:
        print(f"WARNING: {issue}")

    app = create_app()          # starts the queue as it builds

    from app import store
    waiting = len(store.waiting())
    print(f"\n  score roots : "
          f"{', '.join(str(r.path) for r in settings.score_roots) or 'none'}")
    print(f"  jobs        : {settings.work_dir}")
    # can_sync is the aligner the app actually uses. This once read
    # can_autosync — the music_line_extractor route, since removed — and so
    # announced "alignment: unavailable" on a machine whose aligner worked.
    print(f"  alignment   : {'ready' if settings.can_sync else 'unavailable'}")
    print(f"  recognition : {'ready' if settings.can_identify else 'unavailable'}")
    print(f"  email       : {'ready' if settings.can_email else 'unavailable'}")
    if waiting:
        print(f"  queued      : {waiting}")
    print(f"\n  http://{args.host}:{args.port}\n")

    # threaded=True so a status poll is answered while a render runs.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
