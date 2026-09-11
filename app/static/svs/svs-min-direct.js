/* Step 3 as direct manipulation: the frame IS the control surface.
   Layered on top of svs-min.js's px-scaled renderer via delegation on the
   stable #frame container, so repaints can't tear the interaction down. */

let sel = null;
let typing = false;

/* which editable region does a rendered node belong to */
/* container = outlined and tagged · hit = also opens the region */
const REGION = [
  ['logo', '.logo', '.logo'],
  ['pnl',  '.pnl, .ctr-pnl',   '.pnl, .ctr-pnl'],
  ['band', '.band, .ctr-band', '.band, .ctr-band'],
  ['bd',   '.bd',              '.bd, .artlab, .ctr-art'],
];
const bdLive = () => S.panel==='centered';
function regionOf(el){
  /* the performance video fills its own rectangle — nothing to change there */
  if(el.closest('.vidzone')) return null;
  for(const [id,,hit] of REGION) if(el.closest(hit)) return id==='bd' && !bdLive() ? null : id;
  return null;
}
const partsOf = id => {
  const q = REGION.find(r=>r[0]===id)[1];
  return [...($('#frame')?.querySelectorAll(q) || [])];
};

/* ── decorate: run after every paint to re-apply selection + editing ── */
function decorate(){
  const f = $('#frame'); if(!f) return;
  REGION.forEach(([id,q])=>f.querySelectorAll(q).forEach(el=>{
    const dead = id==='bd' && !bdLive();
    el.classList.add('rg');
    el.classList.toggle('static', dead);
    el.classList.toggle('sel', id===sel && !dead);
    let t = el.querySelector(':scope > .rgtag');
    if(!t){ t = document.createElement('span'); t.className = 'rgtag'; el.appendChild(t); }
    t.textContent = id==='logo' ? 'Your logo · change'
      : id==='pnl' ? 'Title panel · edit'
      : id==='band' ? 'Score band · drag to move'
      : 'Backdrop · change';
    if(id==='bd') t.hidden = dead;
  }));
  f.querySelectorAll('[data-f]').forEach(el=>{
    el.setAttribute('contenteditable','plaintext-only');
    el.spellcheck = false;
  });
  if(sel) place(sel);
}

