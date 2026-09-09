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

/* Confidence figures are for us, not for the person uploading: a number
 * beside a piece invites arguing with it, and the honest answer to "is it
 * 98 or 72" is that either way it is the one we heard. Add ?debug to the
 * address to see them while working on recognition. */
const DEBUG = new URLSearchParams(location.search).has('debug');

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

/* The bar covers two real phases. Sending the file is measurable, so it is
 * measured; finding the piece is not — the recogniser says nothing until it
 * is done — so that stretch runs on the clock. Locally the first phase is
 * over instantly and you see mostly the second; over a network it is the
 * other way round, which is when an honest bar matters most. */
const UPLOAD_SHARE = 0.35;
let uploadFraction = 0, uploadDone = false;

function setBar(fraction){
  const bar = $('#listenbar');
  if(bar) bar.style.width = (Math.max(0, Math.min(0.97, fraction)) * 100).toFixed(1) + '%';
}

function listenTick(){
  if(!uploadDone) return setBar(uploadFraction * UPLOAD_SHARE);
  const gone = (Date.now() - listenStarted) / 1000;
  setBar(UPLOAD_SHARE + (1 - UPLOAD_SHARE) * Math.min(1, gone / LISTEN_ESTIMATE));
}

/* fetch cannot report how much of a body has gone out; XHR can. */
function postUpload(name, blob){
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('video', blob, name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', API.upload);
    xhr.upload.onprogress = e => {
      if(e.lengthComputable){ uploadFraction = e.loaded / e.total; listenTick(); }
    };
    xhr.onload = () => {
      let body = {};
      try{ body = JSON.parse(xhr.responseText || '{}'); }catch(err){}
      if(xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body.error || `upload failed (${xhr.status})`));
    };
    // A refusal arrives as a status; a vanished server arrives here, and
    // the two are told apart the same way the rest of this file does it.
    xhr.onerror = () => reject(new TypeError('Failed to fetch'));
    xhr.onabort  = () => reject(new TypeError('Failed to fetch'));
    xhr.send(form);
  });
}

function startListening(){
  listenStarted = Date.now();
  uploadFraction = 0; uploadDone = false;
  clearInterval(listenTimer);
  listenTimer = setInterval(listenTick, 250);
}

function stopListening(){
  clearInterval(listenTimer); listenTimer = null;
  // Give step one back the dropzone it lent us.
  const drop = $('#drop');
  if(drop) drop.classList.remove('busy');
  const browse = $('#browse');
  if(browse) browse.disabled = false;
  if(typeof resetDrop === 'function') resetDrop();
}

