# Deployment log — the development node

Every change made to a machine, in order, with what it was for and what it
proved. Kept so that the next box can be built without guessing, and so
that anything odd about this one has a written cause rather than a story
somebody half remembers.

Append to it. Do not tidy it: a step that turned out to be wrong is worth
more here than a clean account that hides it.

---

## Node

| | |
|---|---|
| Address | `172.104.237.127` |
| Provider | Linode, 4 GB shared |
| Built | 9 September 2026, by the owner |
| OS | Ubuntu 24.04.4 LTS, kernel 6.8.0-134 |
| Resources | 2 cores · 3915 MB RAM · 79 GB disk (69 GB free) |
| Purpose | **Development. Disposable.** Not production, not `chopin.weefeen.com`. |

It exists so that the deployment can be got wrong somewhere other than the
Linode serving weefeen.com. Nothing here is precious; the whole point is
that it can be destroyed and rebuilt from this document.

---

## 1. Access

The owner's existing 2016 RSA key was used rather than a new one:

    C:\ZZ_perso\weefeen\PT\Devops\Putty\2016_keys\id_rsa
      -> ~/.ssh/id_rsa on the development workstation, mode 600
      fingerprint  SHA256:i+SDxY+f88ErpG5gpU/5Z3CE8e9h962qFlJNFNECixU (RSA 2048)

The workstation had no key of its own — only `known_hosts`, so earlier
logins to these servers were by password.

---

## 2. System packages

`deploy/bootstrap.sh`, which is written to be run more than once:

    python3 python3-venv python3-dev python3-pip
    ffmpeg
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0
    libffi-dev shared-mime-info
    git curl ca-certificates build-essential

Installed:

    python3   3.12.3-0ubuntu2.1
    ffmpeg    7:6.1.1-3ubuntu5
    git       1:2.43.0-1ubuntu7.3

`libcairo2` is the one that matters: on Windows cairo only loads out of a
conda tree, and that awkwardness is half the reason development there
needs three interpreters.

---

## 3. User and layout

    adduser --system --group --home /srv/vsw vsw        # uid 110

    /srv/vsw/
      app/                  the webapp          feature/recognition  4e735d1
      VideoScoreSync/       chroma + alignment  main                 6812283
      music_fingerprints/   recognition         discrim-v1           4c61a02
      scores/               score packages (not in git — copied)
      work/                 job workspace
      venv/                 one virtualenv for all of it
      test.mp4              one Chopin recording, for end-to-end runs

Nothing runs as root beyond the setup itself.

---

## 4. Repository access

GitHub allows a deploy key on **one** repository per account, so three
keys were generated on the node, each read-only and scoped to one repo,
each reached through its own SSH host alias in `/root/.ssh/config`:

| alias | repository | key on the node | registered as |
|---|---|---|---|
| `github-webapp` | `weefeen/VideoSync_webapp` | `id_webapp` | `chopin-dev-webapp` |
| `github-vss` | `weefeen/VideoScoreSync` | `id_ed25519` | `chopin-dev-node` |
| `github-fp` | `weefeen/music_fingerprints` | `id_fp` | `chopin-dev-fp` |

The VideoScoreSync row is the odd one: it was registered with the first key
generated on the box, before the per-repo keys existed, so its alias points
at `id_ed25519` rather than at an `id_vss`. Left as it is because it works;
noted because it will look wrong to anyone reading the config.

Read-only is deliberate. Two of these repositories must never be written
to from here, and having GitHub enforce that is better than remembering it.

**`music_line_extractor` is deliberately absent.** Nothing on the running
path imports it: alignment goes through VideoScoreSync's services, and the
only reference left is `app/autosync.py`, which nothing but a developer
script uses. It also holds no score packages — zero files are tracked under
its `project/` folder — so cloning it would fetch the tooling that makes
packages and none of the packages.

---

## 5. One virtualenv

The finding this node was built for. **On Linux, one interpreter runs
everything**, where Windows needs three:

    /srv/vsw/venv/bin/python -- Python 3.12.3

    Flask 3.1.3          python-dotenv 1.2.3   pillow 12.3.0
    CairoSVG 2.9.1       librosa 1.0.0         numba 0.67.0
    numpy 2.5.3          scipy 1.18.1          torch 2.14.0+cpu
    piano-transcription-inference 0.0.6        torchlibrosa 0.1.0
    tqdm 4.70.0          soundfile 0.14.0      validators 0.35.0
    mido 1.3.3           pretty_midi 0.2.11    h5py 3.16.0   matplotlib 3.11.1

Proved rather than assumed:

    torch      2.14.0+cpu, cuda=False
    CHROMA     ok — (12, 81) from 8s of audio
    RASTERISE  ok — cairo loaded with no conda anywhere

The chroma line is the one that counts. Under the app's environment on
Windows that call **aborts the process** — `LLVM ERROR: Symbol not found:
__svml_cosf8_ha`, which cannot be caught — and that is why alignment and
recognition run as subprocesses under a second conda environment there. It
simply works here.

**What that removes:** `tools/identify_runner.py` and `tools/sync_runner.py`
exist only to carry work into another interpreter. On this machine the
stages can import their libraries directly, so those wrappers and the
stderr-pattern-matching that classifies their failures both go away, and a
failure becomes an exception with a traceback.

### Deliberately NOT installed

VideoScoreSync's own `requirements.txt` was not used. It pulls **opencv**,
which needs `libGL`, and a missing `libGL` is exactly the error in that
project's own `embed_score_err.log`. Nothing here imports `cv2`, so the
services it does import were satisfied one package at a time instead:
`validators` was the only one their import chain actually needed beyond
what was already present.

---

## 6. Score packages

Copied rather than cloned — they are build products and are in no
repository. Sent as a tar over ssh, **excluding `*.png`, `*.jpg` and the
`.spj` archive**:

    Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH
      106 MB · 83 svg bands · 0 png bands

The exclusion is the owner's rule and it is a large one: bitmaps are four
fifths of a package, and gzipped SVG alone brings a package to about 2.8 MB.
`tools/doctor.py` reports any package that still carries them.

How the library reaches a production machine is **not yet decided** —
rsync, object storage, or a repository with LFS. At roughly a gigabyte for
all 374 pieces in SVG, any of the three works.

---

## Still to do

- Configure `.env` on the node and run the pipeline end to end on Linux
- Measure seconds-per-second here; every estimate in the design currently
  rests on a Windows workstation figure of 0.82
- gunicorn in place of the Flask development server
- A bot check before anything faces the public
- Apache vhost, certbot and DNS for `chopin.weefeen.com` — on the
  production Linode, not this one, and only once the above is proven
