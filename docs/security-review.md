# Adversarial security review — VideoSync_webapp (chopin.weefeen.com)

**Target:** `C:\ZZ_perso\weefeen\PT\RD\VideoSync_webapp`, branch `feature/recognition`.
**Reviewed at:** committed `HEAD 7396ec2` plus the uncommitted working-tree changes present during the review (`app/jobs.py`, `app/routes.py`, `app/store.py`, `app/render.py`, `run.py` are all modified on disk — the queue/store refactor and the upload duration cap are mid-flight). Line numbers cite a frozen snapshot taken during the review; they may drift as that work lands.
**Method:** read every Python module, both front-end bundles, the deploy pipeline and the design doc; live-verified ffprobe behaviour, Python `email`/`smtplib` header handling, and Flask's `get_json` content-type gating under the app's own interpreter (VideoScoreSync env, Flask 3.1.0 / Werkzeug 3.1.3). Read-only neighbours (`VideoScoreSync`, `music_finrgerprint`, `weefeen`) inspected for blast-radius only; findings there are marked **ask the owner** and were not changed.
**Nothing in any repo was modified.**

---

## Executive summary

The honest headline: **the biggest real risk is not a dramatic exploit — it is unmetered cost and abuse of a free, unauthenticated, expensive endpoint.** Bots burning GPU/CPU and disk, and the service being used as a free mail cannon against third parties, are the things that will actually happen. The code is unusually careful for its stage (safe path handling, no `shell=True`, no `send_from_directory` traversal, secrets kept out of git, the download filename taken from our own library, mail body free of user text). The refactor in flight closes two of the DoS holes (a real render queue instead of thread-per-request, and a 25-minute duration cap enforced at upload). What remains before public exposure is a short, specific list.

**Must fix before going public (in rough priority order):**
1. **No bot check of any kind.** One script can spend GPU and CPU all day within the (generous) rate limits. Add Cloudflare Turnstile at submit/upload. This is the top risk. (§F1)
2. **Mail fan-out to arbitrary third parties.** `notify` accepts `"a@x.com, b@y.com"` — a comma list passes the `"@"` check and Python's `send_message` delivers to *all* of them, counted as one against the cap. Anyone can make weefeen.com email anyone. Reputation and abuse risk. (§F2)
3. **`run.py` (Werkzeug dev server) must never face the internet**; serve under gunicorn behind Apache, as the design already says. (§F3 — planned, verify it happens.)
4. **Apache must *overwrite* `X-Forwarded-For`, and `TRUST_PROXY=true` must be set.** Get this wrong in either direction and rate limiting is either bypassable or applies to everyone at once. (§F4)
5. **Decode of untrusted media (ffmpeg/ffprobe/librosa/torch) has no containment.** Run the pipeline as an unprivileged user; keep decode/align/identify off the always-on box (the per-batch compute instance in the design is the right answer). (§F5)
6. **`render.probe()` has no subprocess timeout** — a crafted file that hangs ffprobe ties up a request thread at the upload gate. (§F6)

**Can wait / lower priority:** CSRF hardening on upload+identify (§F7), rate-limit key weakness for IPv6/NAT + `limits.json` unbounded growth (§F8), 48-bit job ids and filename disclosure via `/status` (§F9), unescaped score-metadata sinks in the front end (§F10), ffmpeg-stderr tail returned to the client (§F11).

**What is already right** is genuinely right — see the dedicated section so you don't churn it.

---

## Findings, ordered by risk (likelihood × impact)