recognise = function(){
  if(S.recog === 'listening'){
    // The wait belongs to step one, where the file was chosen. Step two
    // stays shut until there is a piece to agree with — asking someone to
    // confirm something that is not known yet is not a step.
    $('#pieceblock').classList.remove('on');
    $('#advance').classList.remove('on');
    $('#filebar').classList.remove('on');
    $('#uploadstate').classList.remove('hide');

    const drop = $('#drop');
    if(drop) drop.classList.add('filled', 'busy');
    const browse = $('#browse');
    if(browse) browse.disabled = true;
    const copy = $('#dropcopy');
    if(copy) copy.innerHTML = `
      <h2>Uploading and checking your <em>video</em></h2>
      <span class="uprail"><i id="listenbar" style="width:0%"></i></span>
      <span class="lab">${esc(S.file || '')} &middot; please wait a few seconds</span>`;
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
  if(S.recog === 'heard' && CANDS.length){
    // The design assumed three candidates. Recognition often returns one,
    // because anything scoring near nothing is noise and is not offered —
    // so the "it may also be" heading and its empty list are dropped, the
    // sentence stops promising alternatives that are not there, and the
    // way out of the list never counts what it cannot see.
    const alts = CANDS.slice(1);
    fileBar('<span class="ok">recognised</span>');
    $('#piecelab').textContent = '';
    $('#piecehint').textContent = '';

    const candBtn = ([id, pc]) => {
      const w = W(id), on = id === S.piece;
      if(!w) return '';
      return `<button class="cand${on ? ' on' : ''}" data-p="${id}" role="radio"
        aria-checked="${on}"${DEBUG ? '' : ' style="grid-template-columns:18px 1fr"'}>
        <span class="mark"></span>
        <span class="t">${esc(w.t)}, ${esc(w.op)}</span>
        ${DEBUG ? `<span class="bar"><i style="width:${pc}%"></i></span>
        <span class="pc">${pc}%</span>` : ''}
      </button>`;
    };

    const top = W(CANDS[0][0]);
    $('#recogbody').innerHTML = `
      <p class="heard">We heard <b>${esc(top ? top.t + ', ' + top.op : 'this piece')}</b>.
      It is selected${alts.length ? ' &mdash; confirm it, or pick another below.' : '.'}</p>
      <div role="radiogroup" aria-label="Choose the piece">
        <div class="candhead"><span class="lab">Most likely</span>
          <span>${DEBUG ? '<span class="lab">Confidence</span> ' : ''}<button
            class="linkbtn" id="manualToggle">${S.manual ? 'close' : 'change'}</button></span></div>
        <div class="cands lead">${candBtn(CANDS[0])}</div>
        ${alts.length ? `
        <div class="candhead alt"><span class="lab">Is it wrong? It may also be</span>
          ${DEBUG ? '<span class="lab">Confidence</span>' : ''}</div>
        <div class="cands">${alts.map(candBtn).join('')}</div>` : ''}
      </div>
      ${S.manual ? `<div class="manual"><span class="lab">Choose it yourself</span>
        <div class="pieces" id="pieces"></div></div>` : ''}`;

    $$('#recogbody .cand').forEach(b => b.onclick = () => choose(b.dataset.p));
    $('#manualToggle').onclick = () => { S.manual = !S.manual; recognise(); };
    if(S.manual) manualList();
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
  .frame .band.real{padding:0}
  .frame .band.real .bandwk, .frame .ctr-band.real .bandwk{display:none}
  /* The engraving is transparent, so it simply sits on the band's paper
     and the paper shows through everywhere there is no ink — which is what
     the renderer produces. The ink colour is applied to the file itself on
     the way out, because the engraving has no intrinsic size and so cannot
     be used as a CSS mask: that failed silently and painted a solid block
     of ink over the paper instead. */
  .frame .realband{position:absolute;inset:0;width:100%;height:100%;
    object-fit:contain;object-position:center;pointer-events:none}
  /* A bitmap band already has its paper burnt in: the most that can be
     done is let the band colour show through the white. */
  .frame .realband.raster{mix-blend-mode:multiply}
  /* Step one keeps the wait: a rail across the dropzone it started from. */
  .dropzone.busy{cursor:default}
  .dropzone.busy:hover{border-color:var(--hair-2);background:linear-gradient(#fdfbf7,#f9f5ee)}
  .dropzone.busy .dropbtn{opacity:.35;pointer-events:none}
  .uprail{display:block;position:relative;height:2px;border-radius:2px;
    background:var(--hair);overflow:hidden;margin:14px 0 10px}
  .uprail i{position:absolute;left:0;top:0;height:100%;border-radius:2px;
    background:var(--mag);transition:width .25s linear}
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
    // Tinted where it is served: the engraving arrives already in the
    // chosen ink, transparent everywhere else, so the paper behind it is
    // the band's own colour.
    const src = `${w.band}?ink=${encodeURIComponent(S.noteColor)}`;
    return `<img class="realband vector" src="${src}" alt="" draggable="false"/>`;
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
  applyRealLayout();
  applyBandMark();
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
  centerOn('#uploadstate', 240);

  let data;
  try{
    data = await postUpload(name, blob);
    uploadDone = true;              // the file is there; now it is listening
    listenStarted = Date.now();
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
          video_offset: S.vidOffset ?? 0.5,
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

/* ── the frame, laid out the way the renderer lays it out ────────────── */
/* The preview placed the video across the whole frame and drew the band on
 * top of it, so the band appeared to cover the picture. The renderer never
 * did that: it gives the band its own height, takes that plus the gap off
 * the frame, and puts the video in what is left.
 *
 * What the renderer now does — and what this mirrors — is fill the content
 * width with the video rather than shrinking it to fit the leftover height,
 * because fitting a 16:9 picture into a shorter box leaves a bar down each
 * side. The height that will not fit is cropped, and `S.vidOffset` chooses
 * which slice survives: 0 the top of the frame, 1 the bottom.
 *
 * Behind the band is the backdrop, never the video, so a translucent band
 * shows the background colour through it exactly as the render will.
 */
const CANVAS = { '16/9':[1920,1080], '1/1':[1080,1080], '9/16':[1080,1920] };
const MARGIN = 0, GAP = 0, PANEL_W = 0.301;

if(S.vidOffset === undefined) S.vidOffset = 0.5;

function renderLayout(){
  const w = realBand();
  if(!w) return null;
  const [cw, ch] = CANVAS[S.aspect] || CANVAS['16/9'];
  const [va, vb] = (S.src.aspect || '16/9').split('/').map(Number);
  const videoAspect = va / vb, bandAspect = w.band_w / w.band_h;

  const panelW = S.panel === 'left' ? Math.round(cw * PANEL_W) : 0;
  const contentX = panelW + MARGIN, contentW = cw - panelW - 2 * MARGIN;
  const contentY = MARGIN, contentH = ch - 2 * MARGIN;

  // x264 needs even sides, and the renderer rounds the height first then
  // recomputes the width from it. Mirrored here so the preview does not
  // quietly disagree with the output by a handful of pixels.
  const even = n => Math.max(2, Math.round(n) - (Math.round(n) % 2));
  // A position is not a size: zero is a real answer for one and not the
  // other, and rounding it up to two puts a gap along the edge.
  const evenAt = n => { const w = Math.round(Math.max(0, n)); return w - (w % 2); };
  let bandW = Math.min(contentW, contentH * bandAspect);
  let bandH = even(bandW / bandAspect);
  bandW = even(bandH * bandAspect);
  if(bandW > contentW){ bandW = even(contentW); bandH = even(bandW / bandAspect); }

  const boxH = contentH - bandH - GAP;
  if(boxH < 16) return null;

  // Cover the box on both axes: fitting inside would leave a bar down the
  // sides on a wide frame, and dead space above the video beside a panel.
  const videoW = even(contentW), videoH = even(boxH);
  const sourceH = even(Math.max(videoW / videoAspect, videoH));
  const sourceW = even(sourceH * videoAspect);
  const surplus = Math.max(0, sourceH - videoH);      // the vertical choice
  const surplusX = Math.max(0, sourceW - videoW);     // centred, no choice

  const top = S.bandPos === 'top';
  const bandY = top ? contentY : contentY + boxH + GAP;
  // Against the band, not centred: they meet, and any slack goes to the
  // far edge rather than opening a seam between them.
  const videoY = top ? contentY + bandH + GAP : bandY - GAP - videoH;

  return { cw, ch, surplus, surplusX,
    band:  { x: contentX + evenAt((contentW - bandW) / 2), y: bandY, w: bandW, h: bandH },
    video: { x: contentX, y: videoY, w: videoW, h: videoH } };
}

/* Put the preview's own elements where the renderer would put them. */
function applyRealLayout(){
  const frame = $('#frame');
  const L = renderLayout();
  if(!frame || !L) return;
  const k = frame.clientWidth / L.cw;
  if(!(k > 0)) return;
  const px = (r, key) => (r[key] * k).toFixed(1) + 'px';

  const vid = frame.querySelector('.vidzone');
  if(vid){
    Object.assign(vid.style, {
      left: px(L.video,'x'), top: px(L.video,'y'),
      width: px(L.video,'w'), height: px(L.video,'h'), right: 'auto',
      // cover, then the same slice the renderer takes: centred across,
      // and down wherever there is a choice to make
      backgroundSize: 'cover',
      backgroundPosition: `50% ${(L.surplus > 1 ? S.vidOffset * 100 : 50).toFixed(1)}%`,
    });
  }
  const band = frame.querySelector('.band');
  if(band){
    Object.assign(band.style, {
      left: px(L.band,'x'), top: px(L.band,'y'),
      width: px(L.band,'w'), height: px(L.band,'h'), bottom: 'auto',
    });
  }
}

/* ── the video becomes something you can select and move ─────────────── */
/* The design treated the video as scenery: `regionOf` returned null for it
 * and a click opened the file picker. Now that the picture is cropped,
 * which part of it survives is a real choice, so it gets a region like the
 * others — and the backdrop colour lives there too, because the colour you
 * are choosing is the one that shows around and behind the video. */
REGION.push(['vid', '.vidzone', '.vidzone']);
TITLES.vid = 'Your video';

const baseRegionOf = regionOf;
regionOf = function(el){
  if(el.closest && el.closest('.vidzone')) return 'vid';
  return baseRegionOf(el);
};

const NUDGE = 0.08;          // one press of an arrow
function nudgeVideo(by){
  S.vidOffset = Math.max(0, Math.min(1, (S.vidOffset ?? 0.5) + by));
  paint();
  if(typeof select === 'function' && sel === 'vid') select('vid');
}

const baseInspBody = inspBody;
inspBody = function(id){
  if(id !== 'vid') return baseInspBody(id);
  const L = renderLayout();
  const canMove = L && L.surplus > 1;
  const pct = Math.round((S.vidOffset ?? 0.5) * 100);
  return `<div class="ih"><b>${TITLES.vid}</b>`
    + `<button class="x" data-x aria-label="Close">×</button></div>`
    + (canMove ? `
      <div class="irow"><span class="lab">What to keep</span>
        <div class="slider">
          <button class="o" data-vid="up" title="Move the video up" aria-label="Move up">▲</button>
          <input type="range" min="0" max="100" value="${pct}" id="vidoff"/>
          <button class="o" data-vid="down" title="Move the video down" aria-label="Move down">▼</button>
        </div>
        <p class="inote">The picture is wider than the space beside the score,
        so some of its height is cut. This chooses which part stays —
        ${pct === 0 ? 'the top' : pct === 100 ? 'the bottom' : 'the middle'} of the frame.</p>
      </div>`
    : `<div class="irow"><p class="inote">The whole picture fits beside the
        score here, so nothing is cut.</p></div>`)
    + `<div class="irow"><span class="lab">Behind and around it</span>
        ${chipRow('bdColor',['#241a33','#0f0d13','#381C53','#f6f1e8'])}
        <p class="inote">Also what shows through the score band when its
        paper is made transparent.</p></div>`
    + `<div class="irow"><span class="lab">The file</span>
        <button class="linkbtn" data-vid="replace">Choose a different video</button></div>`;
};

/* Capture-phase, so the design's own handler — which opens the file picker
 * on any click in the video — does not fire first. */
document.addEventListener('click', e=>{
  const zone = e.target.closest && e.target.closest('.vidzone');
  if(zone && $('#frame') && $('#frame').contains(zone)){
    e.stopPropagation();
    select('vid');
  }
}, true);

document.addEventListener('click', e=>{
  const b = e.target.closest('[data-vid]');
  if(!b) return;
  e.stopPropagation();
  if(b.dataset.vid === 'up')   return nudgeVideo(+NUDGE);   // reveal lower down
  if(b.dataset.vid === 'down') return nudgeVideo(-NUDGE);
  if(b.dataset.vid === 'replace') $('#file')?.click();
});

document.addEventListener('input', e=>{
  if(e.target.id !== 'vidoff') return;
  S.vidOffset = (+e.target.value) / 100;
  paint();
});

/* ── paper and ink must differ ───────────────────────────────────────── */
/* A score in the colour of its own paper is an empty band. The clash is
 * stopped where it is offered rather than after it is chosen: the swatch
 * matching the other colour is marked and does nothing. */
const clashCSS = document.createElement('style');
clashCSS.textContent = `
  .chip.clash{opacity:.28;cursor:not-allowed;position:relative}
  .chip.clash::after{content:'';position:absolute;inset:-2px;
    border-radius:inherit;background:
      linear-gradient(to bottom right, transparent 46%, currentColor 46%,
                      currentColor 54%, transparent 54%)}
`;
document.head.appendChild(clashCSS);

const COUNTERPART = { bandColor: 'noteColor', noteColor: 'bandColor' };

const baseChipRow = chipRow;
chipRow = function(g, list){
  const other = COUNTERPART[g];
  let html = baseChipRow(g, list);
  if(other){
    // Mark the one that would make the score vanish.
    html = html.replace(/<span class="chip([^"]*)" data-v="([^"]+)"/g,
      (whole, rest, value) => value.toLowerCase() === String(S[other]).toLowerCase()
        ? `<span class="chip${rest} clash" data-v="${value}"` : whole);
  }
  return html;
};

/* Capture, so the design's own chip handler never sees the click. */
document.addEventListener('click', e=>{
  const chip = e.target.closest && e.target.closest('.chip.clash');
  if(chip){ e.stopPropagation(); e.preventDefault(); }
}, true);

/* ── the mark ────────────────────────────────────────────────────────── */
/* Two declinations, each for the place it belongs:
 *
 *   FULL    the circle with "weefeen" beside it — used in the title panel,
 *           where a wordmark has the width to be read.
 *   CIRCLE  the circle alone — used when there is no panel, sitting on the
 *           score band, where a wordmark would crowd the notation.
 *
 * The colour is chosen, not recoloured: these are the brand's own files,
 * and picking white or purple by the luminance of whatever it sits on
 * keeps it legible without inventing a shade nobody signed off.
 */
const LOGO = '/app/assets/logo';

function luminance(hex){
  const h = String(hex || '#000').replace('#', '');
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h;
  const n = parseInt(full, 16);
  if(!isFinite(n)) return 0;
  return (0.299 * (n >> 16 & 255) + 0.587 * (n >> 8 & 255) + 0.114 * (n & 255)) / 255;
}

/* Light surfaces take the purple mark, dark ones the white — and the two
 * sets name that differently: FULL has a PURPLE, CIRCLE has no plain
 * purple at all, its light-surface version being the white disc with the
 * purple glyph. Asking for the name that does not exist would have served
 * a 404 into the frame. */
const LIGHT_MARK = { FULL: 'PURPLE', CIRCLE: 'WHITE_PURPLE' };
const markFor = (kind, surface) =>
  `${LOGO}/${kind}/${luminance(surface) > 0.55 ? LIGHT_MARK[kind] : 'WHITE'}.svg`;

/* In the panel the mark sits on the backdrop; on the band it sits on the
 * band's own paper, so each asks the surface it is actually on. */
const panelMark = () => markFor('FULL', S.bd === 'colour' ? S.bdColor : '#241a33');
const bandMark  = () => markFor('CIRCLE', S.bandColor);

const baseLogoSlot = logoSlot;
logoSlot = function(size, mb, round){
  if(S.logo !== 'weefeen') return baseLogoSlot(size, mb, round);
  return `<span class="logo" style="display:block;width:${size}px;
    height:${(size / 3.534).toFixed(1)}px;margin-bottom:${mb}px;
    background:url('${panelMark()}') left center/contain no-repeat"></span>`;
};

/* The band carries the circle only when nothing else carries the mark. */
function applyBandMark(){
  const frame = $('#frame');
  if(!frame) return;
  frame.querySelectorAll('.wmk').forEach(el => {
    const alone = S.panel === 'off' && S.logo === 'weefeen';
    if(!alone){ el.style.display = S.logo === 'weefeen' ? 'none' : ''; return; }
    const side = Math.max(10, (frame.clientHeight || 0) * 0.055);
    el.textContent = '';
    Object.assign(el.style, {
      display: 'block', width: side + 'px', height: side + 'px',
      background: `url('${bandMark()}') center/contain no-repeat`,
    });
  });
}

/* ── the rights line is a warning, not a reassurance ─────────────────── */
/* It sat in pale grey with a green tick, which reads as "all good". What
 * it actually says is that publishing this is the uploader's
 * responsibility, so it takes the amber the design already uses for the
 * free-tier note and the dialog's liability line — the same vocabulary,
 * given to the thing that most needs it. */
const rightsCSS = document.createElement('style');
rightsCSS.textContent = `
  .rightsdone{
    color:#4a3212;
    background:#f9f0df;
    border-top:0;
    border-left:2px solid #b8813a;
    border-radius:2px;
    padding:11px 14px;
    margin-top:14px;
    align-items:center}
  .rightsdone b{color:#4a3212}
  .rightsdone svg{color:#b8813a;width:15px;height:15px;flex:0 0 15px;margin-top:0}
`;
document.head.appendChild(rightsCSS);

/* A tick says "done"; this is a caution. Same box, honest glyph. */
function markRightsAsCaution(){
  const svg = document.querySelector('.rightsdone svg');
  if(!svg || svg.dataset.caution) return;
  svg.dataset.caution = '1';
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.innerHTML = `<path d="M8 1.6 15 14H1z" fill="none" stroke="currentColor"
      stroke-width="1.5" stroke-linejoin="round"/>
    <path d="M8 6.2v3.4" stroke="currentColor" stroke-width="1.6"
      stroke-linecap="round"/>
    <circle cx="8" cy="11.7" r=".85" fill="currentColor"/>`;
}
markRightsAsCaution();
document.addEventListener('DOMContentLoaded', markRightsAsCaution);
