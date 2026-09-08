'use strict';
/* Connect the interface to this server.
 *
 * The design package is a working mock: it holds a fixed library, a fixed
 * ranking, and a submit button that only changes screen. Everything else —
 * the frame editor, the poster stills, the rights gate, the address memory
 * — already works client-side and is left alone.
 *
 * This file replaces exactly four things:
 *
 *     WORKS    the library            <- GET  /api/library
 *     CANDS    the recognised piece   <- POST /api/jobs/<id>/identify
 *     upload   read locally only      <- POST /api/upload
 *     #submit  changed screen only    <- POST /api/jobs/<id>/render
 *
 * Two additions the mock had no need for. Recognition takes the better
 * part of a minute, so there is a state for listening; and a piece can be
 * recognised whose score is not installed here, which is neither "we heard
 * it" nor "we could not place it" and must not be told as either.
 */

const API = {
  library:  '/api/library',
  upload:   '/api/upload',
  identify: id => `/api/jobs/${id}/identify`,
  answer:   id => `/api/jobs/${id}/identification`,
  render:   id => `/api/jobs/${id}/render`,
};

let JOB = null;                 // the job this upload belongs to
let SERVER = { online: false, can_identify: false, can_sync: false };

/* What the design shipped with. Opening the HTML on its own is a supported
 * way to use this — the README says so — and it must keep working: with no
 * server, the mock library and ranking come back and every screen behaves
 * as it did before any of this was wired. Falling back is not pretending,
 * as long as the interface says which one you are looking at. */
const MOCK_WORKS = WORKS.slice();
const MOCK_CANDS = CANDS.slice();

/* ── the library ─────────────────────────────────────────────────────── */
async function loadLibrary(){
  try{
    const r = await fetch(API.library);
    if(!r.ok) throw new Error(r.status);
    const data = await r.json();
    SERVER = { online: true, can_identify: data.can_identify,
               can_sync: data.can_sync };
    if(Array.isArray(data.works) && data.works.length) WORKS = data.works;
  }catch(err){
    // No server: keep the shipped library so the whole interface still
    // works locally, which is what it was built to do.
    SERVER.online = false;
    console.info('no server — running the interface on its built-in library');
  }
}

/* Behave exactly as the unwired design did. */
function localOnly(name, why){
  stopListening();
  WORKS = MOCK_WORKS;
  CANDS = MOCK_CANDS;
  named(name);
  const hint = $('#piecehint');
  if(hint) hint.textContent = why || 'local preview — nothing was uploaded';
}

/* ── recognition states the mock did not have ────────────────────────── */
const baseRecognise = recognise;

function fileBar(meta){
  $('#pieceblock').classList.toggle('on', !!S.file);
  $('#advance').classList.toggle('on', !!S.file && !!S.piece);
  $('#uploadstate').classList.toggle('hide', !!S.file);
  $('#filebar').classList.toggle('on', !!S.file);
  $('#filebar').classList.toggle('found', S.recog === 'heard');
  $('#fbName').textContent = S.file || '';
  $('#fbMeta').innerHTML = `${S.src.label} · ${S.src.dur} · ${meta}`;
}

/* Recognition takes about three quarters of a minute. The page needs to
 * show it is alive, and nothing else: how it works is our problem, and a
 * countdown only invites someone to watch it run late. A moving bar, one
 * line, no explanation. */
const LISTEN_ESTIMATE = 45;
let listenStarted = 0, listenTimer = null;

function listenTick(){
  const bar = $('#listenbar');
  if(!bar) return;
  const gone = (Date.now() - listenStarted) / 1000;
  bar.style.width = Math.min(97, (gone / LISTEN_ESTIMATE) * 100).toFixed(1) + '%';
}

function startListening(){
  listenStarted = Date.now();
  clearInterval(listenTimer);
  listenTimer = setInterval(listenTick, 250);
}

function stopListening(){ clearInterval(listenTimer); listenTimer = null; }

