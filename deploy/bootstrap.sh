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
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
    libffi-dev shared-mime-info \
    git curl ca-certificates build-essential
    # libcairo2 is what cairosvg needs and what Windows made difficult;
    # ffmpeg does every encode; the pango/pixbuf trio are cairosvg's
    # runtime dependencies rather than optional extras.

say "An unprivileged user to run it"
id vsw >/dev/null 2>&1 || adduser --system --group --home /srv/vsw --shell /bin/bash vsw
install -d -o vsw -g vsw /srv/vsw /srv/vsw/work /srv/vsw/scores

say "Versions"
python3 --version
ffmpeg -version | head -1
