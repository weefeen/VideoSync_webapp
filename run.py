"""Start the web app.

    python run.py [--host 127.0.0.1] [--port 5000]

What else this process does depends on RABBITMQ_URL. Unset, the queue lives
in here and this process renders too — one command, no broker, which is what
a development machine wants. Set, the renderer is a separate
`python -m app.queue.worker` and this process only serves, applies what the
worker reports, and sweeps.
"""

import argparse
import logging

from app.routes import create_app
from app.settings import settings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    # Without this the applier and the janitor log into nothing: a job the
    # sweep re-offered, or an event that could not be applied, would leave no
    # trace anywhere. The worker configures its own; this is the other half.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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
    print(f"  queue       : "
          f"{'rabbitmq — renders run in `python -m app.queue.worker`'
             if settings.rabbitmq_url else 'in this process'}")
    if waiting:
        print(f"  queued      : {waiting}")
    print(f"\n  http://{args.host}:{args.port}\n")

    # threaded=True so a status poll is answered while work is going on.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
