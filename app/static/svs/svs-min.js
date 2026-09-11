/* Score Video Sync — minimal: state, schematic preview, three moments */

/* source: 'nifc-first-editions' scores carry CC BY 4.0 attribution (ppr/ppp
   mirror the .krn !!!PPR / !!!PPP header fields, kept intact in served files);
   'user-upload' scores carry none. */
let WORKS = [   /* replaced from /api/library by svs-wire.js */
  { id:'sch3', t:'Scherzo No. 3 in C-sharp minor', op:'Op. 39',        bars:660, ref:true,  art:{image:true,  video:true }, src:'nifc-first-editions', ppr:'Breitkopf & Härtel', ppp:'Leipzig' },
  { id:'bal1', t:'Ballade No. 1 in G minor',       op:'Op. 23',        bars:264, ref:true,  art:{image:true,  video:false}, src:'nifc-first-editions', ppr:'Breitkopf & Härtel', ppp:'Leipzig' },
  { id:'bar',  t:'Barcarolle in F-sharp major',    op:'Op. 60',        bars:116, ref:true,  art:{image:true,  video:true }, src:'nifc-first-editions', ppr:'Brandus & Cie', ppp:'Paris' },
  { id:'noc2', t:'Nocturne in E-flat major',       op:'Op. 9 No. 2',   bars:34,  ref:true,  art:{image:false, video:false}, src:'nifc-first-editions', ppr:'M. Schlesinger', ppp:'Paris' },
  { id:'etu3', t:'Étude in E major',               op:'Op. 10 No. 3',  bars:77,  ref:false, art:{image:true,  video:false}, src:'nifc-first-editions', ppr:'Fr. Kistner', ppp:'Leipzig' },
  { id:'pre15',t:'Prelude in D-flat major',        op:'Op. 28 No. 15', bars:89,  ref:false, art:{image:false, video:false}, src:'nifc-first-editions', ppr:'Catelin & Cie', ppp:'Paris' },
];

const FIELDS = [
  ['round','Title','Practice video','span2'],
  ['subtitle','Subtitle','Tokyo',''],
  ['date','Date','Jan 2026',''],
  ['first','First name','Your first name',''],
  ['last','Last name','Last name',''],
  ['country','Country','Poland',''],
  ['age','Age','23',''],
  ['composer','Composer','Fryderyk Chopin','span2'],
  ['work','Work','','span2'],
];

const NAMES = {
  '#f6f1e8':'ivory','#ffffff':'white','#1c1622':'ink','#381C53':'aubergine',
  '#cc237e':'magenta','#241a33':'plum','#0f0d13':'black',
};

const SOURCES = {
  '16/9': { aspect:'16/9', px:'1920 × 1080', label:'16:9' },
  '9/16': { aspect:'9/16', px:'1080 × 1920', label:'9:16' },
  '1/1':  { aspect:'1/1',  px:'1080 × 1080', label:'1:1'  },
};
const S = {
  screen:'pick', file:null, piece:null, recog:'heard', manual:false,
  // `ratio` is the source's REAL shape as a number, filled in from the
  // probe when a file is uploaded. `aspect` is the nearest named one,
  // for the labels only: a phone video is 0.5624, and no caption
  // should say so.
  src:{ aspect:'16/9', ratio:16/9, px:'1920 × 1080', label:'16:9', fps:'30 fps', dur:'7:04', codec:'H.264 · AAC', size:'412 mb' },
  bandPos:'bottom', bd:'colour', bdImage:null, bdPx:null, bdBad:null,
  logo:'weefeen', logoImage:null, logoSrc:null, logoBad:null,
  posters:[], poster:0,
  bandColor:'#f6f1e8', noteColor:'#1c1622', alpha:100, bdColor:'#241a33',
  panel:'off', text:Object.fromEntries(FIELDS.map(([k,,d])=>[k,d])),
  // Where the group sits in a portrait frame. See Style.portrait_offset:
  // the apps draw their interface over the video and do not publish
  // where, so this is the viewer's to set.
  portraitOffset:0.32,
  open:false, email:'',
};
// THE OUTPUT FRAME IS THE RECORDING'S OWN SHAPE - always, by decision.
// There is no chooser: a 16:9 recording makes a 16:9 video and a phone
// recording makes a portrait one, and what changes between them is the
// TEMPLATE, not the dimensions.
//
// This getter was here from the start and was always right. What was wrong
// is that `S.src.aspect` never left its mock default: the probe that comes
// back from the upload was read for its duration and nothing else, so every
// recording was treated as 16:9 - and a portrait one was rendered into a
// landscape frame with most of it cropped away. `setSourceShape` fills it in.
Object.defineProperty(S,'aspect',{get(){return S.src.aspect}});
function nearestFrame(ratio){
  // Nearest of the three we can render, by ratio. A 4:3 recording is closer
  // to square than to widescreen and should not default to 16:9 just
  // because that is the common case.
  const opts = [['16/9', 16/9], ['1/1', 1], ['9/16', 9/16]];
  let best = '16/9', gap = Infinity;
  for (const [name, r] of opts) {
    const d = Math.abs(Math.log(ratio / r));
    if (d < gap) { gap = d; best = name; }
  }
  return best;
}

