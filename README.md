# VideoSync_webapp

A performance video goes in; a video with the matching score band burned in,
advancing in time with the playing, comes out. Public, no sign-in, at
`chopin.weefeen.com`.

Someone uploads a recording. The app names the piece by listening to it,
aligns it to an engraved score, renders 1080p, and emails a link that works
for 48 hours.

---

## The repositories, and how they are joined

Three, joined by **file contracts and subprocesses, never by imports**. Two of
them are read-only and are never modified from here.

| Repo | What it does for us | How we reach it |
|---|---|---|
| `music_finrgerprint` | names the piece from the audio | subprocess, `tools/identify_runner.py` |
| `VideoScoreSync` | chroma extraction and alignment | subprocess, `tools/sync_runner.py` |
| `music_line_extractor` | **not used at runtime** — it builds the score packages, offline | — |
| **this repo** | everything else: the site, the queue, the render | — |

Score packages are build products of `music_line_extractor`. They live in no
repository and are copied to a machine; `SCORE_ROOT_DIGITAL` says where.

**Why subprocesses and not imports.** On Windows no single interpreter can host
the whole workflow: chroma extraction aborts under this app's environment with
an uncatchable `LLVM ERROR: Symbol not found: __svml_cosf8_ha`, and cairo only
loads from the app's conda tree. On Linux one virtualenv runs everything, so
the boundary is not needed there — but it is kept, because it is also the
boundary the compute host will be split along.

---

## Layout

```
run.py                  start the site: python run.py

app/
  routes.py             the HTTP surface — every endpoint
  settings.py           .env into one frozen object; the "can we?" properties
  jobs.py               a Job, its public shape, the queue-position estimate
  store.py              SQLite: the jobs and stage_runs tables, and migrations
  pipeline.py           one job end to end: prepare, align, bands, strip, encode

  queue/                the seam between taking an order and doing the work
    messages.py           RenderTask and Event — the two shapes, versioned
    transport.py          how they travel; in-process today, a broker next
    worker.py             does the work, reports; never opens the database
    ledger.py             the ONLY writer of a worker's report into the table
    webside.py            applier and janitor threads; owns the table

  identify.py           recognition: calls the runner, reads its verdict
  sync.py               alignment: calls the runner, reads measures.data
  library.py            what the interface is offered, and edition resolution
  package.py            reads one score package off disk
  render.py             rasterise and recolour bands, build the strip, encode
  panel.py  fonts.py    the title panel
  svg.py                cairo, and the Windows dance to make it load
  paths.py              a job's folder layout
  limits.py             what one visitor may use
  notify.py             the "it is ready" email
  retention.py          the 48-hour window
  stats.py              how many videos this install has actually made
  static/svs/           the interface, served as handed over, at /app/

tools/                  see tools/README.md
docs/                   see docs/README.md
deploy/                 bootstrap.sh (a fresh machine), deploy.sh (a release)
design/mockup/          the design as handed over, kept for provenance only
```

The site is at `/app/`. `/` redirects there.

---

## Input contract: a score package

A folder. Only `score/` is read, plus `reference/` when present:

```
score/source.krn     the score
score/lines/         band images, named <first_measure>.svg (or .png)
score/export.json    manifest (band_w, band_h, options)

reference/           a curated reference recording — required
  audio.wav
  measures.data
```

Two deliberate exclusions:

- **The `.spj` is never opened.** It is a large opaque bundle and `score/`
  already holds everything needed.
- **`score/measures.data` is never read.** The exporter creates it and never
  fills it — it is always 0 bytes. An alignment belongs to a *performance*,
  not to a score.

**Nothing is ever written into a package.** All output goes to the job folder.
A package's `reference/` holds the recording alignment is measured against;
overwriting it would destroy the reference.

### One alignment mode

`reference` — the upload is aligned audio-to-audio against the package's own
reference recording, by VideoScoreSync. A package without `reference/` cannot
be rendered.

An `auto` mode once aligned against the score itself through
`music_line_extractor`. It is gone: `pipeline.MODES` is `(REFERENCE,)`, and
`app/autosync.py` was removed with it.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit the paths
python tools/doctor.py        # checks config, tools and corpus
python tools/selftest.py      # the checks CI runs, on this machine
python run.py                 # http://127.0.0.1:5000
```

`ffmpeg` and `ffprobe` are external binaries, not Python packages. Without
`cairosvg`, `.svg` bands cannot be rasterised and the `.png` twins beside them
are used instead — installing it lets vector bands render at exactly the
output resolution.

**On Windows, run everything under the app's interpreter** — the
`VideoScoreSync` conda environment, which has Flask and cairo. The
`2026liszt` environment has torch and is what the two runners are pointed at;
running the app there fails on `import flask`, and `selftest.py` says so
rather than leaving it to be guessed.

`.env.prod` is the Linux configuration, committed and holding no secrets. It
sets **every** variable, deliberately: `settings.py` falls back to
`.env.example`, which carries Windows paths, and on Linux `os.pathsep` is `:`
— so one missing value turns `C:\scores` into two roots named `C` and
`\scores`. A check enforces that the two files declare the same variables.

---

## Checking it

| | |
|---|---|
| `tools/selftest.py` | what CI runs, on Windows **and** Linux. No ffmpeg, cairo, torch or network needed. |
| `tools/doctor.py` | is this machine configured, and what packages can it see |
| `tools/try_identify.py` | recognition only, on a real recording |
| `tools/try_render.py` | the whole slice with no browser: recognise, align, render |
| `tools/compare_alignments.py` | two `measures.data` for one recording, measure by measure |

CI runs on `ubuntu-latest` **and** `windows-latest`, and that matters more
than it looks. Development is Windows and the servers are Linux, and the
differences between them do not announce themselves — they surface as an
empty result rather than an error. One shipped: `pathlib.Path` treats a
backslash as an ordinary character on Linux, so every piece the recogniser
identified on the server resolved to no score, at full confidence, with no
error anywhere. See `docs/deployment-log.md` §9.
