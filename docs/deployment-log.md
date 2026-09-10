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

## 13. The queue slice, step 2 — the transport seam, proven

`docs/broker-slice.md` step 2: the work leaves `Registry` for
`app/queue/worker.py`, the applier and janitor threads arrive in
`app/queue/webside.py`, and everything travels through a `Transport`. There
is one implementation and it is two in-memory queues, so nothing is
distributed yet — but the whole path is now the one a broker will carry:

    routes -> publish -> transport -> worker -> events -> transport
           -> applier -> ledger -> row

Same probe as §12, on the node:

     elapsed  state     stage
          0s  running   align
         20s  running   strip
         45s  running   encode
        550s  done      done

    output       131,321,451 bytes — byte for byte what §12 produced
    stage record render, attempt 1, done, elapsed 547.9 s   (§12: 550.8 s)

Two structural changes worth knowing about, both of which would have been
bugs later:

- **`store.claim_next` is gone.** The queue is the claim now. Keeping a
  second way to take a job would let two workers reach one render by two
  different routes.
- **`resume()`'s `running -> queued` statement is gone from startup.** It
  was harmless while the worker was a thread in the same process. Once the
  worker is a separate process it is actively wrong: restarting the web side
  during a render would re-queue work that is still going, and hand out a
  second copy of it. Recovery now asks the transport whether it survives a
  restart — only the in-process one says no.

`MAX_CONCURRENT_RENDERS` now counts consumers rather than threads, and
anything but 1 is refused at startup with the reason, because nothing yet
stops two consumers rendering the same task. The guard for that is the
per-attempt record, and it arrives with the broker.

### Checks

Fourteen, both platforms. The new one runs the entire seam with the render
stubbed — publish, consume, report, apply — so the shapes, the ordering and
the ledger's rules are exercised on every push without a broker anywhere.

    win32  python 3.12.7  all 14 passed
    linux  python 3.12.3  all 14 passed

**Confirmed green on GitHub Actions by the owner**, on both runners. Until
that confirmation the matrix had only been proven by running the same script
on the two machines by hand — the workflow file itself had never been watched
executing.

---

## 14. RabbitMQ, on the dev node

### The broker

    apt install rabbitmq-server        3.12.1 on Ubuntu 24.04

**3.12.1 answers the question §13 left open.** `x-consumer-timeout` is a
per-queue argument from 3.12, so nothing has to be changed in a shared
broker's `rabbitmq.conf`. It was not taken on trust: `brokercheck topology`
declares the queues with that argument against the real broker, and the
broker accepted them.

`/etc/rabbitmq/rabbitmq.conf`:

    listeners.tcp.default = 127.0.0.1:5672
    distribution.listener.interface = 127.0.0.1

Both lines are corrections, not defaults. The package binds **5672 and 25672
to 0.0.0.0**, and this node has a public address with `ufw` inactive — so the
broker and the Erlang distribution port were briefly reachable from the
internet. 25672 is protected only by the Erlang cookie. Whoever builds the
production host should check this before anything else.

    rabbitmqctl add_vhost vsw
    rabbitmqctl add_user vsw <generated>
    rabbitmqctl set_permissions -p vsw vsw ".*" ".*" ".*"
    rabbitmqctl delete_user guest        # remove the default account

The password is in `/srv/vsw/broker_password`, mode 600, and in `.env` as
`RABBITMQ_URL`. Neither is in the repository; `.env.prod` carries
`CHANGE_ME`.

### Two processes

    python -m app.queue.worker     consumes vsw.render — the only renderer
    python run.py                  serves, applies events, sweeps