### F1 — No bot mitigation on a free, expensive, unauthenticated service — **HIGH**
**Evidence:** No CAPTCHA/proof-of-work/Turnstile anywhere (`grep` for turnstile/captcha/hcaptcha/proof-of-work across `app/`, `tools/`, templates and JS returns nothing). The only brakes are `app/limits.py` sliding windows, keyed on client address and email.
**Realistic attack:** A script uploads a 20-second clip and calls `POST /api/jobs/<id>/identify` (`app/routes.py:308`) in a loop. Each identify is ~30–55 s of GPU (`app/identify.py:16-20`) serialised on one card (`_gpu` semaphore, `app/identify.py:43`). The default limit is 8 identifies/hour *per address* (`app/limits.py:59`); from a handful of IPs (or a single customer /64 of IPv6, see F8) that is a full-time GPU-starvation and cost engine that never touches a real exploit. Render is capped at 3/week per address+email but upload+identify — the GPU cost — is hourly and cheap to reach.
**Fix:** Add **Cloudflare Turnstile** (privacy-friendly, invisible for most musicians, free, and you are already fronting with Apache so a Cloudflare hop is natural). Verify the token server-side in `api_upload` before the file is probed, or at latest in `api_identify` before the semaphore is taken. Turnstile beats hCaptcha here on UX; a hashcash-style PoW is a weaker deterrent against a determined GPU thief and annoys honest users on phones. Place it **at submit of the upload**, not only after a threshold — the threshold is exactly the GPU spend you are trying to protect. This has been flagged repeatedly and is still absent; it is the single most important item.

### F2 — Mail can be sent to arbitrary third parties, and to several at once — **HIGH**
**Evidence:** `app/notify.py:43-45` guards only with `if "@" not in address`. `message["To"] = address` (`:64`) then `server.send_message(message)` (`:83`). The render email comes straight from the request: `job.email = str(body.get("email",""))` (`app/routes.py:533`). Live-verified under the app's interpreter:
- `"a@x.com, b@y.com, c@z.com"` passes the `"@"` test and `smtplib.send_message` resolves **three** recipients (`getaddresses` → all three). The mail cap keys on the *whole string* (`limits.allowed("mail_email", address)`, `app/jobs.py:361`), so a comma list is one cap hit but N deliveries — a built-in amplifier.
- CRLF header injection is **not** possible: Python's `EmailMessage` raises `ValueError: Header values may not contain linefeed or carriage return characters`. Good.

**Realistic attack:** Anyone finishes a render (3/week/address is easy) and types a victim's address — or a comma-separated list of victims — as the destination. The victim receives mail *from weefeen.com's own domain* (`SMTP_FROM`, `app/settings.py:294`). Repeated across renders this is a targeted-harassment / snowshoe-spam vector that burns weefeen.com's sending reputation (SPF/DKIM/DMARC all pass because it *is* weefeen sending), risking the production site's own deliverability.
**Fix:**
- Reject any address containing a comma, whitespace, or more than one `@`; validate a single RFC-ish address (the front end already uses `^[^@\s]+@[^@\s]+\.[^@\s]+$` at `svs-wire.js` submit — mirror that server-side).
- The right structural fix is **the mail goes only to an address that has proved control of that inbox** — i.e. the recipient is the person who uploaded, and you cannot verify that without login. Practical middle ground: keep the page-shows-the-link path (already built, `watchRender`/`deliveryBox` in `svs-wire.js`) as the primary delivery, and treat email as opt-in convenience with a low per-address weekly cap **and** a global daily cap (both already exist: `mail_email` `app/limits.py:74`, `mail_total` `:75`). Lower `LIMIT_MAIL_PER_DAY` (200 today) until you trust the traffic. Consider a one-time confirmation ("click to receive the link by email") before the first send to a new address.

### F3 — Development server must not be the public server — **HIGH (deployment discipline)**
**Evidence:** `run.py:42` calls `app.run(..., threaded=True)` (Werkzeug dev server). The design (`docs/queue-design.md §15.1`) and `deploy/deploy.sh` comments say the real server is **gunicorn behind Apache** (`ProxyPass / http://127.0.0.1:5057/`). But there is no gunicorn config or `wsgi.py` in the repo yet, and the health check hits `127.0.0.1:5057` (`deploy/deploy.sh:34`).
**Realistic attack:** The Werkzeug dev server is single-purpose, not hardened, and is trivially held open by slow clients (slowloris) or crashed by malformed requests; it also carries the interactive debugger if `debug` is ever on. On a box shared with production Symfony, that is unacceptable.
**Fix:** Ship the gunicorn invocation (a `deploy/supervisor/vsw.conf` running `gunicorn -w N --timeout 3600 app.wsgi:app` per the design) and confirm `run.py` is used only for local dev. Set Apache `ProxyTimeout 3600` and a matching gunicorn `--timeout` for large uploads (design §10.2). Never run with `--debug` in production (the Werkzeug console would be an RCE; today it is off by default, `run.py:21`).

