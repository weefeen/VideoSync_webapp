# tools/

Four kinds of thing live here, and it is worth knowing which is which before
changing one.

## On the running path — the app calls these

The app runs these as subprocesses under a different interpreter. **They are
not developer scripts: editing one changes what the site does.**

| | |
|---|---|
| `identify_runner.py` | names the piece. Called by `app/identify.py` with `ID_PYTHON`. Reads `music_finrgerprint`, which is read-only. |
| `sync_runner.py` | aligns the recording. Called by `app/sync.py` with `SYNC_PYTHON`. Reads `VideoScoreSync`, which is read-only. |

Both exist because on Windows no single interpreter can host both the web app
and the engines. On Linux one virtualenv runs everything and these are simply
that same interpreter — kept anyway, because this is also the boundary the
compute host will be split along.

## Checks — run these before believing anything

| | |
|---|---|
| `selftest.py` | what CI runs, on Windows and Linux. No ffmpeg, cairo, torch or network. Every check exists because something worked on one platform and silently did nothing on the other. |
| `visitors.py` | who has used this, from where, and what they played — the addresses with their country and city, the pieces with how long they run, and every answer the recogniser gave. Reads the database directly, so it works on a box where the web process is not running. `--who`, `--where`, `--what`, `--recent`, `--json`. |
| `doctor.py` | is this machine configured, and what score packages can it see. Read-only. |
| `compare_alignments.py` | two `measures.data` for one recording, measure by measure. A successful exit code does not tell you an alignment is right. |
| `brokercheck.py` | against a **real** RabbitMQ, by hand, on a machine that has one. `ping` answers in a second without touching ffmpeg, so a wrong URL or a queue whose arguments disagree shows up immediately instead of at the end of a half-hour render. |

## Trying the real thing — needs ffmpeg, a package, and time

| | |
|---|---|
| `try_identify.py` | recognition only, on a real recording |
| `try_render.py` | the whole slice with no browser: recognise, resolve, align, render |
| `make_video.py` | render a *chosen* score from the command line |
| `matrix.py` | one clip across many style combinations, reusing one alignment |

## One-offs

| | |
|---|---|
| `extract_mockup.py` | unpacks a Claude Design standalone HTML export into readable source. Used once per handover; the result is in `design/mockup/`. |
