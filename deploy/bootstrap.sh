#!/usr/bin/env bash
#
# Bring a bare Ubuntu box to the point where this app can run.
#
# Written to be run twice without harm, because the first attempt at a new
# machine is never the last. It installs system packages, makes an
# unprivileged user, and builds one virtualenv.
#
# The question it exists to answer is at the end: whether torch, numba and
# cairo can share a single interpreter on Linux. On Windows they cannot —
# chroma extraction aborts the process under the app's environment, and
# cairo only loads from a conda tree — which is why development here runs
# three interpreters. If Linux does not have that problem, the compute side
# is one process rather than three and a good deal of the design gets
# simpler.

set -euo pipefail
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-dev python3-pip \
    ffmpeg \
    rabbitmq-server \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi-dev shared-mime-info \
    git curl ca-certificates build-essential
    # libcairo2 is what cairosvg needs and what Windows made difficult;
    # ffmpeg does every encode; the pango/pixbuf trio are cairosvg's
    # runtime dependencies rather than optional extras.

say "An unprivileged user to run it"
id vsw >/dev/null 2>&1 || adduser --system --group --home /srv/vsw --shell /bin/bash vsw
# The release layout: code under releases/, the live one symlinked as
# current/, and everything that must outlive a release under shared/.
install -d -o vsw -g vsw /srv/vsw /srv/vsw/releases /srv/vsw/shared                         /srv/vsw/shared/var /srv/vsw/scores

say "One virtualenv, shared by every release"
# Shared rather than one per release, which is what the script this was
# adapted from does. Here that would be wrong twice over: the environment
# is 1.5 GB because of torch, so five releases is 7.5 GB of near-identical
# copies; and the heavy half of it is dictated by two READ-ONLY engine
# repositories, so it does not change when this app's code changes. What a
# rollback needs to undo is the code.
REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [ ! -x /srv/vsw/venv/bin/python ]; then
    python3 -m venv /srv/vsw/venv
fi
/srv/vsw/venv/bin/pip install --quiet --upgrade pip
# BOTH files. requirements.txt alone serves pages and renders; it cannot
# recognise or align, because those run in subprocesses whose imports
# nothing at startup touches — so the omission shows up on the first
# upload rather than at deploy time.
/srv/vsw/venv/bin/pip install --quiet     -r "$REPO/requirements.txt" -r "$REPO/requirements-engine.txt"
chown -R vsw:vsw /srv/vsw/venv

say "Can the engines actually be imported?"
# The check that a requirements-only install would have failed.
/srv/vsw/venv/bin/python - <<'PYCHECK'
import importlib, sys
missing = []
for module in ("flask", "cairosvg", "pika", "torch", "librosa", "numba",
               "soundfile", "piano_transcription_inference"):
    try:
        importlib.import_module(module)
    except Exception as exc:                       # noqa: BLE001
        missing.append(f"  {module}: {type(exc).__name__}: {exc}")
if missing:
    print("ENGINES NOT USABLE:"); print("
".join(missing)); sys.exit(1)
import torch
print(f"  torch {torch.__version__}, cuda={torch.cuda.is_available()}")
PYCHECK

say "Versions"
python3 --version
ffmpeg -version | head -1
