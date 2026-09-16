"""Build the demo payload from the real Op.39 package.

Every number the page uses comes from files the pipeline actually produced:
bar positions out of the engraved band SVGs, bar times out of
performance/measures.data, band order out of score/export.json.

Bar boxes are MEASURED IN THE BROWSER, not derived from the path data.
Verovio nests its coordinate systems -- a page-margin translate inside a
root viewBox that the band exporter re-crops -- so reading `d="M6338 ..."`
and dividing by the viewBox width puts every box off by the margin. Here
each band is laid out at the exact raster width and the barlines are read
back with getBoundingClientRect, so the boxes line up with the picture by
construction rather than by arithmetic that has to be kept in step.

The bands ship as ~390 KB of SVG each, 27 MB for this piece, which no single
page can carry; they are rasterised to WebP at display width. Geometry is
taken before that and is unaffected.
"""
import base64, io, json, pathlib, re, shutil, subprocess, sys

PKG = pathlib.Path(r"C:\ZZ_perso\weefeen\PT\RD\music_line_extractor\project"
                   r"\Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH")
HERE = pathlib.Path(__file__).parent
TMP = HERE / "raster"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
WIDTH = 1500
OUT = pathlib.Path(sys.argv[1])

from PIL import Image

TMP.mkdir(exist_ok=True)

MEASURE_JS = """
<style>html,body{margin:0;padding:0;background:#fff}
#w{width:%dpx}#w svg{display:block;width:%dpx;height:auto}</style>
<div id="w">%s</div><pre id="out"></pre>
<script>
var svg=document.querySelector('#w svg'), r=svg.getBoundingClientRect(), res=[];
var ms=svg.querySelectorAll('g.measure');
for(var i=0;i<ms.length;i++){
  var bl=ms[i].querySelector('g.barLine');
  if(!bl) continue;
  var b=bl.getBoundingClientRect();
  if(!b.width && !b.height) continue;
  res.push({x:(b.left+b.width/2-r.left)/r.width,
            y:(b.top+b.height/2-r.top)/r.height});
}
document.getElementById('out').textContent=JSON.stringify({w:r.width,h:r.height,b:res});
</script>
"""


def chrome(args, **kw):
    return subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                           "--force-device-scale-factor=1", "--no-sandbox"] + args,
                          check=True, capture_output=True, timeout=180, **kw)


# ---- real alignment: <seconds>\t<measure>\t<n>\t<n> ----------------------
times = {}
for line in (PKG / "performance" / "measures.data").read_text(encoding="utf-8").splitlines():
    p = line.split("\t")
    if len(p) >= 2:
        times[int(p[1])] = float(p[0])

export = json.loads((PKG / "score" / "export.json").read_text(encoding="utf-8"))
starts = sorted(e["first_measure"] for e in export["entries"])

bands, total, mismatched = [], 0, []
for idx, first in enumerate(starts):
    src = PKG / "score" / "lines" / f"{first}.svg"
    svg = src.read_text(encoding="utf-8")
    vx, vy, vw, vh = [float(x) for x in re.search(r'viewBox="([^"]+)"', svg).group(1).split()]
    height = max(2, round(WIDTH / (vw / vh)))

    # ---- measure the barlines where they actually land -------------------
    probe = TMP / f"probe_{first}.html"
    probe.write_text(MEASURE_JS % (WIDTH, WIDTH, svg), encoding="utf-8")
    dom = chrome(["--virtual-time-budget=4000", "--dump-dom", probe.as_uri()]).stdout.decode("utf-8", "replace")
    got = re.search(r'<pre id="out">(.*?)</pre>', dom, re.S)
    geo = json.loads(got.group(1))

    # a barline outside the band's own strip belongs to another system
    onband = [b for b in geo["b"] if -0.02 <= b["y"] <= 1.02]
    onband.sort(key=lambda b: b["x"])

    bars, left = [], 0.0
    for b in onband:
        bars.append({"a": round(left * 100, 3), "b": round(b["x"] * 100, 3)})
        left = b["x"]
    for k, b in enumerate(bars):
        b["m"] = first + k
        b["t"] = round(times.get(first + k, -1), 3)

    nxt = starts[idx + 1] if idx + 1 < len(starts) else None
    if nxt is not None and bars and bars[-1]["m"] + 1 != nxt:
        mismatched.append((first, bars[-1]["m"] + 1, nxt))

    # ---- raster ----------------------------------------------------------
    flat = TMP / f"{first}.svg"
    shutil.copyfile(src, flat)
    png = TMP / f"{first}.png"
    chrome(["--default-background-color=FFFFFFFF", f"--window-size={WIDTH},{height}",
            f"--screenshot={png}", flat.as_uri()])
    im = Image.open(png).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=82, method=5)
    data = buf.getvalue()
    total += len(data)
    bands.append({"first": first, "aspect": round(im.width / im.height, 4),
                  "bars": bars,
                  "img": "data:image/webp;base64," + base64.b64encode(data).decode()})
    if idx < 3 or idx == len(starts) - 1:
        print(f"  band {first:>4}: {im.width}x{im.height}, {len(data)//1024} KB, "
              f"bars {bars[0]['m']}-{bars[-1]['m']}, "
              f"first box {bars[0]['a']:.1f}%-{bars[0]['b']:.1f}%")

placed = sum(1 for b in bands for x in b["bars"] if x["t"] >= 0)
print(f"{len(bands)} bands, {sum(len(b['bars']) for b in bands)} bars, {placed} with a real time")
print("band/export.json disagreements:", mismatched or "none")
print(f"engraving: {total/1048576:.2f} MB")
OUT.write_text(json.dumps({"bands": bands, "end": max(times.values())},
                          separators=(",", ":")), encoding="utf-8")
print("payload:", round(OUT.stat().st_size / 1048576, 2), "MB")