recognise = function(){
  if(S.recog === 'listening'){
    fileBar('<span class="lab">listening</span>');
    $('#piecelab').textContent = 'Listening';
    $('#piecehint').textContent = '';
    $('#recogbody').innerHTML = `
      <p class="heard">Detecting the piece, <b>please wait a few seconds.</b></p>
      <div class="cands lead"><div class="cand" style="grid-template-columns:1fr">
        <span class="bar"><i id="listenbar" style="width:0%"></i></span>
      </div></div>`;
    listenTick();
    return;
  }
  if(S.recog === 'unavailable'){
    // Not `named` — that is the design's own function, and shadowing it
    // here would be a trap for whoever edits this block next.
    const heard = CANDS.length ? (W(CANDS[0][0])?.t || S.heardLabel) : S.heardLabel;
    fileBar('<span class="ok">recognised</span>');
    $('#piecelab').textContent = 'Recognised';
    $('#piecehint').textContent = '';
    $('#recogbody').innerHTML = `
      <p class="heard">We heard <b>${esc(heard || 'this piece')}</b>, but that
      score is not in the library yet.<br>It is being added one at a time —
      until then, pick something else below.</p>
      <div class="manual"><span class="lab">What we can do today</span>
        <div class="pieces" id="pieces"></div></div>`;
    manualList();
    return;
  }
  return baseRecognise();
};

/* ── the real score band, at its real shape ──────────────────────────── */
/* The mock drew staves because it had no score to show. With one selected
 * we show the actual first band the renderer will use, which also fixes
 * the proportions: the design assumed a strip about 13:1, and Op.39's
 * bands are 1306x244 — 5.35:1, two and a half times taller. Drawing the
 * real image at the mock's height would misrepresent the output.
 *
 * The band box becomes the strip itself. The stave area is given the whole
 * box and the caption line is dropped, because in the rendered video the
 * band IS the notation — the caption was chrome the mock could afford. */
const bandCSS = document.createElement('style');
bandCSS.textContent = `
  .frame .band.real .stv, .frame .ctr-band.real .stv{height:100%}
  .frame .band.real{padding-bottom:0}
  .frame .band.real .bandwk, .frame .ctr-band.real .bandwk{display:none}
  /* Vector bands are ink on transparency, so the score is used as a mask
     and filled with the chosen note colour — the same ink-on-paper model
     the renderer uses, rather than a picture of someone else's ink. */
  .frame .realband{position:absolute;inset:0;pointer-events:none}
  .frame .realband.vector{
    -webkit-mask-image:var(--band);mask-image:var(--band);
    -webkit-mask-size:contain;mask-size:contain;
    -webkit-mask-repeat:no-repeat;mask-repeat:no-repeat;
    -webkit-mask-position:center;mask-position:center}
  /* A bitmap band already has its paper burnt in: the most that can be
     done is let the band colour show through the white. */
  .frame .realband.raster{width:100%;height:100%;object-fit:contain;
    mix-blend-mode:multiply}
`;
document.head.appendChild(bandCSS);

/* The chosen work, but only when the server told us about its band. */
function realBand(){
  if(!S.piece) return null;
  const w = W(S.piece);
  return (w && w.band && w.band_w && w.band_h) ? w : null;
}

const baseGeo = geo;
geo = function(){
  const g = baseGeo();
  const w = realBand();
  if(w){
    const [aw, ah] = S.aspect.split('/').map(Number);
    // Height the strip needs to span the width it is given, in percent of
    // the frame height. Capped so a very tall band cannot eat the video.
    g.band = Math.min(55, (100 - g.pnl) * (aw / ah) / (w.band_w / w.band_h));
  }
  return g;
};

const baseStave = staveHTML;
staveHTML = function(bars, k, a){
  const w = realBand();
  if(!w) return baseStave(bars, k, a);
  if(w.vector){
    // A block of the note colour, shaped by the score. Changing the ink
    // changes this, exactly as it changes the rendered video.
    return `<div class="realband vector" style="--band:url('${w.band}');`
         + `background-color:${S.noteColor}"></div>`;
  }
  return `<img class="realband raster" src="${w.band}" alt="" draggable="false"/>`;
};

/* Mark the band so the stylesheet above applies only when it is real. */
const basePaint = paint;
paint = function(){
  const out = basePaint.apply(this, arguments);
  const real = !!realBand();
  $$('.frame .band, .frame .ctr-band').forEach(el =>
    el.classList.toggle('real', real));
  return out;
};