The web process **deliberately does not consume tasks** when a broker is
configured. With the in-process queue it must, because nobody else can reach
that queue; with a broker, consuming as well would put two consumers on one
queue and nothing yet stops both rendering the same task.

    rabbitmqctl list_consumers -p vsw
      vsw.render   (the worker)
      vsw.events   (the web process's applier)

### Proof

    brokercheck ping     a worker took it in 0.07s (1 consumer)

`ping` is answered without touching ffmpeg, so a wrong URL, a missing vhost,
a bad password or a queue whose arguments disagree shows up in a second
rather than at the end of a half-hour render.

It measures the queue draining rather than catching the worker's `pong`, and
that is a correction rather than a preference. The first version listened on
`vsw.events` — which the web process's applier also consumes, so RabbitMQ
round-robined between them and `ping` reported NO ANSWER on a broker that was
working, with both consumers registered. The real hazard was worse than a
false negative: the listener acked whatever it was handed, so running `ping`
during a live render could have swallowed that job's `done` event. The video
would exist, the row would stay `running`, the lease would expire, and the
janitor would render it a second time.

Then a real upload, over HTTP, to the running web process:

     elapsed  state    stage
          0s  running  align    matching the recording to the score
          5s  running  strip    timing bands to the performance
         35s  running  encode   1920x1080 · band 1916x358 bottom
        535s  done     done

    output      131,321,451 bytes — byte for byte what §12 and §13 produced
    record      render, attempt 1, done, elapsed 546.7 s
    row after   done, worker NULL, lease NULL, error NULL, published_at set
    queues      vsw.render 0, vsw.events 0, both dead-letter queues 0

| | elapsed | s/s |
|---|---|---|
| §12, in-process worker thread | 550.8 s | 1.299 |
| §13, through the transport seam | 547.9 s | 1.292 |
| **§14, through RabbitMQ, separate processes** | **546.7 s** | **1.289** |

A broker between two processes costs nothing measurable, which is the answer
to the only real objection to putting one there.

**Mid-render, `vsw.render` held 1 message with 1 consumer.** That is the ack
discipline working: the delivery stays unacked until the video exists, so a
worker that dies gets it redelivered rather than losing the job. It is also
exactly the case that RabbitMQ's 30-minute `consumer_timeout` default would
have broken, for the longest uploads only.

### Not yet done here

- **Neither process is supervised.** Both were started with `setsid` by hand
  and neither comes back after a reboot. systemd units belong with gunicorn.
- **The web process logs nothing from the queue** — `run.py` never calls
  `logging.basicConfig`, so the applier's and janitor's messages go nowhere.
  The worker configures its own and is readable.
- The kill-the-worker-mid-render and stop-the-broker checks from
  `broker-slice.md` §9 have not been run.

---

## 15. Redelivery, proved by killing a worker

A broker redelivers whenever it is in any doubt, and that is the mechanism
that makes a dead worker recoverable — so redelivery is not something to
prevent, it is something the work has to survive. Two cases, both run against
the real broker.

### A worker killed mid-encode

`kill -9` on the worker, forty seconds into the encode:

| | |
|---|---|
| The broker | saw the connection die and **requeued**: `vsw.render` went to 1 message, 0 consumers |
| The row | stayed `running`, naming the dead worker, 573 s of lease left — correct, nothing had reported anything |
| On disk | `…_synced.**part**.mp4`, 6 MB. Without the rename that would look exactly like a finished video |
| A fresh worker | took the redelivered message and **re-ran from the start** — right, because the attempt record said `started`, not `done` |
| The record | two `stage_runs`: the first closed `error / Interrupted`, the second `running` |

    error   23:13:34 -> 23:15:01   elapsed  86 s   Interrupted
    done    23:15:01 -> 23:31:11   elapsed 970 s

### The retry took 970 s against a normal 547 s

**Killing the worker orphans its ffmpeg.** `pkill -9` ends the Python
process; its ffmpeg child is not in the signal's scope and carries on
encoding — to a file the retry has already unlinked, so it writes to a dead
inode and nothing is corrupted, but it holds a core for its remaining eight
minutes. On two cores that is why the retry took nearly twice as long, and
`uptime` still showed a 15-minute load average of 4.86 afterwards.

A process killed with SIGKILL cannot clean up after itself, so this cannot be
fixed inside the worker. **The supervisor has to do it**, and systemd does by
default: `KillMode=control-group` kills the whole cgroup, ffmpeg included.
That turns the systemd units from tidiness into a measured requirement.

`preexec_fn` with `PR_SET_PDEATHSIG` would also work and is deliberately not
used: the worker is multi-threaded and `preexec_fn` is documented as unsafe
in a process with threads.

### A duplicate delivery of finished work

Re-publishing the same task — the same job, the same attempt — is what a
redelivery of an already-finished render looks like:

    attempt 1 of 1e5e4c5b3bbc already done here;
    reporting it again, not rendering again

Nothing was rendered: no new `.part` file appeared, and the job folder still
held only the finished video, its record, `measures.data` and `sync`. On the
web side the repeated `done` changed nothing — **2 stage_runs, not 3, and the
delivered-video count stayed at 4 rather than going to 5.** That count is the
same guard that stops a second email going out about one video.

Both queues drained, nothing dead-lettered.

### What this leaves

A worker that dies mid-render still re-renders from scratch. That is correct
and it is not free: a 25-minute upload costs another half hour. Resuming a
render from the middle is not possible, so the only improvement available is
to lose less by staging the work — which is the per-stage queue split of
`queue-design.md`, not this slice.

---

## 16. systemd, and the orphaned ffmpeg fixed

§15 measured the problem: `kill -9` on a hand-started worker leaves its
ffmpeg encoding for another eight minutes, holding one of two cores, so the
retry the broker correctly redelivered ran at half speed.

It cannot be fixed inside the worker. SIGKILL cannot be caught, so the
process never gets to clean up, and its child is reparented to init and
carries on. **The supervisor has to own it**, which is what systemd's default
`KillMode=control-group` does: everything the unit starts is in the unit's
cgroup, and stopping the unit signals the whole cgroup.

    deploy/vsw-worker.service     the renderer
    deploy/vsw-web.service        serves, applies events, sweeps
    deploy/install-units.sh       installs both, idempotent

Two things ruled out rather than forgotten. `preexec_fn` with
`PR_SET_PDEATHSIG` would also kill the child with its parent, and is unsafe
here: the worker is multi-threaded and `preexec_fn` runs between fork and
exec, where a lock held by another thread can deadlock the child.
`start_new_session` plus killing the group on shutdown handles a graceful
stop and does nothing at all on SIGKILL, which is the case that caused this.

### Proved, not assumed

    worker main : 45607
    encode pid  : 45839   (parent 45607)
    cgroup      : 45607 45839

    kill -9 45607          # the worker only, exactly as in §15

    encode pid 45839 is gone — systemd killed it with the cgroup
    ffmpeg processes whose parent is init: none

The unit restarted, the broker redelivered, and the job picked up again —
after two such kills its record read two `Interrupted` runs and a third
running.

**A false result on the way, worth writing down.** The first attempt at this
check used `pgrep -f ffmpeg` and reported survivors. They were the checking
shell itself: its own command line contained the string "ffmpeg", so it
matched. `pgrep -x ffmpeg` plus a parent check is what the result above uses.
A test that greps for a string it also contains will lie in exactly one
direction — it says the bug is still there.

### What the units also buy

- **Both processes survive a reboot.** Neither did before; both were started
  by hand with `setsid` and would not have come back.
- **Logs go to the journal** — `journalctl -u vsw-worker -f`. The web
  process previously wrote to a file only because it was started with a
  redirect, and before this session it configured no logging at all.
- `Requires=rabbitmq-server.service` on the worker, so it does not spend its
  first minute failing to connect at boot.

`TimeoutStopSec=30` on the worker is a deliberate trade: a render takes up to
32 minutes and cannot resume from the middle, so a stop either waits half an
hour or costs a re-render. It costs the re-render, because redelivery is safe
and an operator who cannot restart a service inside a minute stops
restarting it.

---

## 17. The node became a real deploy target

Until now the node was updated by hand — `git fetch && git merge --ff-only`
as root, then `chown`. That is not what production will do, so it proved
nothing about deployment, and it had already gone wrong once: `settings.py`
sat nine lines behind because individual files were being shipped by tar
while GitHub was unreachable, and only a git comparison caught it.

### What it exposed first

`deploy.sh` built a per-release venv from `requirements.txt` — which has **no
torch and no librosa**. Those had been installed on the node by hand and
written down only in this document.

A release built that way would not have failed loudly. Flask starts,
`create_app()` succeeds, and the old health check called exactly that. It
would have swapped the symlink, reported healthy, and broken on the first
upload — because recognition and alignment run in subprocesses whose imports
nothing at startup touches.

Three fixes: `requirements-engine.txt` pins what the two read-only engine
repositories need; `bootstrap.sh` builds the venv from both files instead of
the venv being a manual step recorded nowhere; and the release check now
imports the engines, not just the app.

### The layout now

    /srv/vsw/
      releases/<timestamp>-<sha>/   one deploy's code
      current -> releases/…         what the units run
      shared/.env                   configuration, mode 600
      shared/var/                   jobs.sqlite, uploads, videos (864 MB)
      venv/                         ONE environment, shared
      scores/  VideoScoreSync/  music_fingerprints/

`/srv/vsw` is the path on every host. Production's storage is on a mounted
volume, and `/srv/vsw` there is a symlink to `/mnt/volume_1/vsw` — one path
everywhere, so one set of unit files rather than two that drift.

**The venv is shared, not per-release.** It is 1.5 GB because of torch, so
five releases would be 7.5 GB of near-identical copies, and its heavy half is
dictated by repositories that do not change when this app's code does.

The move preserved everything: 5 done jobs and 1 uploaded, the finished
videos, and 16 checks passing from inside the release.

### Both paths proved

A deploy, run exactly as the workflow will run it:

    Linking shared state -> Dependencies -> Checking the release can start
    app builds, engines import; 0 configuration warning(s)
    Switching current -> Restarting our units only -> Health check
    Healthy. Deployed 20260910001900-608d5fc

And the safety net, with a release that starts, passes the import check, and
then never serves:

    Health check
    !!! did not become healthy
    Rolling back to 20260910001646-…
    rolled back            site: HTTP 200, units active

**A false test first, worth recording.** The first attempt at breaking a
release changed `run.py`'s default port — which does nothing, because the
unit passes `--port 5000` explicitly. It deployed green and I nearly read
that as "rollback works". A test has to break the thing the check actually
looks at.

### Still by hand

The GitHub workflow cannot run yet: it needs four secrets, and the private
key must be generated on the server and pasted into GitHub by a person.
`deploy/README.md` has the sequence. Until then a deploy is
`rsync` + `deploy.sh` run by hand, which is what the two runs above were.

`/etc/sudoers.d/vsw-deploy` lets `vsw` restart exactly the two units and
nothing else, validated with `visudo -cf`.

---

## 18. GitHub deploys to the node

    current  : 20260910011948-b63d1d9
    units    : active active        site: HTTP 200
    jobs     : 6                    WORK_DIR /srv/vsw/shared/var

The job count is the part worth checking: it was 6 before the deploy and 6
after, so the queue database lives in `shared/` and was not replaced along
with the code. A deploy swaps the code and leaves the data alone.

### What was actually wrong, and how long it took to see it

Four runs failed, each reported in the run summary as nothing but

    Process completed with exit code 1

The cause was **not** the key or the secrets. Those worked from the first
attempt after `SSH_KNOWN_HOSTS` was given every host key type rather than
just the ed25519 one: the auth log showed ten successful `vsw` logins and
the rsync delivered all 107 files. It was failing inside `deploy.sh`,
because two steps in the workflow still named `/mnt/volume_1/vsw` — the
production layout — while the node and `deploy.sh` both use `/srv/vsw`. The
release directory was being created in the right place and then looked for
in the wrong one.

That was found by accident, while adding diagnostics for a different
theory. The diagnostics were chasing the wrong thing.

**What actually located it was watching the server, not reading the
workflow.** A loop on the node checking for a new release directory caught
one appearing while `current` never moved — which places the failure after
the rsync and before the symlink swap, in one observation. That is the
lesson worth keeping: when a remote process reports only an exit code, the
fastest question is what changed on the machine it was talking to.

### Diagnostics kept anyway

The run summary shows annotations; a failing `run:` step contributes only
"exit code 1" to it, and anything the step printed is one click further in,
where it went unread four times. So each step now records where it got to
and a final `if: failure()` step names it, `ssh-add -l` proves an identity
was actually loaded rather than trusting the agent's exit code, and
known_hosts is counted after being written. None of it diagnosed this
failure. All of it will make the next one legible.

### Still manual

The `push:` trigger is still commented out — a deploy is the "Run workflow"
button. Turning it on is one line, now that a real run has been watched
succeeding.

---

## Still to do

- Measure seconds-per-second on the plan production will actually use;
  2 shared cores gave 1.292 and a dedicated 4-core box will differ
- gunicorn in place of the Flask development server. One line in
  `deploy/vsw-web.service` and a dependency; supervision itself is done
  (§16), so this is now only about the server, not about the process
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