### F4 — Rate-limit trust model depends entirely on the Apache config — **HIGH if misconfigured**
**Evidence:** `app/limits.py:174-185`: `client_key` uses `request.remote_addr` unless `TRUST_PROXY` is truthy, in which case it takes **the first** `X-Forwarded-For` entry (`forwarded[0].strip()`). Default `TRUST_PROXY=false` (`.env.example`).
**Realistic attack, two ways to get it wrong:**
- **`TRUST_PROXY` left false behind Apache:** every request looks like `127.0.0.1` (the proxy), so the *entire internet shares one bucket* — 8 uploads/hour total, a self-inflicted DoS, and one abuser locks out everyone.
- **`TRUST_PROXY=true` but Apache *appends* to `X-Forwarded-For` instead of overwriting:** a client sends `X-Forwarded-For: <anything>` and `forwarded[0]` is attacker-chosen, so each request can claim a fresh identity → unlimited quota, and an attacker can also *poison* a chosen victim IP's counters.
**Fix (exact):** In the `chopin.weefeen.com` vhost:
```apache
RemoteIPHeader X-Forwarded-For
RemoteIPInternalProxy 127.0.0.1
# Ensure the header the app reads cannot be client-controlled — overwrite, never append:
RequestHeader set X-Forwarded-For "%{REMOTE_ADDR}s"
ProxyAddHeaders Off
```
and set `TRUST_PROXY=true` in the server `.env`. The design already states this (`docs/queue-design.md §15.1`, "overwrite, never append"); the review confirms `client_key` will honour exactly one entry and trusts it verbatim, so the directive is load-bearing. Better still, once `mod_remoteip` rewrites `REMOTE_ADDR`, prefer reading `request.remote_addr` (now the real client) over the raw header — but the header-overwrite approach above is sufficient and matches the current code.

### F5 — Untrusted media decode has no containment — **HIGH impact, needs deployment controls** (partly *ask the owner* for the read-only engines)
**Evidence:** Every upload is fed to large C/C++ parsers with long CVE histories, all on attacker-supplied bytes:
- `ffprobe` at upload (`app/render.py:271-280`, `app/identify.py:132-149`) and `ffmpeg` at render (`app/render.py:433-440`, `_band_strip` and the main encode).
- `librosa`/`soundfile`/`audioread` inside the alignment subprocess — `sync_runner.py:148` calls `audio2chroma`, which is `VideoScoreSync/services/audio_to_chroma.py` → `api_audio/chroma.py:audio_to_chroma_chunked` → `librosa.load` on the raw upload (**read-only repo**).
- `librosa.load` + `torch` + `piano_transcription_inference` inside the recognition subprocess — `identify_runner.py:70` → `weefeen_id.aggregate.identify_aggregated` → `librosa.load` (`music_finrgerprint/src/weefeen_id/aggregate.py:138`, **read-only repo**).