function setSourceShape(width, height){
  if (!width || !height) return;
  S.src.ratio = width / height;
  S.src.px = `${width} × ${height}`;
  S.src.aspect = nearestFrame(S.src.ratio);
  S.src.label = S.src.aspect.replace('/', ':');
  // The renderer REFUSES a title panel in portrait - a column in a frame
  // 1080 across leaves too little picture to watch. Turned off here rather
  // than left to fail at submit, which would be a render request rejected
  // after the person had finished designing.
  if(S.src.aspect === '9/16') S.panel = 'off';
}
let CANDS = [   /* replaced by the recogniser's answer */['sch3',96],['bal1',62],['bar',41]];
const W = id => WORKS.find(w=>w.id===id);
const P = () => W(S.piece);
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const pieceLabel = () => S.piece ? `${P().t}, ${P().op}` : '';
function scoreSource(){
  const el = $('#scoresrc'); if(!el) return;
  const w = S.piece ? P() : null;
  if(!w || w.src !== 'nifc-first-editions'){ el.innerHTML = ''; el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = `Source: Fryderyk Chopin Institute, <a href="https://chopinscores.org">chopinscores.org</a> `
    + `(<a href="https://creativecommons.org/licenses/by/4.0">CC BY 4.0</a>). First edition: ${w.ppr}, ${w.ppp}. `
    + `<a href="Credits.html">Credits</a>`;
}

/* ── sample output on the landing screen ── */
function sampleHTML(){
  const paper='#f6f1e8', note='#1c1622', bd='#241a33';
  const bandH = 25, pnlW = 27;
  const lines = [0,25,50,75,100].map(t=>
    `<span class="ln" style="top:${t}%;background:${note};opacity:.32"></span>`).join('');
  const half = () => {
    const bars = [1,2,3].map(i=>
      `<span class="br" style="left:${i*25}%;background:${note};opacity:.45"></span>`).join('');
    const n = 10;
    const notes = Array.from({length:n},(_,i)=>{
      const x = (i+.5)/n*100, y = 8+((i*31)%78);
      return `<span class="nh" style="left:${x}%;top:${y}%;width:1.3cqw;height:.95cqw;background:${note}"></span>`
           + `<span class="st" style="left:${x+.6}%;top:${Math.max(0,y-32)}%;height:${Math.min(34,y)}%;background:${note}"></span>`;
    }).join('');
    return `<div class="half">${bars}${notes}</div>`;
  };
  return `
    <div class="bd" style="background:${bd}"></div>
    <div class="pnl" style="width:${pnlW}%;padding:0 2.4cqw;gap:1.5cqw;color:${inkOn(bd)}">
      <span class="hr" style="width:26%"></span>
      <div class="pi"><div class="r" style="font-size:1.5cqw">Practice video</div>
        <div class="sm" style="font-size:1.5cqw">Tokyo</div>
        <div class="sm" style="font-size:1.5cqw">Jan 2026</div></div>
      <div class="pi"><div class="nm" style="font-size:3.1cqw">Your first name</div>
        <div class="nm" style="font-size:3.1cqw">Last name</div>
        <div class="sm" style="font-size:1.5cqw;margin-top:.5cqw">Poland · 23</div></div>
      <div class="pi"><div class="sm" style="font-size:1.5cqw">Fryderyk Chopin</div>
        <div class="wk" style="font-size:1.9cqw">Nocturne, Op. 9 No. 2</div></div>
    </div>
    <div class="band" style="bottom:0;height:${bandH}%;left:${pnlW}%;background:${paper}">
      <div class="stv">${lines}<div class="roll">${half()}${half()}</div>
        <span class="play" style="left:38%"></span>
      </div>
      <span class="wmk" style="font-size:1.35cqw;color:${note}">weefeen</span>
    </div>`;
}

/* ── recognition ── */
function choose(id){
  S.piece = id;
  S.text.work = `${W(id).t}, ${W(id).op}`;
  const a = W(id).art;
  if(S.bd==='image' && !a.image) S.bd='colour';
  if(S.bd==='video' && !a.video) S.bd='colour';
  draw({keepScroll:true});
}
function manualList(){
  const host = $('#pieces'); if(!host) return;
  host.innerHTML = WORKS.map(w=>`
    <button class="piece${w.id===S.piece?' on':''}" data-p="${w.id}">
      <span class="t">${w.t}, ${w.op}</span>
      <span class="m">${w.bars} bars</span>
    </button>`).join('');
  $$('#pieces .piece').forEach(b=>b.onclick=()=>choose(b.dataset.p));
}
function recognise(){
  const lab = $('#piecelab'), hint = $('#piecehint'), body = $('#recogbody');
  const block = $('#pieceblock'), adv = $('#advance');

  block.classList.toggle('on', !!S.file);
  adv.classList.toggle('on', !!S.file && !!S.piece);
  $('#uploadstate').classList.toggle('hide', !!S.file);
  $('#filebar').classList.toggle('on', !!S.file);
  if(S.file){
    $('#filebar').classList.toggle('found', S.recog!=='none');
    $('#fbName').textContent = S.file;
    $('#fbMeta').innerHTML = S.recog==='none'
      ? `${S.src.label} · ${S.src.dur} · <span class="alert">not recognised</span>`
      : `${S.src.label} · ${S.src.dur} · <span class="ok">recognised</span>`;
  }
  if(!S.file) return;

  if(S.recog==='none'){
    lab.innerHTML = '<span class="alert">Not recognised</span>';
    hint.textContent = '';
    body.innerHTML = `
      <p class="heard">We couldn’t place this recording. <b>Is it a Chopin piece?</b> The library is Chopin and nothing else — if it’s another composer, this isn’t the tool for it.</p>
      <div class="manual"><span class="lab">Or find it yourself</span><div class="pieces" id="pieces"></div></div>`;
    manualList();
    return;
  }

  const top = W(CANDS[0][0]);
  lab.textContent = '';
  hint.textContent = '';
  const candBtn = ([id,pc]) => {
    const w = W(id), on = id===S.piece;
    return `<button class="cand${on?' on':''}" data-p="${id}" role="radio" aria-checked="${on}">
      <span class="mark"></span>
      <span class="t">${w.t}, ${w.op}</span>
      <span class="bar"><i style="width:${pc}%"></i></span>
      <span class="pc">${pc}%</span>
    </button>`;
  };
  body.innerHTML = `
    <p class="heard">We heard <b>${top.t}, ${top.op}</b>.<br>It is selected — confirm it, or pick another below.</p>
    <div role="radiogroup" aria-label="Choose the piece">
      <div class="candhead"><span class="lab">Most likely</span><span class="lab">Confidence</span></div>
      <div class="cands lead">${candBtn(CANDS[0])}</div>
      <div class="candhead alt"><span class="lab">Is it wrong? It may also be</span><span class="lab">Confidence</span></div>
      <div class="cands">${CANDS.slice(1).map(candBtn).join('')}</div>
    </div>
    <div class="manualrow"><button class="linkbtn" id="manualToggle">${S.manual?'Hide the library':'None of these three'}</button></div>
    ${S.manual?`<div class="manual"><span class="lab">The full library</span><div class="pieces" id="pieces"></div></div>`:''}`;

  $$('#recogbody .cand').forEach(b=>b.onclick=()=>choose(b.dataset.p));
  $('#manualToggle').onclick = ()=>{ S.manual=!S.manual; recognise(); };
  if(S.manual) manualList();
}

/* ── backdrop availability ── */
function backdrop(){
  const a = S.piece ? P().art : {image:false,video:false};
  if(!$('#bdopts')) return;
  $('#bdopts').innerHTML = `
    <button class="o${S.bd==='colour'?' on':''}" data-v="colour">Plain colour</button>
    <button class="o${S.bd==='upload'?' on':''}" data-v="upload">Your image</button>
    <button class="o${S.bd==='image'?' on':''}" data-v="image" ${a.image?'':'disabled'}>Still artwork</button>
    <button class="o${S.bd==='video'?' on':''}" data-v="video" ${a.video?'':'disabled'}>Looping video</button>`;
  const miss = [!a.image&&'still', !a.video&&'looping'].filter(Boolean);
  const named = S.piece ? P().op : 'this piece';
  $('#bdwhy').textContent = S.bd==='upload'
    ? 'Exactly 16:9, and at least 1920 × 1080 so it stays sharp behind the score.'
    : miss.length
      ? `No ${miss.join(' or ')} artwork is set for ${named} — use a plain colour or your own image.`
      : `Both artworks are set for ${named}.`;

  $('#bdcolourctl')?.classList.toggle('hidden', S.bd!=='colour');
  $('#bdimgctl')?.classList.toggle('hidden', S.bd!=='upload');
  const drop = $('#bdimgdrop');
  if(drop){
    drop.classList.toggle('bad', !!S.bdBad);
    drop.innerHTML = S.bdBad
      ? `<span class="bdimg-name">${esc(S.bdBad.name)}</span><span class="bdimg-err">${esc(S.bdBad.why)}</span><span class="bdimg-swap">Try another</span>`
      : S.bdImage
        ? `<span class="bdimg-name">${esc(S.bdImage)}</span><span class="bdimg-ok">✓ ${esc(S.bdPx||'1920 × 1080')} · 16:9</span><span class="bdimg-swap">Replace</span>`
        : `<span class="bdimg-cta">Drop a 16:9 image, or click to choose</span><span class="lab">jpg · png · webp</span>`;
  }
  const req = $('#bdreq');
  if(req) req.classList.toggle('hidden', S.bd!=='upload');
}

/* ── panel fields ── */
function fields(){
  const host = $('#fields');
  if(!host) return;
  const ctl = host.closest('.ctl');
  if(ctl) ctl.classList.toggle('hidden', S.panel==='off');
  host.classList.toggle('hidden', S.panel==='off');
  if(S.panel==='off'){ host.innerHTML=''; return; }
  host.innerHTML = FIELDS.map(([k,label,,cls])=>`
    <div class="fi ${cls}"><label class="lab" for="f-${k}">${label}</label>
    <input id="f-${k}" data-f="${k}" value="${(S.text[k]||'').replace(/"/g,'&quot;')}" placeholder="—"/></div>`).join('');
  $$('#fields input').forEach(i=>i.oninput=()=>{ S.text[i.dataset.f]=i.value; paint(); });
}

/* ── schematic preview ── */
function geo(){
  const [aw,ah] = S.aspect.split('/').map(Number);
  return { aw, ah,
    band: S.aspect==='9/16' ? 14 : S.aspect==='1/1' ? 19 : 23,
    pnl: S.panel==='left' ? (S.aspect==='9/16' ? 36 : S.aspect==='1/1' ? 31 : 25) : 0,
    bars: S.aspect==='9/16' ? 3 : S.aspect==='1/1' ? 4 : 5,
  };
}
function staveHTML(bars,k,a){
  const inkLines = Array.from({length:5},(_,i)=>
    `<span class="ln" style="top:${(i/4)*100}%;background:${S.noteColor};opacity:${(.3+.12*a).toFixed(2)}"></span>`).join('');
  const barLines = Array.from({length:bars-1},(_,i)=>
    `<span class="br" style="left:${((i+1)/bars)*100}%;background:${S.noteColor};opacity:.42"></span>`).join('');
  const nw = Math.max(3, 5.5*k), nh = Math.max(2.4, 4*k);
  const notes = Array.from({length:bars*3},(_,i)=>{
    const x = ((i+.68)/(bars*3))*100, y = 10+((i*41)%78);
    return `<span class="nh" style="left:${x}%;top:${y}%;width:${nw}px;height:${nh}px;background:${S.noteColor}"></span>`
      + `<span class="st" style="left:calc(${x}% + ${nw*.82}px);top:${Math.max(0,y-46)}%;height:${Math.min(48,y)}%;background:${S.noteColor};opacity:.85"></span>`;
  }).join('');
  return inkLines+barLines+notes;
}
function centeredHTML(w,h){
  const a = S.alpha/100, k = w/560, t = S.text;
  const fs = s => `${Math.max(4,s*k).toFixed(1)}px`;
  const padX = w*.042, pnlW = w*.215;
  let vidW = w - pnlW - padX, vidH = vidW*9/16;
  const gap = Math.max(5, h*.019);
  let bandH = Math.max(30*k, vidH*.32);
  const avail = h - h*.10, total = vidH + gap + bandH;
  if(total > avail){ const s = avail/total; vidW*=s; vidH*=s; bandH*=s; }
  const top = (h - (vidH+gap+bandH))/2;
  const bandTop = S.bandPos==='top';
  const bandY = bandTop ? top : top+vidH+gap;
  const vidY  = bandTop ? top+bandH+gap : top;
  const art = S.bd==='colour' ? '' : `<span class="ctr-art${S.bd==='upload'?' up':''}"></span>`;
  const ctrBd = `<div class="bd" style="background:${S.bdColor}"></div>`;
  const vlab = S.bd==='colour' ? 'your performance' : S.bd==='upload' ? (S.bdImage||'your image') : (S.bd==='image'?'still artwork':'looping artwork');
  const pc = S.bd==='colour' ? inkOn(S.bdColor) : '#fff';

  return `
    ${ctrBd}
    <div class="ctr-pnl" style="width:${pnlW}px;padding:${h*.07}px ${pnlW*.13}px;color:${pc}">
      <div class="ctr-top">
        ${logoSlot(pnlW*.4, 0, true)}
        <span class="pi r"  data-f="round"    style="font-size:${fs(7.5)};margin-top:${h*.035}px">${esc(t.round)}</span>
        <span class="pi nm" data-f="first"    style="font-size:${fs(12.5)}">${esc(t.first)}</span>
        <span class="pi nm" data-f="last"     style="font-size:${fs(12.5)}">${esc(t.last)}</span>
        <span class="pi sm" data-f="subtitle" style="font-size:${fs(7)};margin-top:${h*.012}px">${esc(t.subtitle)}</span>
        <span class="pi sm" data-f="date" style="font-size:${fs(7)}">${esc(t.date)}</span>
        <span class="pi sm nowrap" style="font-size:${fs(7)}"><span data-f="country">${esc(t.country)}</span><span class="sep"> · </span><span data-f="age">${esc(t.age)}</span></span>
        <span class="pi wk" data-f="work"     style="font-size:${fs(8)};margin-top:${h*.03}px">${esc(t.work)}</span>
      </div>
      <div class="ctr-by">
        <span class="pi r" style="font-size:${fs(5.6)};opacity:.45">powered by</span>
        <span class="ctr-wm" style="font-size:${fs(10)};color:${pc}">weefeen</span>
      </div>
    </div>
    <div class="ctr-vid vidzone fill${S.posters[S.poster]?' shot':''}" style="${S.posters[S.poster]?`background-image:url(${S.posters[S.poster]});`:''}left:${pnlW}px;top:${vidY}px;width:${vidW}px;height:${vidH}px">${art}<span class="vidlab"><span class="vl">Video file</span><span class="vf">${esc(S.file||'your video.mp4')}</span><span class="vswap">Click to replace</span></span></div>
    <div class="ctr-band" style="left:${pnlW}px;top:${bandY}px;width:${vidW}px;height:${bandH}px;padding:0 ${Math.max(8,20*k)}px;background:${rgba(S.bandColor,a)}">
      <div class="stv">${staveHTML(5,k,a)}<span class="play" style="left:37%;box-shadow:0 0 ${8*k}px rgba(204,35,126,.75)"></span></div>
      <span class="bandwk" style="font-size:${Math.max(6,6.6*k)}px;color:${S.noteColor}">${esc(S.text.work||'')}${S.piece?' · bars 12–16':''}</span>
    </div>`;
}
function logoSlot(size, mb, round){
  const s = Math.max(22, size);
  const st = `width:${s.toFixed(1)}px;height:${s.toFixed(1)}px;margin-bottom:${(mb||0).toFixed(1)}px`;
  if(S.logo==='weefeen')
    return `<span class="logo wf${round?' round':''}" style="${st}"><svg viewBox="0 0 271.923 276.93"><path fill="currentColor" fill-rule="evenodd" clip-rule="evenodd" d="M135.961 0C60.8743 0 0 61.9968 0 138.46C0 214.933 60.8743 276.93 135.961 276.93C211.048 276.93 271.923 214.933 271.923 138.46C271.923 61.9968 211.048 0 135.961 0ZM190.52 206.493C187.779 197.09 175.254 197.786 160.445 198.608C146.207 199.399 129.856 200.307 118.059 192.469C99.5672 180.196 90.4548 132.541 84.2595 100.141C81.1217 83.7311 78.7321 71.2344 76.2513 69.23C76.1814 68.8286 102.138 76.4552 114.486 135.823C114.818 137.421 115.144 139.019 115.469 140.612C119.201 158.923 122.79 176.529 134.517 186.281C137.344 188.635 141.965 191.396 147.124 192.125C134.74 184.026 128.936 165.076 127.274 146.541C125.051 121.658 122.512 99.6714 107.74 85.472C107.706 85.2994 107.669 85.1238 107.632 84.9479L107.611 84.8486C107.593 84.7641 107.576 84.6796 107.558 84.5955C132.771 91.3346 141.882 117.893 145.432 149.213C145.783 152.299 146.041 155.379 146.294 158.411C147.407 171.709 148.444 184.101 156.947 192.188C159.148 192.123 161.569 192.051 164.081 192.018C156.425 185.164 156.99 171.548 157.588 157.138C157.954 148.299 158.333 139.161 156.835 131.102C154.082 116.27 148.633 104.695 137.419 97.6428C137.559 97.1295 138.23 96.4253 138.433 97.0423C160.675 100.014 174.893 120.905 173.618 145.954C173.382 150.532 172.298 155.54 171.179 160.719C168.915 171.187 166.501 182.348 170.63 192.045C184.432 192.392 197.684 194.929 190.52 206.493Z"/></svg></span>`;
  if(S.logo==='upload' && S.logoImage)
    return S.logoSrc
      ? `<span class="logo shown${round?' round':''}" style="${st}"><img class="logoimg" src="${S.logoSrc}" alt=""/></span>`
      : `<span class="logo on${round?' round':''}" style="${st}"><span class="logoname">${esc(S.logoImage.replace(/\.[a-z0-9]+$/i,''))}</span></span>`;
  /* None: nothing is drawn — the slot only surfaces while the panel is hovered */
  return `<span class="logo empty${round?' round':''}" style="${st}"><span class="logoghost">add<br>logo</span></span>`;
}
function frameHTML(w,h){
  if(S.panel==='centered') return centeredHTML(w,h);
  const g = geo(), a = S.alpha/100, k = w/560; // k = scale factor vs reference width
  const bandH = h*g.band/100, pnlW = w*g.pnl/100;
  const stave = staveHTML(g.bars,k,a);

  const t = S.text, fs = s => `${(s*k).toFixed(1)}px`;
  const panel = S.panel==='left' ? `
    <div class="pnl" style="width:${g.pnl}%;padding:${(h*.07).toFixed(1)}px ${(pnlW*.12).toFixed(1)}px;gap:${(h*.016).toFixed(1)}px;color:${S.bd==='colour'?inkOn(S.bdColor):'#fff'}">
      ${logoSlot(pnlW*.44, h*.022)}
      <span class="hr" style="width:${(pnlW*.24).toFixed(1)}px;margin-bottom:${(h*.02).toFixed(1)}px"></span>
      <span class="pi r"  data-f="round"    style="font-size:${fs(7.5)}">${esc(t.round)}</span>
      <span class="pi sm" data-f="subtitle" style="font-size:${fs(8)}">${esc(t.subtitle)}</span>
      <span class="pi sm" data-f="date" style="font-size:${fs(8)};margin-bottom:${(h*.022).toFixed(1)}px">${esc(t.date)}</span>
      <span class="pi nm" data-f="first"    style="font-size:${fs(15)}">${esc(t.first)}</span>
      <span class="pi nm" data-f="last"     style="font-size:${fs(15)}">${esc(t.last)}</span>
      <span class="pi sm nowrap" style="font-size:${fs(8)};margin-top:${(h*.012).toFixed(1)}px"><span data-f="country">${esc(t.country)}</span><span class="sep"> · </span><span data-f="age">${esc(t.age)}</span></span>
      <span class="pi sm" data-f="composer" style="font-size:${fs(8)};margin-top:${(h*.03).toFixed(1)}px;opacity:.5">${esc(t.composer)}</span>
      <span class="pi wk" data-f="work"     style="font-size:${fs(9)}">${esc(t.work)}</span>
    </div>`
  : S.panel==='off' ? `
    <div class="pnl ghost" style="width:22%"><span class="ghostlab">No title panel<br>click to add one</span></div>` : '';

  const bd = S.bd==='colour'
    ? `<div class="bd" style="background:${S.bdColor}"></div>`
    : `<div class="bd art${S.bd==='upload'?' up':''}" style="left:${g.pnl}%"></div>
       <span class="artlab" style="left:${g.pnl+(100-g.pnl)/2}%">${S.bd==='upload'?esc(S.bdImage||'your image'):S.bd==='image'?'still artwork':'looping artwork'}</span>`;

  const shot = S.posters[S.poster];
  const vid = `<span class="vidzone${S.bd==='colour'?' fill':''}${shot?' shot':''}" style="left:${g.pnl}%${shot?`;background-image:url(${shot})`:''}"><span class="vidlab"><span class="vl">Video file</span><span class="vf">${esc(S.file||'your video.mp4')}</span><span class="vswap">Click to replace</span></span></span>`;
  return `
    ${bd}
    ${vid}
    ${panel}
    <div class="band" style="${S.bandPos==='top'?'top:0':'bottom:0'};height:${bandH}px;left:${pnlW}px;padding:0 ${Math.max(10,26*k)}px;background:${rgba(S.bandColor,a)}">
      <div class="stv">${stave}<span class="play" style="left:37%;box-shadow:0 0 ${8*k}px rgba(204,35,126,.75)"></span></div>
      <span class="bandwk" style="left:0;font-size:${Math.max(6,7.2*k)}px;color:${S.noteColor}">${esc(S.text.work||'')}${S.piece?' · bars 12–16':''}</span>
      <span class="wmk" style="font-size:${Math.max(6,7.6*k)}px;color:${S.noteColor}">weefeen</span>
    </div>`;
}
function esc(s){ return (s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c])); }
function inkOn(hex){ const n=parseInt(hex.slice(1),16);
  return (.299*(n>>16&255)+.587*(n>>8&255)+.114*(n&255))/255 > .6 ? '#1c1622' : '#fff'; }
