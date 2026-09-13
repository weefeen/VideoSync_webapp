"""The page that says what this machine is doing, and lets you change it.

WHY A PAGE. Lending a machine to the queue was two controls on two
machines: whether `tools/volunteer.py` was running here, and what
`COMPUTE_MODE` said in a .env file on the web box. Neither is visible from
the other, one of them is a keystroke in a scrolling terminal, and the only
way to answer "what is happening right now" was to read a log. That is a
control surface you have to hold in your head, and holding it in your head
is how a laptop sits there believing it is helping while a paid machine
does the work.

So: one page, on this machine, at 127.0.0.1. It answers three questions in
the order they get asked --

    am I taking jobs?          the heading, in words, not a status light
    what is happening now?     the render, its stage, how long it has run
    what if I close this?      the web box's mode, stated and changeable

-- and every control is explicit. Nothing here decides anything on its own.

LOOPBACK ONLY, and that is not a detail. This page can pause a renderer and
change what the production server does with its money, and it has no login
because it is not reachable from anywhere that would need one. Bound to
127.0.0.1, refused otherwise.

No dependency: `http.server` from the standard library, and a page with no
build step. The machine this runs on is somebody's desktop, and a control
panel that needs an install is a control panel nobody opens.
"""
from __future__ import annotations

import html
import json
import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("volunteer.panel")

# The web box, for the one question this page asks about the other machine.
# ssh, not an API: this laptop already holds a key to that box, and the
# alternative is an authenticated admin endpoint on a public site -- a
# login to build and protect so that a page on loopback can read one word.
SERVER = "root@172.104.237.127"
ENV_PATH = "/srv/vsw/shared/.env"

# What the mode may be set to. A WHITELIST, because this value is
# interpolated into a command that runs as root on the production server.
# Nothing typed reaches that line: a value not on this list is refused here.
MODES = {
    "manual": ("Nothing happens",
               "The job waits in the queue until this machine comes back. "
               "Nothing is ever rented, so nothing is ever charged."),
    "auto": ("A machine is rented",
             "Only while this one is not listening. It stands aside the "
             "moment this machine says hello again."),
    "cloud": ("A machine is always rented",
              "Every job goes to a paid machine and this one is ignored, "
              "even while it is running."),
}


class Panel:
    """What the page may ask of the volunteer, and nothing else."""

    def __init__(self, state, pause, resume, stop_after) -> None:
        self.state = state
        self.pause = pause
        self.resume = resume
        self.stop_after = stop_after
        self._mode = {"name": "", "problem": "reading…"}
        self._mode_lock = threading.Lock()

    # -- the other machine ----------------------------------------------
    def mode(self) -> dict:
        with self._mode_lock:
            return dict(self._mode)

    def refresh_mode(self) -> None:
        """Read COMPUTE_MODE off the web box. Never raises."""
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 SERVER, f"grep -E '^COMPUTE_MODE=' {ENV_PATH} || true"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._set_mode("", f"could not reach the web box: {exc}")
            return
        if done.returncode != 0:
            self._set_mode("", (done.stderr or "").strip()[-200:]
                           or "the web box refused the connection")
            return
        name = ""
        for line in done.stdout.splitlines():
            if line.startswith("COMPUTE_MODE="):
                name = line.split("=", 1)[1].strip().lower()
        # Unset means `auto`, which is what app/store.py falls back to.
        self._set_mode(name or "auto", "")

    def set_mode(self, name: str) -> str:
        """Change it on the web box. Returns '' or a reason."""
        if name not in MODES:
            return f"{name!r} is not a mode"
        # The scaler reads this at start-up, so it is restarted; vsw-web
        # holds the same settings object and answers the page that tells a
        # visitor what to expect.
        command = (
            f"grep -q '^COMPUTE_MODE=' {ENV_PATH}"
            f" && sed -i 's/^COMPUTE_MODE=.*/COMPUTE_MODE={name}/' {ENV_PATH}"
            f" || echo 'COMPUTE_MODE={name}' >> {ENV_PATH};"
            f" systemctl restart vsw-scaler vsw-web")
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 SERVER, command],
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not reach the web box: {exc}"
        if done.returncode != 0:
            return (done.stderr or "").strip()[-200:] or "the change was refused"
        self.refresh_mode()
        logger.info("compute mode set to %s on the web box", name)
        return ""

    def _set_mode(self, name: str, problem: str) -> None:
        with self._mode_lock:
            self._mode = {"name": name, "problem": problem}


