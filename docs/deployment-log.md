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
    CairoSVG 2.9.1       librosa 1.0.0 ->      numba 0.67.0
    numpy 2.5.3 ->       scipy 1.18.1          torch 2.14.0+cpu

    -> these two were WRONG and are corrected in section 7. What pip
       resolved is not what the engine repositories were written against.
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

## 7. Library versions — pinned to what the engines expect

The first run failed here, and it was not configuration:

    TypeError: get_duration() got an unexpected keyword argument 'filename'
      VideoScoreSync/api_audio/chroma.py:364

`librosa.get_duration(filename=...)` was deprecated in 0.10 and **removed
in 1.0**. pip had resolved librosa 1.0.0 and numpy 2.5.3, because
VideoScoreSync's requirements pin numpy but leave librosa open.

Pinned to the pair already proven on the developer's machine — the same
combination its recognition environment runs torch and librosa on together:

    librosa==0.11.0    numpy==1.26.4

Worth stating as a rule rather than a fix: **the engine repositories are
read-only, so the environment bends to them.** Whatever pip resolves today
is not the question; what those repositories were written against is.

---

## 8. First full run on Linux

    align     17s   649 measures placed
    bands     29s   70 bands rasterised at 1916x358
    strip     23s
    encode   479s   1920x1080, band bottom
    ----------------
    total    548s   131 MB output, from 424s of music

    RATE  1.292 seconds per second of music

**This is 1.54x slower than the Windows workstation**, which measured
0.838. Every estimate in the design was built on the workstation figure,
so every one of them was optimistic by half:

| | assumed 0.82 | measured 1.292 |
|---|---|---|
| median 3.5-min piece | 2.9 min | **4.5 min** |
| a 10-minute upload | 8.2 min | **12.9 min** |
| the 25-minute cap | 20.5 min | **32 min** |

Measured on 2 shared cores. A dedicated 4-core plan will fall between the
two, and that measurement is still owed. The job table records elapsed time
against media length for exactly this reason: the constant in `app/jobs.py`
is a starting point and the median of real runs replaces it.


---

## 9. Recognition on Linux, and the bug it exposed

Identification itself worked here on the first attempt:

    indexes    0.7 s
    identify 129.0 s      8 windows, consensus 1.00
    RESULT   3ème Scherzo pour le Piano, Op. 39

and then said **"recognised, but no installed score to render it"** — with
full confidence, and no error anywhere.

The recogniser was right. What failed was the step after it, which turns a
piece id into a score package, and it failed for a reason that exists only
on this side:

    weefeen_id/labels.py:48    stem = Path(row["video"]).stem

Every `video` in `pair_list.json` is an absolute **Windows** path. On Linux
`pathlib.Path` is a `PosixPath`, and to a PosixPath a backslash is an
ordinary character — so the "stem" of

    C:\ZZ_perso\...\work_op_39__troisieme_scherzo,_..._dyZmzXMHItI.mp4

is that entire string. It matches no piece id. Measured on the two
platforms against the same file:

| | piece ids resolved |
|---|---|
| Windows | 186 of 186 |
| this node | **0 of 186** |

Not some pieces. All of them, silently, while reporting consensus 1.00.

**Fixed in `tools/identify_runner.py`, not in `music_finrgerprint`.** That
file already describes itself as the web app's side of the boundary, so it
now reads the pair list itself — eight lines, three fields — with
`PureWindowsPath`, which splits both separators on every platform. Checked
row by row against the dependency's own output on Windows: zero
disagreements, so nothing changed there.

`music_finrgerprint` still has the bug for its own scripts on Linux
(`identify_v6.py`, `run_aggregate_experiment.py`,
`drill_clean_failures.py`). That is the owner's to fix; it does not block
this. The one-line change is `PureWindowsPath` in place of `Path`.

---

## 10. Both platforms are checked on every push

The bug above reached this machine because **nothing ever ran on both**.
CI was `ubuntu-latest` only and development is Windows only, so a
difference between them could only be found by deploying and noticing that
the answer was wrong — and this whole class of difference produces an empty
result rather than an error.

`tools/selftest.py` is the part of the app that can be checked anywhere: no
ffmpeg, cairo, torch, score package or network. `.github/workflows/ci.yml`
runs it on `ubuntu-latest` and `windows-latest`, and it is the same script
to run by hand, so a red build is reproducible with one command.

    python tools/selftest.py

Seven checks, each from something that has actually gone wrong here. Both
platforms, same commit:

    win32  python 3.12.7  os.pathsep ';'    all 7 passed   sqlite 3.53.4
    linux  python 3.12.3  os.pathsep ':'    all 7 passed   sqlite 3.45.1

The differing SQLite builds are the reason the job store is exercised
rather than assumed: claiming uses `UPDATE ... RETURNING`, which is not in
older SQLite.

**The regression check was verified by putting the bug back**, not by
trusting that it would have caught it. With `PureWindowsPath` reverted to
`Path` on this node, `piece ids resolve` failed with the expected editions
against `got []`; restored, it passed again. A second, text-based check
went green throughout — it searched the file for "PureWindowsPath" and the
docstring explaining the fix contains it — so it was removed. A check that
passes on broken code is read as evidence and is worse than none.

### On Windows, run it under the app's interpreter