/* ── inspector ── */
const TITLES = { bd:'Backdrop', pnl:'Title panel', band:'Score band', logo:'Your logo' };
function chipRow(g, list){
  return `<div class="chips" data-g="${g}">${list.map(v=>
    `<span class="chip${S[g]===v?' on':''}" data-v="${v}" title="${NAMES[v]||v}" style="background:${v}"></span>`).join('')}
    <span class="chipname">${NAMES[S[g]]||''}</span></div>`;
}
function inspBody(id){
  const head = `<div class="ih"><b>${TITLES[id]}</b><button class="x" data-x aria-label="Close">×</button></div>`;
  if(id==='bd') return head + `
    <div class="irow"><span class="lab">Source</span><div class="opts" data-g="bd" id="bdopts"></div>
      <span class="why-inline" id="bdwhy"></span></div>
    <div class="irow ${S.bd==='colour'?'':'hidden'}"><span class="lab">Colour</span>${chipRow('bdColor',['#241a33','#0f0d13','#381C53','#f6f1e8'])}</div>
    <div class="irow ${S.bd==='upload'?'':'hidden'}"><span class="lab">Your image</span>
      <label class="bdimg" id="bdimgdrop" for="bdfile"></label>
      <ul class="bdreq" id="bdreq">
        <li><b>16:9 exactly</b> — 1920×1080 or larger</li>
        <li><b>1920×1080 minimum</b> — smaller looks soft once encoded</li>
        <li>jpg, png or webp · up to 10 mb</li>
      </ul></div>`;
  if(id==='logo') return head + `
    <div class="irow"><span class="lab">Logo</span>
      <div class="opts" data-g="logo">
        <button class="o${S.logo==='weefeen'?' on':''}" data-v="weefeen">Weefeen</button>
        <button class="o${S.logo==='upload'?' on':''}" data-v="upload">Your own</button>
        <button class="o${S.logo==='none'?' on':''}" data-v="none">None</button>
      </div></div>
    <div class="irow ${S.logo==='upload'?'':'hidden'}"><span class="lab">The file</span>
      <label class="bdimg${S.logoBad?' bad':''}" id="logoimgdrop" for="logofile">${
        S.logoBad ? `<span class="bdimg-name">${esc(S.logoBad.name)}</span><span class="bdimg-err">${esc(S.logoBad.why)}</span><span class="bdimg-swap">Try another</span>`
        : S.logoImage ? `<span class="bdimg-name">${esc(S.logoImage)}</span><span class="bdimg-ok">✓ ready</span><span class="bdimg-swap">Replace</span>`
        : `<span class="bdimg-cta">Drop your logo, or click to choose</span><span class="lab">svg · png · webp</span>`}</label>
      <ul class="bdreq">
        <li><b>Transparent background</b> — svg or png</li>
        <li><b>200 px minimum</b> on the short side</li>
        <li>It sits at the top of the title panel</li>
      </ul></div>
    <p class="inote">${S.logo==='weefeen' ? 'The Weefeen mark — the default on the free plan.' : 'The Weefeen mark in the score band stays on the free plan.'}</p>`;
  if(id==='pnl') return head + `
    ${S.aspect === '9/16' ? `
    <div class="irow"><span class="lab">Height</span>
      <div class="slider"><input type="range" min="0" max="100" value="${Math.round(S.portraitOffset*100)}" id="pofs"/>
        <span class="sv num" id="pofsv">${Math.round(S.portraitOffset*100)}%</span></div>
      <span class="why-inline" id="pofswhy">Instagram and TikTok draw their own buttons over the video. Move it clear of them.</span></div>` : ''}
    <div class="irow"><span class="lab">Layout</span>
      <div class="opts" data-g="panel">
        <button class="o${S.panel==='left'?' on':''}" data-v="left"${S.aspect==='9/16'?' disabled':''}>Panel</button>
        <button class="o${S.panel==='centered'?' on':''}" data-v="centered"${S.aspect==='9/16'?' disabled':''}>Centered</button>
        <button class="o${S.panel==='off'?' on':''}" data-v="off">None</button>
      </div>
      ${S.aspect==='9/16' ? '<span class="why-inline">A title column does not fit 1080 across \u2014 it would leave too little picture.</span>' : ''}</div>
    ${S.panel==='left' ? `<div class="irow"><span class="lab">Panel ground</span>${chipRow('bdColor',['#241a33','#0f0d13','#381C53','#f6f1e8'])}
      <span class="why-inline">The panel text ink follows it automatically.</span></div>` : ''}
    <p class="inote">${S.aspect==='9/16'
      ? 'Portrait is the shape Reels and Shorts want. Your recording keeps all of itself \u2014 the space around it is backdrop.'
      : S.panel==='off'
      ? 'With no panel the frame is all video, and the credit sits in the score band.'
      : 'Click any line of panel text in the frame to retype it.'}</p>`;
  return head + `
    <div class="irow"><span class="lab">The paper</span>${chipRow('bandColor',['#f6f1e8','#ffffff','#1c1622','#381C53'])}</div>
    <div class="irow"><span class="lab">The notes</span>${chipRow('noteColor',['#1c1622','#381C53','#f6f1e8','#cc237e'])}</div>
    <div class="irow"><span class="lab">Paper opacity</span>
      <div class="slider"><input type="range" min="0" max="100" value="${S.alpha}" id="alpha"/>
        <span class="sv num" id="alphav">${S.alpha}%</span></div>
      <button class="zero" id="floatBtn">Float the notes</button>
      <span class="why-inline" id="alphawhy"></span></div>
    <div class="irow"><span class="lab">Position</span>
      <div class="opts" data-g="bandPos">
        <button class="o${S.bandPos==='top'?' on':''}" data-v="top">Top</button>
        <button class="o${S.bandPos==='bottom'?' on':''}" data-v="bottom">Bottom</button>
      </div>
      <span class="why-inline">Or drag the band across the frame.</span></div>`;
}
function select(id){
  if(id==='bd' && !bdLive()) id = null;
  const was = sel;
  sel = id;
  const insp = $('#insp');
  $('#insphint')?.classList.toggle('hide', !!id);
  if(!id){
    insp.classList.remove('on');
    if(was !== null && !typing) paint(); else decorate();
    return;
  }
  insp.innerHTML = inspBody(id);
  insp.classList.add('on');
  if(id==='bd') backdrop();
  if(id==='band') alphaCopy();
  /* was nothing selected? the frame must yield the gutter */
  if(was === null && !typing) paint(); else decorate();
  place(id);
}
function deselect(){ if(sel!==null) select(null); }

function place(){ /* docked: the column owns the position */ }