/* ── upload, then listen ─────────────────────────────────────────────── */
async function uploadAndIdentify(name, blob){
  // Someone can choose a file before the library has answered; waiting here
  // is the difference between "there is no server" and "we asked too soon".
  await READY;

  // Nothing to upload to: the design's own behaviour, straight away.
  if(!SERVER.online || !SERVER.can_identify){
    return localOnly(name, SERVER.online
      ? 'recognition is not configured — choose the piece yourself'
      : 'local preview — nothing was uploaded');
  }

  S.file = name;
  S.piece = null;
  S.manual = false;
  S.recog = 'listening';
  startListening();
  draw();
  centerOn('#pieceblock', 240);

  let data;
  try{
    const form = new FormData();
    form.append('video', blob, name);
    const r = await fetch(API.upload, { method:'POST', body: form });
    data = await r.json();
    if(!r.ok) throw new Error(data.error || `upload failed (${r.status})`);
  }catch(err){
    // A server that refuses this file has said something worth reading; a
    // server that vanished mid-upload has not, and should not strand the
    // interface on a screen with no way forward.
    if(err instanceof TypeError) return localOnly(name, 'lost the server — local preview');
    return failed(err.message || 'The upload did not go through.');
  }

  JOB = data.job.id;
  if(data.probe && data.probe.duration) S.src.dur = clock(data.probe.duration);

  try{
    await fetch(API.identify(JOB), { method:'POST' });
  }catch(err){
    return localOnly(name, 'lost the server — local preview');
  }
  poll();
}

function clock(seconds){
  const s = Math.round(seconds);
  return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
}

function failed(message){
  stopListening();
  S.recog = 'none';
  draw();
  const body = $('#recogbody');
  if(body) body.insertAdjacentHTML('afterbegin',
    `<p class="heard"><b class="alert">${esc(message)}</b></p>`);
}

/* Ask until it answers. The work is on a thread over there; this only
 * decides when to stop waiting. */
async function poll(started){
  started = started || Date.now();
  if(Date.now() - started > 5 * 60 * 1000) return failed('Listening timed out.');
  let a;
  try{
    const r = await fetch(API.answer(JOB));
    a = await r.json();
  }catch(err){
    return localOnly(S.file, 'lost the server — local preview');
  }
  if(a.state === 'running' || a.state === 'idle'){
    return setTimeout(()=>poll(started), 2000);
  }
  if(a.state === 'error') return failed(a.error || 'Listening failed.');
  applyAnswer(a);
}

function applyAnswer(a){
  stopListening();
  const cands = a.candidates || [];
  // The interface keys everything by the library's own ids, and the server
  // answers with the same ones, so a recognised piece the library knows is
  // selectable immediately.
  CANDS = cands.filter(c => c.renderable).map(c => [c.package, c.confidence]);
  S.heardLabel = cands.length ? cands[0].label : '';

  if(a.outcome === 'matched' && CANDS.length){
    S.recog = 'heard';
    choose(CANDS[0][0]);
    fileRow(S.file, 'recognised');
    draw();
    centerOn('#pieceblock', 240);
    return;
  }
  if(a.outcome === 'unavailable'){
    S.recog = 'unavailable';
    S.piece = null;
    draw();
    centerOn('#pieceblock', 240);
    return;
  }
  unheard();
}

/* ── send it to render ───────────────────────────────────────────────── */
const baseSubmit = $('#submit').onclick;

$('#submit').onclick = async function(){
  const address = $('#email').value.trim();
  if(!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address)){
    emailNote('That address does not look right yet.', true);
    $('#email').focus();
    return;
  }
  if(!S.piece){
    emailNote('Choose a piece first.', true);
    return;
  }
  // Local preview: carry on through the screens as the design does, so the
  // whole flow can still be walked without a server behind it.
  if(!SERVER.online || !JOB){
    emailNote();
    return baseSubmit();
  }
  emailNote('Sending it to render…');
  try{
    const r = await fetch(API.render(JOB), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        score: S.piece,
        style: {
          aspect: S.aspect,
          band_position: S.bandPos,
          panel: S.panel !== 'off',
          band_bg: S.bandColor,
          band_fg: S.noteColor,
          band_bg_opacity: S.alpha / 100,
          canvas_bg: S.bdColor,
          background: S.bd === 'colour' ? 'none' : S.bd,
        },
        meta: S.text,
        email: address,
      }),
    });
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || `render refused (${r.status})`);
  }catch(err){
    emailNote(err.message || 'That did not go through.', true);
    return;
  }
  emailNote();
  baseSubmit();          // the screens, the address memory, the recap
};

/* ── take over the upload, leaving the rights gate exactly as it was ─── */
$('#rightsGo').onclick = ()=>{
  const name = pendingFile, blob = pendingBlob;
  $('#rights').classList.remove('on');
  pendingFile = null; pendingBlob = null; $('#file').value = '';
  if(blob) grabPosters(blob);              // stills are read locally, as before
  if(name && blob) uploadAndIdentify(name, blob);
};

const READY = loadLibrary().then(()=>{ if(!S.file) draw(); });