The three-interpreter split still applies here. `2026liszt` has torch and
no Flask, so the checks fail there on `app.routes`; the check says so
rather than leaving it to be guessed:

    C:\Users\msmabq\.conda\envs\VideoScoreSync\python.exe tools/selftest.py

Two smaller things the writing of it found and fixed: `app/store.py` had no
`close()`, so a temporary `WORK_DIR` could not be cleaned up on Windows,
where an open file cannot be deleted; and `.env.example` was not setting
`MAX_UPLOAD_GB`, `MAX_DURATION_MINUTES` or `MAX_CONCURRENT_RENDERS`, so a
local install and the server could not be compared line by line. Both
templates now carry the same 41 variables, and the check enforces it.

---

## 11. Operating this node

Two things cost time and are written down so they do not again:

- **`github-webapp` is an SSH host alias, not a git remote.** The remote is
  `origin`; `git fetch github-webapp` fails with a message about access
  rights that reads exactly like a broken deploy key. The key is fine:
  `ssh -T git@github-webapp` answers `Hi weefeen/VideoSync_webapp!`.
- **The deploy keys are root's and the checkout is `vsw`'s.** git as `vsw`
  cannot fetch; git as root leaves root-owned files behind. So it is
  `git fetch && git merge --ff-only` as root, then
  `chown -R vsw:vsw /srv/vsw/app`. Root also needs
  `git config --global --add safe.directory /srv/vsw/app`, once, or every
  git command refuses with "dubious ownership".

`deploy/deploy.sh` does not apply here — it is written for the production
host's rsynced release directories under `/mnt/volume_1/vsw`, not a git
checkout.

`.env` on this node has **no `SMTP_PASSWORD`**, so it cannot send mail. That
is deliberate for a disposable box and means the delivery step has not been
exercised here; it was proven on the workstation instead.

---

## 12. The queue slice, step 1 — proven on this node

`docs/broker-slice.md` step 1: the worker stops writing the job table and
reports instead, through `app/queue/ledger.py`. Both halves are still one
process; only the contract changed.

Driven through the real API on the node — `POST /api/upload`, then
`POST /api/jobs/<id>/render`, then polling `/api/jobs/<id>/status`, so the
whole path ran, not a stubbed piece of it:

     elapsed  state     stage    detail
          0s  running   -
          5s  running   align    matching the recording to the score
         20s  running   strip    timing bands to the performance
         45s  running   encode   1920x1080 · band 1916x358 bottom
        555s  done      done

    final stages   all six done        error  None
    output         131,321,451 bytes
    stage record   render, attempt 1, done, elapsed 550.8 s

**550.8 s for 424 s of music is 1.299 s/s**, against 1.292 measured before
the change (§8). The rewiring costs nothing measurable, which is the point:
it moved where facts are written, not what the work does.

`bands` passed inside one five-second poll because that package's bands were
already rasterised on disk from the earlier run. A score being rendered for
the first time will sit there for about half a minute.

### What this actually fixes

`stages` was a dict on the in-memory `Job`. Every `/status` poll rebuilds
the `Job` from the table, so it read all-pending for the whole render and
then jumped to complete — the progress indicator in `svs-wire.js:1153-1157`
has never moved. The stage now lives in a column and the row above is what
the page sees.

### Checks

Twelve now, still on both platforms, still needing no ffmpeg, cairo, torch
or network:

    win32  python 3.12.7  os.pathsep ';'   all 12 passed   sqlite 3.53.4
    linux  python 3.12.3  os.pathsep ':'   all 12 passed   sqlite 3.45.1

Two of the five new ones are worth naming. `old databases gain the new
columns` exists because every statement in `SCHEMA` is
`CREATE TABLE IF NOT EXISTS`, which does nothing to a table that already
exists: without `_migrate()` the first write naming `stage` would fail here,
against the only copy of the queue that matters. `ledger is idempotent` was
verified by removing the guard in `_done` and watching it fail with "a
redelivered 'done' changed the row a second time" — that guard is what stops
a redelivered completion sending a second email about one video.

### Also gone

The development page at `/` and its 380-line `app.js`, the SSE endpoint it
was the only user of, and `/api/options` and `/api/scores`, which nothing
else called. `/` redirects to `/app/`. The remaining routes are exactly the
API the designed interface calls, and nothing more.

---

## Still to do

- Measure seconds-per-second on the plan production will actually use;
  2 shared cores gave 1.292 and a dedicated 4-core box will differ
- gunicorn in place of the Flask development server — and with it the
  first real proof that the app *serves* on Linux, which "every module
  imports" is not
- A bot check before anything faces the public
- Apache vhost, certbot and DNS for `chopin.weefeen.com` — on the
  production Linode, not this one, and only once the above is proven

### Known, not yet addressed

**Score package layout is looked up case-exactly.** `app/package.py` probes
fixed names — `score/lines`, `measures.data`, `export.json`, `chroma.npy`.
Windows matches those whatever the case on disk; Linux does not. Every
package built so far is lowercase and the one installed here loads, so this
is a latent risk rather than a fault: a package that works on the
workstation could arrive here and simply not be found, which is the same
silent shape as the bug in section 9. Worth a check in `tools/doctor.py`,
which needs real packages and so cannot live in the CI self-test.