/* ── events ── */
function bindShape(){
  const frame = $('#frame'), insp = $('#insp');
  if(!frame) return;

  /* region clicks (delegated — survives repaints) */
  frame.addEventListener('click', e=>{
    if(e.target.closest('[data-f]')) return;
    if(e.target.closest('.vidzone')){ e.stopPropagation(); deselect(); $('#file')?.click(); return; }
    const id = regionOf(e.target);
    e.stopPropagation();
    select(id && id===sel ? null : id);
  });

  /* in-place text editing: never repaint mid-type or focus is lost */
  frame.addEventListener('focusin', e=>{
    if(e.target.closest('[data-f]')){ typing = true; select('pnl'); }
  });
  frame.addEventListener('input', e=>{
    const el = e.target.closest('[data-f]'); if(!el) return;
    S.text[el.dataset.f] = el.textContent.trim();
    vals();
  });
  frame.addEventListener('keydown', e=>{
    const el = e.target.closest('[data-f]'); if(!el) return;
    if(e.key==='Enter' || e.key==='Escape'){ e.preventDefault(); el.blur(); }
  });
  frame.addEventListener('focusout', e=>{
    if(e.target.closest('[data-f]')){ typing = false; paint(); }
  });

  /* drag the band between top and bottom */
  let drag = null;
  frame.addEventListener('pointerdown', e=>{
    if(e.target.closest('[data-f]')) return;
    if(regionOf(e.target) !== 'band') return;
    drag = { y0:e.clientY, moved:false };
  });
  frame.addEventListener('pointermove', e=>{
    if(!drag) return;
    if(!drag.moved && Math.abs(e.clientY - drag.y0) < 5) return;
    drag.moved = true;
    partsOf('band').forEach(el=>el.classList.add('dragging'));
    const r = frame.getBoundingClientRect();
    drag.want = (e.clientY - r.top) < r.height/2 ? 'top' : 'bottom';
    const dz = $('#dzband');
    const b = partsOf('band')[0]?.getBoundingClientRect();
    if(dz && b){
      const s = $('.stage').getBoundingClientRect();
      dz.style.left = (b.left - s.left)+'px';
      dz.style.width = b.width+'px';
      dz.style.height = b.height+'px';
      dz.style.top = (drag.want==='top'
        ? r.top - s.top
        : r.bottom - s.top - b.height)+'px';
      dz.classList.add('show');
    }
  });
  frame.addEventListener('pointerup', ()=>{
    if(!drag) return;
    $('#dzband')?.classList.remove('show');
    partsOf('band').forEach(el=>el.classList.remove('dragging'));
    if(drag.moved && drag.want && drag.want !== S.bandPos){
      S.bandPos = drag.want; paint(); if(sel) select(sel);
    }
    drag = null;
  });

  /* inspector controls */
  insp.addEventListener('click', e=>{
    if(e.target.closest('[data-x]')){ deselect(); return; }
    const o = e.target.closest('.opts[data-g] .o');
    if(o && !o.disabled){
      const g = o.closest('.opts').dataset.g, was = S[g];
      S[g] = o.dataset.v;
      if(S.bd!=='upload'){ S.bdBad=null; }
      if(g==='logo' && o.dataset.v==='none'){ S.logoBad=null; }
      paint(); select(sel);
      /* the one moment a picker is welcome: just switched to "Your own", nothing chosen yet */
      if(g==='logo' && was!=='upload' && o.dataset.v==='upload' && !S.logoImage && !S.logoBad) $('#logofile')?.click();
      return;
    }
    const c = e.target.closest('.chips[data-g] .chip');
    if(c){ S[c.closest('.chips').dataset.g] = c.dataset.v; paint(); select(sel); return; }
    if(e.target.closest('#floatBtn')){ S.alpha = S.alpha===0?100:0; paint(); select(sel); return; }
  });
  insp.addEventListener('input', e=>{
    if(e.target.id==='alpha'){ S.alpha = +e.target.value; alphaCopy(); paint(); place(sel); }
    // Portrait only: where the video and score sit in the tall frame.
    // Instagram, TikTok and Shorts draw their own buttons over the video and
    // none of them publishes where, so this is the one the person posting has
    // to be able to set for themselves.
    if(e.target.id==='pofs'){
      S.portraitOffset = (+e.target.value) / 100;
      const out = document.getElementById('pofsv');
      if(out) out.textContent = e.target.value + '%';
      paint(); place(sel);
    }
  });

  /* dismiss */
  document.addEventListener('click', e=>{
    if(typing) return;
    /* the inspector re-renders mid-propagation, so the target can already be
       detached — test the composed path, not the live node */
    const path = e.composedPath ? e.composedPath() : [e.target];
    if(!path.some(n=>n && (n.id==='frame' || n.id==='insp'))) deselect();
  });
  document.addEventListener('keydown', e=>{
    if(e.key==='Escape' && !e.target.closest('[data-f]')) deselect();
  });
  addEventListener('resize', ()=>{ if(sel) place(sel); });
  addEventListener('scroll', ()=>{ if(sel) place(sel); }, {passive:true});
}