Containment today: **none** beyond per-call timeouts. A memory-safety bug in any of these parsers on a malicious container is code execution as the web user.
**Realistic attack:** A crafted MKV/MP4/WebM triggers a known or zero-day parser bug in ffmpeg or an audio backend and runs code in the process that also holds your SMTP credentials and sits next to the production Symfony site.
**Fix (deployable here):**
- **Run everything as a dedicated unprivileged user** (`vsw`), owning nothing but `/mnt/volume_1/vsw` — the design already specifies this (`docs/queue-design.md §15.1`); make sure the supervisor programs actually run as `vsw` and **not root** (the neighbour's own consumers run as `user=root` in `weefeen/etc/supervisor/weefeen_prod.ini:8` — do not copy that).
- **Do the decode on the throwaway compute instance, not the always-on web box.** The design's create-per-batch, destroy-after singleton is a genuine containment property worth naming: a compromise dies with the instance and never had persistent secrets (per-instance short-lived creds, design §9.3). Keep recognition and alignment there; the web box should only probe (seconds) and proxy.
- **Reduce ffmpeg surface:** pass `-nostdin`, drop protocol whitelists you don't need (`-protocol_whitelist file` only — the live test confirmed ffprobe already refuses HLS/`m3u8` fetch on a non-standard extension and refused a disguised playlist, so SSRF-via-playlist is not currently reachable, but pinning the whitelist makes that guarantee explicit). Consider `-analyzeduration`/`-probesize` caps.
- **seccomp / systemd hardening** on the worker programs: `NoNewPrivileges=yes`, `PrivateTmp=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `MemoryMax=`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, and a seccomp profile that blocks `ptrace`/`mount`/etc. These are cheap under supervisord+systemd and bound the blast radius.
- **Resource limits:** `MAX_DURATION_MINUTES=25` now enforced at upload (`app/routes.py:441`) is the key one and caps the aligner's quadratic DTW memory (design §1.2: 25 min ≈ 5.5 GB). Add `nice`/`ionice` and a cgroup `MemoryMax` so a decode cannot OOM the box.

**Ask the owner (read-only repos):** `music_finrgerprint` and `VideoScoreSync` load attacker audio with librosa/torch and cannot be patched by us. Note one latent issue: `VideoScoreSync/api_audio/video_to_audio.py:19-20` builds an ffmpeg command by string concatenation and runs it with `shell=True`. It is **not** on this app's path (the webapp feeds the media straight to `audio2chroma`/librosa, never `video_to_audio`), and the filenames it would see are our own job-id paths, so it is not currently exploitable — but if the pipeline is ever rerouted through it, an attacker-influenced path becomes shell injection. Flag it to the engine owner.

### F6 — `render.probe()` has no subprocess timeout — **MEDIUM**
**Evidence:** `app/render.py:274-277`: `subprocess.run([... ffprobe ...], capture_output=True, text=True, check=True)` — **no `timeout=`**. This runs at the upload gate (`app/routes.py:427`) and again at render. Contrast `app/identify.py:137` which correctly passes `timeout=60` to its ffprobe.
**Realistic attack:** A file crafted so ffprobe blocks (pathological container, huge stream count, a demuxer that spins) ties up a request-handling thread indefinitely; a few of these exhaust the worker pool and wedge the site — cheap to upload, ruinous to process, and it never finishes to hit any downstream cap.
**Fix:** Add `timeout=` (e.g. 30 s) to the `subprocess.run` in `render.probe`, mirroring `identify.probe_media`. Also cap stream/probe cost with `-analyzeduration` and `-probesize`. On timeout, delete the upload and return 415/413. (The MKV-with-thousands-of-streams and absurd-frame-rate cases land here too; note `fps` is read but not sanity-bounded at `app/render.py:292-299` — a wild `avg_frame_rate` flows into `-r str(round(fps))` at `:481` and the strip length, worth a clamp.)

### F7 — CSRF: cross-origin can burn quota and GPU, but not render or mail — **MEDIUM**
**Evidence:** No CSRF token, no auth, no `Origin`/`Sec-Fetch-Site` check anywhere (grep confirms). Live-verified Flask behaviour under the app interpreter:
- `POST /api/jobs/<id>/render` (`app/routes.py:499`) reads the body only via `request.get_json(silent=True)` — which returns `None` for `text/plain`, form, and multipart bodies, and parses JSON only for `application/json`. A cross-origin `application/json` POST requires a CORS preflight, which will fail (no CORS headers are sent). So **render — and therefore the mail send — is effectively CSRF-resistant** as written. Good, and worth stating so it isn't "fixed" into something weaker.
- `POST /api/upload` (`app/routes.py:394`) uses `request.form`/`request.files` (multipart) — a **simple** request needing no preflight. A malicious page can make a visitor's browser upload the attacker's blob with `rights=true` (the front end sends exactly this, `svs-wire.js` `postUpload`), consuming the visitor's upload quota and triggering ffprobe.
- `POST /api/jobs/<id>/identify` (`app/routes.py:308`) is a bodyless POST; chained after a CSRF upload it spends GPU on the victim's quota.
**Realistic attack:** An embedded page silently uploads+identifies using visiting musicians' IPs, spreading GPU abuse across many innocent addresses (and defeating per-IP limits). Lower impact than F1 because it needs victim traffic, but it amplifies F1.
**Fix:** Check `Sec-Fetch-Site` (reject `cross-site` on the state-changing POSTs) and/or validate `Origin`/`Referer` against `https://chopin.weefeen.com` in `api_upload`, `api_identify`, and `api_render`. Tokens are overkill for an app with no sessions; **Origin/Sec-Fetch checking is the proportionate choice** — say so. Turnstile (F1) also blunts this.

### F8 — Rate-limit key granularity and `limits.json` growth — **MEDIUM**
**Evidence:** `client_key` returns a single address string (`app/limits.py:185`); counters are keyed `f"{bucket}:{key}"` (`:120,130`).
- **IPv6:** a residential IPv6 customer typically gets a whole /64 (or larger). Per-*address* limiting is meaningless — an abuser rotates through 2^64 addresses; an innocent household could also be split across addresses. Per-IPv4 also punishes shared NAT (an entire school/office behind one IP shares 8 uploads/hour).
- **Unbounded growth / O(n) amplification:** `record()` only prunes when `len(self._hits) > 500` and only drops *empty* lists (`app/limits.py:136-137`); non-empty keys within their window are never evicted. Every `record()` re-serialises the **entire** dict and rewrites `limits.json` synchronously under a lock (`_save`, `:101-109`). An attacker minting fresh keys (rotating IPv6, or spoofed XFF if F4 is misconfigured) can push the map to hundreds of thousands of entries within the 1-hour upload window, so each request pays an ever-growing JSON serialize+fsync — a self-amplifying slowdown, and the file can grow to many MB.
**Fix:**
- Normalise IPv6 keys to the **/64** (and optionally IPv4 to /24) before keying, so a customer prefix is one bucket.
- Bound the store: cap total keys, evict oldest, and don't rewrite the whole file on every hit (write periodically or use per-key expiry). At minimum, prune all keys whose newest timestamp is outside the largest window, not just empty ones.
- This weakness is *why F1 matters more*: rate limits are a speed bump, not a wall — the design's own `limits.py` docstring says as much (`app/limits.py:12-14`).

### F9 — Result access control: 48-bit ids and filename disclosure — **LOW–MEDIUM**
**Evidence:** Job id is `uuid4().hex[:12]` = 48 bits (`app/jobs.py:440`). `api_download` (`app/routes.py:561`) gates only on id + `state=="done"` + retention window; the id is the sole capability protecting one person's personal recording from another. No enumeration endpoint exists (`registry.all()` / `store.query` are **not** reachable over HTTP — confirmed no route calls them). `job.public()` (returned by `/status`, `/events`, `/identification`) does **not** include the email (good), but it does include `name` — the visitor's **original uploaded filename** (`app/jobs.py:136`), for any guessed id.
**Assessment:** 48 bits is not practically enumerable over the network (2^48 with per-request cost and rate limits), so it is *adequate* as a bearer token, but it is a bearer token — anyone who obtains the id (shared link, forwarded email, proxy/browser history, the `/status` polling URL in logs) gets the video and the uploader's filename. Ids appear in: the emailed link (unavoidable), server logs (`notify.py:87` logs address+id; `jobs.py` logs ids), and the browser (polling/download URLs). No `Referer` leak to third parties was found because the download is same-origin and click-driven.
**Fix (proportionate):** Use the full `uuid4().hex` (128 bits) — it costs nothing and removes the question entirely. Don't return the visitor's original filename in `public()` unless the page needs it (it is shown in the UI, so if kept, accept it as the uploader's own data). Set `Referrer-Policy: no-referrer` and `Cache-Control: private, no-store` on the download response. Keep ids out of any access log that is shipped off-box.

### F10 — Unescaped server data in front-end HTML (stored-XSS sinks) — **LOW (operator-trust data today)**
**Evidence:** `esc()` (`svs-wire.js:337`) escapes only `< > &` — not quotes — and is **not** applied on every path:
- `app/static/svs/svs-min.js:60` — `scoreSource()` builds `... First edition: ${w.ppr}, ${w.ppp} ...` into `innerHTML` with **no `esc()`**. `ppr`/`ppp` come straight from the score's Humdrum `PPR`/`PPP` header (`app/routes.py:126-127` ← `package._read_score_metadata`, `app/package.py:287-307`).
- `app/static/svs/svs-min.js:114-115` — `manualList()` interpolates `${w.t}, ${w.op}` unescaped (title `OTL` / opus `OPS` from the same headers). The wired `svs-wire.js` `candBtn` *does* `esc(w.t)` — so the codebase is inconsistent about the same data.
- Attribute contexts use unescaped values (`data-p="${w.id}"`, `src="${w.band}"`); `w.id` is the package folder name. `esc()` wouldn't help in an attribute anyway (no quote escaping).
**Assessment:** All of this data originates from **score files the operator installs** (NIFC Chopin first editions), not from any visitor input — so this is low risk *today* and the brief's framing is correct. It bites the day a score package from a less-trusted source is installed, or if a filename/header ever carries a `<script>` or a quote. There is also **no Content-Security-Policy** anywhere (grep confirms), so any such injection runs unconstrained.
**Fix:** Escape all interpolated server strings consistently (extend `esc()` to cover `"`/`'` and apply it in `scoreSource` and `manualList`), and add a CSP header (`default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`) — note the page loads Google Fonts, so allow `fonts.googleapis.com`/`fonts.gstatic.com`. CSP also mitigates F7-style injection.

### F11 — ffmpeg stderr tail returned to the client — **LOW**
**Evidence:** `render._run` raises `ToolFailed`, whose message is `f"{what} failed:\n" + last 6 stderr lines` (`app/render.py:429-430`), and `pipeline.run` re-raises it as `PipelineError(str(exc))` (`app/pipeline.py:123`), which `api_render`/the job's error field surface to the page. The full command is logged server-side (good, `app/render.py:438-439`) and **not** put in the visitor message (good). `identify`/`sync` classify stderr into safe messages and mostly avoid leaking (`app/identify.py:254-262`, `app/sync.py:197-217`), though `_failure` can append the last stderr line.
**Assessment:** ffmpeg's stderr tail can contain internal file paths (our own job-folder paths) and filter-graph internals — minor structure disclosure, not credentials. `settings.problems()` and `tools/doctor.py` print interpreter/score paths but are **CLI-only and not reachable over HTTP** (confirmed — no route invokes them). Low risk.
**Fix:** Return a generic "the video could not be rendered" to the client and keep the stderr tail in logs only. Confirm `debug=False` in production so Flask never returns tracebacks.

---

## What is already right (do not churn these)

- **No path traversal.** `/app/<path:filename>` uses `send_from_directory` (safe_join-backed) (`app/routes.py:76`); `/api/library/<name>/band` resolves via an exact dict lookup in the pre-scanned catalogue (`library.find`, `app/library.py:159-161`), so an attacker-controlled `name` is never a filesystem path. The download name is sanitised from our own library, never the visitor's filename (`_safe_stem`, `app/routes.py:583`; `app/paths.py:112-116`). Uploads are stored under a job-id name, not the visitor's filename (`app/routes.py:421-424`).
- **No `shell=True`, no `os.system`, no argument injection** in this repo. Every subprocess uses an argument *list* (`app/identify.py:210-216`, `app/sync.py:118-126`, `app/render.py`), so user-derived paths are single argv elements and cannot inject flags or shell metacharacters. Runners take fixed `--flags`; user data reaches them only as the media path (a real file we wrote) and the score-package path (from our own catalogue).
- **Mail body carries nothing the uploader typed** (`app/notify.py:8-10,59-70`) — piece name comes from our library; performer/title/filename never appear. Combined with the CRLF rejection in Python's `email` lib, classic header injection is closed. (The recipient-fan-out of F2 is the remaining gap.)
- **`TRUST_PROXY` defaults OFF** so `X-Forwarded-For` is ignored unless explicitly enabled (`app/limits.py:181`, `.env.example` `TRUST_PROXY=false`).
- **Secrets are clean.** `.env` is gitignored (`.gitignore:2`) and was **never** committed on any branch (`git log --all -- .env` empty). `.env.example` ships empty `SMTP_PASSWORD`/`SMTP_USER` (verified length 0). The only secret-shaped strings in history (`LINODE_TOKEN`, `SMTP_PASSWORD`) are **empty placeholders** in `.env.example`/`docs/queue-design.md` (verified: token value length 0, it's `LINODE_TOKEN=` followed by a comment). No live secret is in git history.
- **Deploy pipeline is careful.** `deploy.yml` runs only on `main`/manual, never on `pull_request` (`.github/workflows/deploy.yml:22-31`), pins the host key from a secret (`:57-61`), and uses `ssh-agent` (no key on disk). `deploy.sh` runs on the shared box with `set -euo pipefail`, rsyncs with `--exclude .env --exclude var`, touches **only** its own supervisor group (`deploy.sh:16,75-78`), never `docker prune`/`compose down -v`/port-5672 kills, and refuses to delete the release `current` points at (`:105`). `$REL` is a `date+sha` string, not user input. CI (`ci.yml`) only imports/round-trips; no deploy on PR. **Ask the owner:** the *read-only* `VideoScoreSync/.github/workflows/docker-CICD.yml` deploys on `push` **and `pull_request`** to a different host and runs `docker system prune -af --volumes` + `docker rm -f rabbitmq` — the exact destructive pattern this repo avoided; confirm that repo's CI cannot target the shared chopin box.
- **The compute-instance model is a real containment win** — created per batch, destroyed after, no persistent secrets baked in (design §9.1-9.3). Name it as such in the threat model.
- **The in-flight refactor closes real DoS holes:** a genuine SQLite-backed queue with one worker replaces thread-per-request (`app/jobs.py`, `app/store.py`), so N simultaneous uploads no longer become N simultaneous encodes; and `MAX_DURATION_MINUTES=25` is now enforced at upload (`app/routes.py:441`), capping both encode time and the aligner's quadratic memory. Let this land.

---

## Prioritised checklist

### Before going public (blockers)
- [ ] **F1** Add Cloudflare Turnstile; verify server-side before probe/GPU (upload + identify).
- [ ] **F2** Reject multi-recipient / malformed email addresses server-side; lower `LIMIT_MAIL_PER_DAY`; make email opt-in with the page-link as primary delivery; consider first-send confirmation.
- [ ] **F3** Serve via gunicorn behind Apache; never expose `run.py`; `debug=False`.
- [ ] **F4** Apache `RemoteIPHeader` + `RequestHeader set X-Forwarded-For "%{REMOTE_ADDR}s"` + `ProxyAddHeaders Off`; set `TRUST_PROXY=true`. Verify a client-supplied `X-Forwarded-For` cannot change the keyed identity.
- [ ] **F5** Run workers as unprivileged `vsw` (not root); keep decode/align/identify on the throwaway compute instance; systemd/seccomp hardening + `MemoryMax`; `-nostdin` and a pinned `-protocol_whitelist file` on ffmpeg/ffprobe.
- [ ] **F6** Add `timeout=` + `-probesize`/`-analyzeduration` to `render.probe`; clamp `fps`.
- [ ] Confirm the queue refactor and duration cap are committed and working (they were uncommitted at review time).

### Soon after (hardening)
- [ ] **F7** Origin / `Sec-Fetch-Site` check on `api_upload`, `api_identify`, `api_render`.
- [ ] **F8** Key rate limits on IPv6 /64 (and IPv4 /24); bound and expire `limits.json`; stop rewriting the whole file per hit.
- [ ] **F9** Use full 128-bit `uuid4().hex` for job ids; `Referrer-Policy: no-referrer` + `Cache-Control: private, no-store` on downloads.
- [ ] **F10** Escape all server strings in the front end consistently; add a CSP header (allow the Google Fonts hosts).
- [ ] **F11** Generic render-failure message to clients; stderr tails to logs only.
- [ ] Add baseline security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` / CSP `frame-ancestors 'none'`).

### Ask the owner (read-only repos — cannot change, must coordinate)
- [ ] `VideoScoreSync/api_audio/video_to_audio.py:19-20` `shell=True` with concatenated paths — not on the current path, but confirm nothing reroutes through it.
- [ ] `VideoScoreSync/.github/workflows/docker-CICD.yml` destructive deploy on push+PR — confirm it cannot touch the shared chopin box / broker.
- [ ] `music_finrgerprint` / `VideoScoreSync` decode attacker audio with librosa/torch and cannot be patched by us — the containment in F5 (unprivileged user + disposable compute instance) is the compensating control.

### Shared-box blast radius (design §15.1 is sound; verify it is implemented)
- [ ] Our supervisor programs run as `vsw`, owning only `/mnt/volume_1/vsw` — **not root** (the neighbour's consumers run as root; don't inherit that).
- [ ] RabbitMQ: separate **`vsw` vhost**, scoped users (`vsw_web`, per-instance `vsw_c_<id>`), `max-length`/connection/queue limits on `^vsw\.` so a runaway queue cannot trip the broker's global memory alarm and block the Symfony site's publishers.
- [ ] Broker listener for us on the VLAN address + TLS only; nothing of ours on `0.0.0.0` or Linode's shared private IP (design §9.2).
- [ ] Cloud Firewall: inbound 22 (admin IP), 80/443 only.

---

## Update — 2026-09-11 review pass (fixes applied, proven, committed)

A second review of the same surface, this time firing each finding at a
local instance (no credentials wired to it) to prove the primitive, fixing
it, and re-running the same probe. Nothing was fired at production.

**Fixed and proven this pass:**

- **Rate-limit key forgeable (was the root of F8's severity).** `client_key`
  read `X-Forwarded-For[0]` — the leftmost entry, which the client writes —
  so rotating the header reset every limit. Proven: 10 uploads with 10 forged
  first-entries all passed an 8/hour cap. Now keys on the LAST entry (the peer
  our own Apache appends), and `deploy/install-web.sh` also has Apache
  overwrite the header (`RequestHeader set X-Forwarded-For "%{REMOTE_ADDR}s"`).
  Re-tested with the production header shape: blocks at request 9.
- **Arbitrary local-file read via `background_path`** (new; not in the first
  review). A render could name any file `vsw` could read — every other
  visitor's upload and video — and ffmpeg composited it into a downloadable
  output. `_style_from` now resolves the path only from our config; the
  request cannot set it.
- **Fail-open duration cap.** Code default and BOTH env templates set 25 on a
  box that OOMs past ~11 minutes. Now 8 everywhere.
- **Disk-fill DoS.** Uploads are kept forever and nothing reclaims them.
  `MIN_FREE_DISK_GB` now refuses an upload with 507 before writing if it would
  breach the floor — fails closed, deletes nothing.
- **F7 CSRF** — `_cross_site` refuses a cross-origin caller on upload,
  identify and render.
- **F9 job ids** — full 128-bit `uuid4().hex`; downloads send
  `Cache-Control: private, no-store`.
- **F8 `limits.json` growth** — `_prune` now sweeps stale timestamps across
  ALL keys and drops the empty ones (the old pass reclaimed nothing).

Each of the above has a regression check in `tools/selftest.py` (37 total).

**Still open, and why:**

- **F1 bot check** — still the top item, and now clearly the lever behind the
  remaining resource-exhaustion findings. Needs Turnstile keys from the owner.
- **The live `.env`** must set `MAX_DURATION_MINUTES=8` and add
  `MIN_FREE_DISK_GB=5` by hand — the committed templates are fixed, the
  server's copy is edited on the box.
- **The Apache directive** takes effect only when `install-web.sh` is re-run
  or the line is added to the live vhost and Apache reloaded. The code fix
  already closes the hole; this is defence in depth.
- **F10 CSP / metadata escaping**, **F11 generic failure message** — not yet
  done; low today (operator-installed data), worth a pass.
- **Object-storage credential scoping** before `COMPUTE_ENABLED=true` — the
  key handed to a compute node is still full-bucket read/delete.
- **F5 decode containment** — untrusted media still runs through
  ffmpeg/torch/librosa on the always-on box until rendering moves to the
  disposable compute node.