def serve(panel: Panel, port: int = 5055) -> str:
    """Start the panel on a background thread. Returns its URL, or ''."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass                      # the console belongs to the render

        def _send(self, code, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # Nothing here is for anyone else to frame or cache.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code=200) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:                     # noqa: N802
            if self.path.split("?")[0] == "/":
                self._send(200, PAGE.replace("__PORT__", str(port))
                           .encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.split("?")[0] == "/state":
                self._json({**panel.state(), "mode": panel.mode()})
            else:
                self._send(404, b"no", "text/plain")

        def do_POST(self) -> None:                    # noqa: N802
            path = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                body = {}

            if path == "/take":
                panel.resume()
            elif path == "/hold":
                panel.pause()
            elif path == "/stop":
                panel.stop_after()
            elif path == "/mode":
                problem = panel.set_mode(str(body.get("mode") or ""))
                if problem:
                    self._json({"problem": problem}, code=400)
                    return
            else:
                self._send(404, b"no", "text/plain")
                return
            self._json({**panel.state(), "mode": panel.mode()})

    try:
        # 127.0.0.1, never 0.0.0.0: this pauses a renderer and spends money.
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        logger.warning("could not open the panel on port %d: %s", port, exc)
        return ""

    threading.Thread(target=server.serve_forever, name="panel",
                     daemon=True).start()
    return f"http://127.0.0.1:{port}/"


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------
# Palette and type taken from the site itself (app/static/svs/index.html), so
# the machine that makes the videos does not look like a different product
# from the one that sells them. Fonts are linked but every one has a real
# fallback: this is a local tool and it has to open on a train.
_MODE_ROWS = "".join(
    f'<label class="mode" data-mode="{name}">'
    f'<input type="radio" name="mode" value="{name}">'
    f'<span class="mt">{html.escape(title)}</span>'
    f'<span class="md">{html.escape(detail)}</span></label>'
    for name, (title, detail) in MODES.items())

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lending this machine</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400&family=Inter:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root{
  --paper:#f6f1e8; --ink:#1c1622; --soft:#5a5266; --faint:#9a92a6;
  --hair:rgba(28,22,34,.13); --hair-2:rgba(28,22,34,.07);
  --b1:#381C53; --b3:#663893; --mag:#cc237e;
  --surface:#fffdf9; --tint:#faf7f1;
  --serif:Fraunces,Georgia,"Times New Roman",serif;
  --sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:680px;margin:0 auto;padding:38px 22px 70px}
.lab{font-family:var(--mono);font-size:9.5px;letter-spacing:.19em;
  text-transform:uppercase;color:var(--faint)}
header{display:flex;align-items:baseline;gap:12px;margin-bottom:40px}
.wordmark{font-family:var(--serif);font-weight:400;font-size:15px}

/* the answer to the first question, as a sentence */
h1{font-family:var(--serif);font-weight:300;letter-spacing:-.03em;
  font-size:clamp(30px,5vw,42px);line-height:1.08;margin:0 0 10px;
  text-wrap:balance}
h1 b{font-weight:400;color:var(--b1)}
h1.off b{color:var(--faint)}
.because{color:var(--soft);max-width:48ch;margin:0}

.act{margin:26px 0 0;display:flex;flex-wrap:wrap;gap:10px}
button{font:inherit;font-family:var(--mono);font-size:9.5px;letter-spacing:.19em;
  text-transform:uppercase;border:0;border-radius:2px;padding:14px 24px;
  cursor:pointer;transition:background .18s,opacity .18s}
button:focus-visible{outline:2px solid var(--mag);outline-offset:2px}
/* BOTH STATES, ALWAYS SHOWN. This was one button whose label was the
   ACTION -- "Stop taking jobs" -- so the only way to learn that the other
   state existed was to already be in it. What a control does and which
   way it is set are different things, and a switch says both at once. */
.switch{display:inline-flex;border:1px solid var(--hair);border-radius:3px;
  overflow:hidden}
.switch button{background:none;color:var(--soft);padding:13px 22px}
.switch button+button{border-left:1px solid var(--hair)}
.switch button:hover:not(.on){color:var(--ink);background:var(--hair-2)}
.switch button.on{background:var(--b1);color:#fff}
.switch button.on[data-want="hold"]{background:var(--soft)}
.ghost{background:none;color:var(--soft);border:1px solid var(--hair);
  padding:13px 23px}
.ghost:hover{border-color:var(--ink);color:var(--ink)}
button[disabled]{opacity:.32;cursor:default}

section{margin-top:38px;padding-top:22px;border-top:1px solid var(--hair-2)}
section>.lab{display:block;margin-bottom:14px}

/* the render */
.job{background:var(--surface);border:1px solid var(--hair);border-radius:4px;
  padding:20px 22px}
.job .piece{font-family:var(--serif);font-size:21px;font-weight:400;
  letter-spacing:-.01em;margin:0 0 3px}
.job .meta{font-family:var(--mono);font-size:10px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint);font-variant-numeric:tabular-nums}
.stages{display:flex;gap:5px;margin-top:17px}
.stg{flex:1;text-align:center}
.stg i{display:block;height:3px;border-radius:2px;background:var(--hair);
  transition:background .3s}
.stg.done i{background:var(--b3)}
.stg.now i{background:var(--mag)}
.stg span{font-family:var(--mono);font-size:8px;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint);margin-top:7px;display:block}
.stg.now span{color:var(--mag)}
.detail{margin:15px 0 0;color:var(--soft);font-size:14px;min-height:1.55em}
.idle{color:var(--soft);margin:0}

/* facts */
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:1px;background:var(--hair-2);border:1px solid var(--hair-2)}
.fact{background:var(--paper);padding:14px 16px}
.fact .n{font-family:var(--serif);font-size:23px;font-weight:300;
  font-variant-numeric:tabular-nums;line-height:1.1}
.fact .k{font-family:var(--mono);font-size:8.5px;letter-spacing:.15em;
  text-transform:uppercase;color:var(--faint);margin-top:4px}

/* the other machine */
.mode{display:block;background:var(--surface);border:1px solid var(--hair);
  border-radius:4px;padding:15px 17px;margin-bottom:9px;cursor:pointer;
  transition:border-color .18s,background .18s}
.mode:hover{border-color:var(--faint)}
.mode:has(input:checked){border-color:var(--b1);background:var(--tint)}
.mode input{position:absolute;opacity:0;pointer-events:none}
.mode .mt{display:block;font-weight:500;font-size:14.5px}
.mode:has(input:checked) .mt{color:var(--b1)}
.mode .md{display:block;color:var(--soft);font-size:13.5px;margin-top:2px}
.mode:has(input:focus-visible){outline:2px solid var(--mag);outline-offset:2px}
.problem{color:var(--mag);font-size:13.5px;margin:10px 0 0;min-height:1.2em}
footer{margin-top:44px;color:var(--faint);font-size:13px}
footer code{font-family:var(--mono);font-size:12px}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#17131d; --ink:#f2ecf7; --soft:#a79fb4; --faint:#6f6780;
    --hair:rgba(242,236,247,.16); --hair-2:rgba(242,236,247,.08);
    --surface:#1f1a27; --tint:#241d2e; --b1:#b79ae0; --b3:#c9b2ea;
  }
}
</style></head><body>
<div class="wrap">
<header><span class="wordmark">weefeen</span>
  <span class="lab">lending this machine</span></header>

<h1 id="head">…</h1>
<p class="because" id="because"></p>

<div class="act">
  <div class="switch" id="switch" role="group" aria-label="Take jobs or not">
    <button data-want="take">Take jobs</button>
    <button data-want="hold">Don&rsquo;t take jobs</button>
  </div>
  <button class="ghost" id="stop">Stop after this job</button>
</div>

<section>
  <span class="lab">What is happening now</span>
  <div id="now"><p class="idle">…</p></div>
</section>

<section>
  <span class="lab">What this machine can take</span>
  <div class="facts" id="facts"></div>
</section>

<section>
  <span class="lab">If this machine is not listening</span>
  <div id="modes">__MODES__</div>
  <p class="problem" id="problem"></p>
</section>

<footer>This page is on this computer only —
  <code>127.0.0.1:__PORT__</code>. Closing it changes nothing; closing the
  terminal running <code>volunteer.py</code> is what stops the machine
  taking work.</footer>
</div>

<script>
const STAGES = [['prepare','ready'],['align','align'],['bands','bands'],
                ['strip','time'],['encode','encode'],['done','done']];
let busy = false;

async function send(path, body){
  busy = true;
  try{
    const r = await fetch(path, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body || {})});
    const s = await r.json();
    if(!r.ok){ document.getElementById('problem').textContent =
                 s.problem || 'That did not go through.'; }
    else { document.getElementById('problem').textContent = ''; paint(s); }
  }catch(e){
    document.getElementById('problem').textContent = e.message;
  }finally{ busy = false; }
}

function minutes(s){
  if(!s && s !== 0) return '—';
  const m = Math.floor(s/60), r = Math.floor(s%60);
  return m ? `${m}m ${String(r).padStart(2,'0')}s` : `${r}s`;
}

function paint(s){
  const head = document.getElementById('head');
  const taking = s.taking, job = s.job;

  // The heading answers the question in words, and says what it means.
  head.className = taking ? '' : 'off';
  if(job){
    head.innerHTML = 'This machine is <b>making a video</b>.';
    document.getElementById('because').textContent =
      'It took the job from the queue. Nothing was rented.';
  }else if(taking){
    head.innerHTML = 'This machine is <b>taking jobs</b>.';
    document.getElementById('because').textContent =
      'Nothing is rendering right now. The next upload comes here.';
  }else if(s.paused){
    head.innerHTML = 'This machine is <b>not taking jobs</b>.';
    document.getElementById('because').textContent =
      'You paused it. Anything uploaded now waits for the setting below.';
  }else{
    head.innerHTML = 'This machine is <b>standing back</b>.';
    document.getElementById('because').textContent =
      'It just handed a job back, so it stays quiet for ' +
      minutes(s.quiet_for) + ' to let another machine take it.';
  }

  // The switch shows which way it is set; the other half is the thing you
  // can click. Never a label that changes under the pointer.
  document.querySelectorAll('#switch button').forEach(b => {
    const isOn = (b.dataset.want === 'take') === taking;
    b.classList.toggle('on', isOn);
    b.setAttribute('aria-pressed', isOn ? 'true' : 'false');
    b.disabled = busy;
  });
  const stop = document.getElementById('stop');
  stop.disabled = !job;
  stop.textContent = job ? 'Stop after this job' : 'Nothing to finish';

  // The render.
  const now = document.getElementById('now');
  if(!job){
    now.innerHTML = '<p class="idle">Nothing is rendering. ' +
      (taking ? 'Waiting for an upload.'
              : 'This machine is not listening for one.') + '</p>';
  }else{
    const at = STAGES.findIndex(([k]) => k === job.stage);
    now.innerHTML =
      '<div class="job"><p class="piece">' + job.piece + '</p>' +
      '<p class="meta">' + (job.minutes ? job.minutes.toFixed(1) + ' min · ' : '') +
      'running ' + minutes(job.running_for) + ' · job ' + job.id + '</p>' +
      '<div class="stages">' + STAGES.map(([k,label],i) =>
        '<div class="stg ' + (i < at ? 'done' : i === at ? 'now' : '') +
        '"><i></i><span>' + label + '</span></div>').join('') + '</div>' +
      '<p class="detail">' + (job.detail || '') + '</p></div>';
  }

  // What it could accept, measured now.
  document.getElementById('facts').innerHTML = [
    [s.free_gb == null ? '—' : s.free_gb.toFixed(1) + ' GB', 'memory free'],
    [s.longest_min == null ? '—' : Math.floor(s.longest_min) + ' min',
     'longest it will take'],
    [s.scores_here + ' of ' + s.scores_total, 'scores already here'],
  ].map(([n,k]) => '<div class="fact"><div class="n">' + n +
                   '</div><div class="k">' + k + '</div></div>').join('');

  // The other machine.
  const mode = (s.mode && s.mode.name) || '';
  document.querySelectorAll('.mode input').forEach(i => {
    i.checked = (i.value === mode);
    i.disabled = !mode;
  });
  if(s.mode && s.mode.problem){
    document.getElementById('problem').textContent =
      'The web box could not be read: ' + s.mode.problem;
  }
}

document.querySelectorAll('#switch button').forEach(b => {
  b.onclick = () => send(b.dataset.want === 'take' ? '/take' : '/hold');
});
document.getElementById('stop').onclick = () => send('/stop');
document.querySelectorAll('.mode input').forEach(i => {
  i.onchange = () => { if(i.checked) send('/mode', {mode: i.value}); };
});

async function tick(){
  if(!busy){
    try{ paint(await (await fetch('/state')).json()); }catch(e){}
  }
  setTimeout(tick, 1500);
}
tick();
</script></body></html>
""".replace("__MODES__", _MODE_ROWS)