function rgba(hex,a){ const n=parseInt(hex.slice(1),16); return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`; }
function fit(maxW,maxH){
  const {aw,ah} = geo();
  let w = maxW, h = w*ah/aw;
  if(h>maxH){ h = maxH; w = h*aw/ah; }
  return [Math.round(w), Math.round(h)];
}
function paint(){
  if(typeof typing !== 'undefined' && typing){ vals(); return; }
  const f = $('#frame');
  if(f){
    const stage = $('.stage');
    const sw = stage ? stage.clientWidth : 700;
    /* the inspector floats (position:fixed), so the frame keeps one constant
       size for every option — it must never resize on a click */
    /* only the stills rail is reserved — the inspector docks below the frame */
    const avail = Math.max(280, sw - 14 - 102);
    const cap = Math.max(420, innerHeight - 230);
    const [w,h] = fit(Math.min(avail,1240), cap);
    f.style.width=w+'px'; f.style.height=h+'px';
    f.style.background = S.bd==='colour' ? S.bdColor : '#2b2336';
    f.innerHTML = frameHTML(w,h);
  }
  const m = $('#miniFrame');
  if(m){
    const [w,h] = fit(150,190);
    m.style.width=w+'px'; m.style.height=h+'px';
    m.style.background = S.bd==='colour' ? S.bdColor : '#2b2336';
    m.innerHTML = frameHTML(w,h);
  }
  const px = S.src.px;
  posterRail();
  const cap = $('#stagecap');
  if(cap) cap.textContent = `${px} — schematic. Real notation is set during the render.`;
  vals();
  if(typeof decorate === 'function') decorate();
}

/* ── summary line (the surface at rest) ── */
function vals(){
  const parts = [
    `${S.src.label} from source`,
    `band ${S.bandPos}`,
    S.alpha===0 ? 'notes floating' : `${NAMES[S.bandColor]} paper ${S.alpha}%`,
    `${NAMES[S.noteColor]} notes`,
    S.bd==='colour' ? `${NAMES[S.bdColor]} backdrop` : S.bd==='upload' ? 'your image' : `${S.bd} backdrop`,
    S.panel==='off' ? 'no panel' : S.panel==='centered' ? 'centered' : 'left panel',
  ];
  const v = $('#vals');
  if(v) v.innerHTML = parts.map(p=>`<b>${p}</b>`).join('<i>/</i>');
}

/* ── opacity copy ── */
function lum(hex){ const n=parseInt(hex.slice(1),16); return (0.299*(n>>16&255)+0.587*(n>>8&255)+0.114*(n&255))/255; }
function alphaCopy(){
  if(!$('#alphav')) return;
  $('#alphav').textContent = S.alpha+'%';
  $('#floatBtn')?.classList.toggle('on', S.alpha===0);
  const clash = S.alpha<=22 && S.bd==='colour' && Math.abs(lum(S.noteColor)-lum(S.bdColor))<0.28;
  $('#alphawhy').textContent = clash
    ? `At this opacity the ${NAMES[S.noteColor]} notes sit too close to the ${NAMES[S.bdColor]} backdrop to read. Lighten the notes, or bring the paper back.`
    : S.alpha===0
      ? 'The paper is gone. Notes sit straight on the backdrop — the most graphic of the looks.'
      : S.alpha<45 ? 'The backdrop reads through the paper.'
      : S.alpha<100 ? 'The paper takes a tint from the backdrop behind it.'
      : 'Solid paper behind the notes.';
}

/* ── sync control states ── */
function sync(){
  $$('.opts[data-g]').forEach(g=>[...g.querySelectorAll('.o')]
    .forEach(b=>b.classList.toggle('on', b.dataset.v===S[g.dataset.g])));
  $$('.chips[data-g]').forEach(g=>[...g.querySelectorAll('.chip')]
    .forEach(c=>c.classList.toggle('on', c.dataset.v===S[g.dataset.g])));
  ['bandColor','noteColor','bdColor'].forEach(k=>{
    const el = $('#'+k+'Name'); if(el) el.textContent = NAMES[S[k]]||'';
  });
  const al = $('#alpha');
  if(al){ al.value = S.alpha; alphaCopy(); }
  $$('.view').forEach(v=>v.classList.toggle('on', v.dataset.view===S.panel));
  const si = $('#srcinfo');
  if(si) si.innerHTML = [
    ['Frame', S.src.label],
    ['Size', S.src.px],
    ['Length', S.src.dur],
    ['Rate', S.src.fps],
  ].map(([k,v])=>`<div><span>${k}</span><b>${v}</b></div>`).join('');
  $('#controls')?.classList.add('open');
}

/* ── recap on confirmation ── */
function recap(){
  $('#doneAddr').textContent = S.email || 'you@example.com';
  const va = $('#verifyAddr'); if(va) va.textContent = S.email || 'you@example.com';
  const px = S.src.px;
  $('#recap').innerHTML = [
    ['Piece', `<em>${pieceLabel()}</em>`],
    ['Frame', `${S.src.label} — ${px}, kept from your video`],
    ['Band', `${S.bandPos}${S.alpha===0?', notes floating':`, ${NAMES[S.bandColor]} paper`}`],
    ['Panel', S.panel==='off'?'Off':S.panel==='centered'?'Centered':'Left column'],
    ['Source', S.file||'performance.mp4'],
  ].map(([k,v])=>`<dt>${k}</dt><dd>${v}</dd>`).join('');
}

/* ── screens ── */
const LABELS = { pick:'One of four', shape:'Three of four', verify:'Confirm the address', done:'Done' };
function draw(o){
  $$('section[data-s]').forEach(s=>s.classList.toggle('on', s.dataset.s===S.screen));
  $$('#sw button').forEach(b=>{
    const k = b.dataset.go;
    const on = k==='shape-open'  ? (S.screen==='shape' && S.open)
             : k==='shape'       ? (S.screen==='shape' && !S.open)
             : k==='pick-heard'  ? (S.screen==='pick' && !!S.file && S.recog==='heard')
             : k==='pick-none'   ? (S.screen==='pick' && !!S.file && S.recog==='none')
             : k==='pick'        ? (S.screen==='pick' && !S.file)
             : S.screen===k;
    b.classList.toggle('on', on);
  });
  if(S.screen==='pick'){
    const sf = $('#sampleFrame');
    if(sf && !sf.dataset.built){ sf.innerHTML = sf.innerHTML + sampleHTML(); sf.dataset.built='1'; }
    recognise();
  }
  if(S.screen==='shape'){
    $('#shapePiece').textContent = pieceLabel();
    scoreSource();
    backdrop(); fields(); sync(); paint();
  }
  if(S.screen==='done'){ recap(); paint(); }
  if(S.screen==='verify'){
    const va = $('#verifyAddr'); if(va) va.textContent = S.email || 'you@example.com';
    const rn = $('#resentNote'); if(rn) rn.hidden = true;
  }
  if(!(o&&o.keepScroll)){
    if(S.screen==='shape'){
      const st = $('.stage');
      const y = st ? Math.max(0, st.getBoundingClientRect().top + scrollY - 96) : 0;
      window.scrollTo(0, y);
    } else window.scrollTo(0,0);
  }
}

/* ── events ── */
document.addEventListener('click', e=>{
  const o = e.target.closest('.opts[data-g] .o');
  if(o && !o.disabled){
    const g = o.closest('.opts').dataset.g;
    S[g] = o.dataset.v;
    if(g==='panel') fields();
    if(g==='bd' || g==='panel'){ backdrop(); }
    sync(); paint(); return;
  }
  const c = e.target.closest('.chips[data-g] .chip');
  if(c){ S[c.closest('.chips').dataset.g] = c.dataset.v; sync(); paint(); return; }
  const v = e.target.closest('.view');
  if(v){
    S.panel = v.dataset.view;
    fields(); sync(); paint(); return;
  }
});
$('#toShape').onclick = ()=>{ S.screen='shape'; draw(); };
function emailNote(msg, bad){
  const n = $('#emailnote'); if(!n) return;
  n.textContent = msg || 'First time from this browser? We send one confirmation link before rendering starts.';
  n.classList.toggle('bad', !!bad);
}
$('#submit').onclick = ()=>{
  const v = $('#email').value.trim();
  if(!v || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v)){ emailNote('That address does not look right yet.', true); $('#email').focus(); return; }
  emailNote(); S.email = v;
  S.screen = knownAddress(v) ? 'done' : 'verify';
  draw();
};
/* addresses already confirmed on this browser skip the gate next time */
function knownAddress(a){
  try{ return (JSON.parse(localStorage.getItem('svs.verified')||'[]')).includes(a.toLowerCase()); }
  catch(e){ return false; }
}
function rememberAddress(a){
  try{
    const l = JSON.parse(localStorage.getItem('svs.verified')||'[]');
    if(!l.includes(a.toLowerCase())){ l.push(a.toLowerCase()); localStorage.setItem('svs.verified', JSON.stringify(l)); }
  }catch(e){}
}
['#email'].forEach(s=>{
  $(s).addEventListener('keydown', e=>{ if(e.key==='Enter') $('#submit').click(); });
  $(s).addEventListener('input', ()=>emailNote());
});
$('#resendBtn').onclick = ()=>{ $('#resentNote').hidden = false; };
$('#otherAddr').onclick = ()=>{ S.screen='shape'; draw(); setTimeout(()=>{ $('#email')?.focus(); }, 80); };
$('#simClick').onclick = ()=>{ if(S.email) rememberAddress(S.email); S.screen='done'; draw(); };
$('#againBtn').onclick = ()=>{ S.screen='pick'; S.open=false; draw(); };
$$('#sw button').forEach(b=>b.onclick=()=>{
  const k = b.dataset.go;
  if(k==='shape-open'){ S.screen='shape'; S.open=true; }
  else if(k==='shape'){ S.screen='shape'; S.open=false; }
  else if(k==='pick-heard'){ S.screen='pick'; named(S.file||'performance.mp4'); return; }
  else if(k==='pick-none'){ S.screen='pick'; S.file=S.file||'performance.mp4'; unheard(); return; }
  else if(k==='pick'){ S.screen='pick'; S.file=null; S.piece=null; S.manual=false; $('#drop').classList.remove('filled'); resetDrop(); }
  else S.screen=k;
  if((S.screen==='shape'||S.screen==='done'||S.screen==='verify') && !S.piece){ S.file=S.file||'performance.mp4'; S.recog='heard'; choose(CANDS[0][0]); }
  draw();
});

/* file drop */
function resetDrop(){
  $('#dropcopy').innerHTML = `<h2>Drop your performance video</h2><span class="lab">mp4 · mov · avi · mkv · webm — up to 500 mb</span>`;
}
function fileRow(n, tag){
  $('#drop').classList.add('filled');
}
function centerOn(sel2, delay=120){
  const el = typeof sel2==='string' ? $(sel2) : sel2;
  if(!el) return;
  setTimeout(()=>{
    const r = el.getBoundingClientRect();
    const y = r.top + scrollY - Math.max(24, (innerHeight - r.height)/2);
    const soft = matchMedia('(prefers-reduced-motion: reduce)').matches;
    scrollTo({top:Math.max(0,y), behavior: soft ? 'auto' : 'smooth'});
  }, delay);
}
function named(n){
  S.file = n; S.recog='heard'; S.manual=false;
  S.piece = CANDS[0][0]; S.text.work = `${W(S.piece).t}, ${W(S.piece).op}`;
  fileRow(n, 'recognised');
  draw();
  centerOn('#pieceblock', 240);
}
function unheard(){
  S.recog='none'; S.piece=null; S.manual=false;
  fileRow(S.file, '<span class="alert">not recognised</span>');
  draw();
  centerOn('#pieceblock', 240);
}
['dragover','dragenter'].forEach(ev=>$('#drop').addEventListener(ev,e=>{e.preventDefault();$('#drop').classList.add('hot')}));
['dragleave','drop'].forEach(ev=>$('#drop').addEventListener(ev,e=>{e.preventDefault();$('#drop').classList.remove('hot')}));
/* rights confirmation gate: nothing uploads until it is accepted */
let pendingFile = null, pendingBlob = null;
/* three candidate stills, read straight from the local file — nothing is sent */
function grabPosters(blob){
  S.posters = []; S.poster = 0;
  if(!blob) return;
  const url = URL.createObjectURL(blob);
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.preload = 'metadata'; v.src = url;
  const c = document.createElement('canvas');
  const done = ()=>{ URL.revokeObjectURL(url); v.removeAttribute('src'); v.load?.(); };
  v.onloadedmetadata = ()=>{
    const dur = isFinite(v.duration) && v.duration > 0 ? v.duration : 0;
    const stops = dur ? [dur*0.08, dur*0.27, dur*0.47, dur*0.68, dur*0.88] : [1,3,6,9,12];
    const w = 640, hh = Math.round(w * (v.videoHeight||9) / (v.videoWidth||16));
    c.width = w; c.height = hh;
    let i = 0;
    const shoot = ()=>{
      if(i >= stops.length){ done(); paint(); posterRail(); return; }
      v.currentTime = Math.min(stops[i], Math.max(0, (dur||8) - 0.1));
    };
    v.onseeked = ()=>{
      try{
        c.getContext('2d').drawImage(v, 0, 0, w, hh);
        S.posters.push(c.toDataURL('image/jpeg', 0.82));
      }catch(err){ /* protected or undecodable — placeholders stay */ }
      i++; posterRail(); paint(); shoot();
    };
    shoot();
  };
  v.onerror = ()=>{ done(); S.posters = []; posterRail(); paint(); };
}
function posterRail(){
  const r = $('#posters'); if(!r) return;
  const has = S.posters.length > 0;
  r.innerHTML = `<span class="lab">Still</span>` + [0,1,2,3,4].map(i=>{
    const src = S.posters[i];
    return `<button class="pth${i===S.poster&&has?' on':''}${src?'':' empty'}" data-p="${i}"${src?'':' disabled'}>${
      src ? `<img src="${src}" alt=""/>` : `<span class="pthn">${i+1}</span>`}</button>`;
  }).join('') + (has ? '' : `<span class="pthnote">Five frames appear once a video is chosen.</span>`);
  $$('#posters .pth').forEach(b=>b.onclick=()=>{ S.poster = +b.dataset.p; paint(); posterRail(); });
}
function askRights(name){
  pendingFile = name || 'performance.mp4';
  $('#rightsFile').textContent = pendingFile;
  const ok = $('#rightsOk');
  ok.checked = false;
  $('#rightsGo').disabled = true;
  $('#rights').classList.add('on');
  setTimeout(()=>ok.focus(), 40);
}
function closeRights(){
  $('#rights').classList.remove('on');
  pendingFile = null; pendingBlob = null;
  $('#file').value = '';
}
$('#rightsOk').onchange = e=>{ $('#rightsGo').disabled = !e.target.checked; };
$('#rightsGo').onclick = ()=>{
  const n = pendingFile;
  $('#rights').classList.remove('on');
  pendingFile = null; $('#file').value = '';
  if(n) named(n);
  if(pendingBlob){ grabPosters(pendingBlob); pendingBlob = null; }
};
$('#rightsNo').onclick = closeRights;
$('#rights').addEventListener('click', e=>{ if(e.target.id === 'rights') closeRights(); });
addEventListener('keydown', e=>{
  if(e.key === 'Escape' && $('#rights').classList.contains('on')) closeRights();
});

$('#drop').addEventListener('drop', e=>{ const f = e.dataTransfer?.files?.[0]; if(f) pendingBlob = f; askRights(f?.name); });
$('#browse').onclick = e=>{ e.preventDefault(); $('#file').click(); };
$('#file').onchange = e=>{ const f = e.target.files[0]; if(f){ pendingBlob = f; askRights(f.name); } };

/* backdrop image */
function checkBdImage(file){
  if(!file) return;
  const name = file.name;
  const url = URL.createObjectURL(file);
  const img = new Image();
  img.onload = ()=>{
    const {naturalWidth:w, naturalHeight:h} = img;
    URL.revokeObjectURL(url);
    const ratio = w/h, off = Math.abs(ratio - 16/9);
    if(off > 0.02){
      S.bdImage=null; S.bdPx=null;
      S.bdBad={name, why:`${w} × ${h} is not 16:9`};
    } else if(w < 1920){
      S.bdImage=null; S.bdPx=null;
      S.bdBad={name, why:`${w} × ${h} — below 1920 × 1080`};
    } else {
      S.bdBad=null; S.bdImage=name; S.bdPx=`${w} × ${h}`;
    }
    backdrop(); paint();
  };
  img.onerror = ()=>{
    URL.revokeObjectURL(url);
    S.bdImage=null; S.bdPx=null; S.bdBad={name, why:'could not be read'};
    backdrop(); paint();
  };
  img.src = url;
}
function checkLogoImage(file){
  if(!file) return;
  const name = file.name;
  const keep = ()=>{ if(S.logoSrc) URL.revokeObjectURL(S.logoSrc); S.logoSrc = URL.createObjectURL(file); };
  if(/\.svg$/i.test(name)){ S.logoBad=null; S.logoImage=name; keep(); S.logo='upload'; select('logo'); paint(); return; }
  const url = URL.createObjectURL(file), img = new Image();
  img.onload = ()=>{
    URL.revokeObjectURL(url);
    const w = img.naturalWidth, hh = img.naturalHeight;
    if(Math.min(w,hh) < 200){ S.logoImage=null; S.logoBad={name, why:`${w} × ${hh} — below 200 px`}; }
    else { S.logoBad=null; S.logoImage=name; keep(); S.logo='upload'; }
    select('logo'); paint();
  };
  img.onerror = ()=>{ URL.revokeObjectURL(url); S.logoImage=null; S.logoBad={name, why:'could not be read'}; select('logo'); paint(); };
  img.src = url;
}
$('#bdfile').onchange = e=>checkBdImage(e.target.files[0]);
$('#logofile').onchange = e=>checkLogoImage(e.target.files[0]);
['dragover','dragenter'].forEach(ev=>$('#insp').addEventListener(ev,e=>{
  if(!e.target.closest('#bdimgdrop')) return;
  e.preventDefault(); $('#bdimgdrop').classList.add('hot');
}));
['dragleave','drop'].forEach(ev=>$('#insp').addEventListener(ev,e=>{
  e.preventDefault(); $('#bdimgdrop')?.classList.remove('hot');
}));
$('#insp').addEventListener('drop', e=>{
  if(e.target.closest('#bdimgdrop')) checkBdImage(e.dataTransfer?.files?.[0]);
  if(e.target.closest('#logoimgdrop')) checkLogoImage(e.dataTransfer?.files?.[0]);
});

/* boot */
bindShape();
$('#fbSwap').onclick = ()=>{
  S.file=null; S.piece=null; S.manual=false;
  $('#drop').classList.remove('filled'); resetDrop(); draw();
};
addEventListener('resize', ()=>{ if(S.screen==='shape'||S.screen==='done') paint(); });
draw();
