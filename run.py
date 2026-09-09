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

    app = create_app()          # resumes the queue as it builds

    from app import store
    waiting = len(store.waiting())
    print(f"\n  score roots : "
          f"{', '.join(str(r.path) for r in settings.score_roots) or 'none'}")
    print(f"  jobs        : {settings.work_dir}")
    print(f"  alignment   : {'ready' if settings.can_autosync else 'unavailable'}")
    print(f"\n  http://{args.host}:{args.port}\n")

    # threaded=True so a running render doesn't block the SSE stream.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
