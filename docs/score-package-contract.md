# The score package contract

A score package is produced by **music_line_extractor** (MLE) and consumed by
**VideoSync_webapp** (this repo) and by **VideoScoreSync** (VSS). Three
programs read the same folder and none of them owns it, which is exactly the
shape that produces bugs nobody can attribute.

This file records the contract, the places the three have already drifted
apart, and where each rule is enforced. It exists because the same class of
problem arrived twice in one day: something MLE had already solved, in a
code path that does not feed us.

Verified against the two installed packages on 2026-09-13. Where a claim is
a count, it was measured, not assumed.

---

## The layout

```
<folder name>/
    score/
        lines/<first_measure>.{svg,png,jpg}   one image per score band
        export.json                           band geometry
        source.krn                            the Humdrum source
        chroma.npy                            the reference chroma
    reference/                                (a project calls this performance/)
        measures.data                         the alignment
        chroma.npy
```

**The folder name is the identity.** It is character-for-character a key in
the recogniser's `pair_list.json`. Resolution is a dictionary lookup, never a
fuzzy match, because quietly rendering the wrong edition is worse than saying
no. `app/library.py` normalises both sides to Unicode NFC first — a folder
that has passed through macOS comes back NFD and the lookup would miss at
full confidence.

**The band filename is the measure.** `lines/17.svg` is the band that begins
at measure 17. Nothing else records it, so a band named wrongly puts the
wrong music on screen at the wrong moment, silently and plausibly.

*Enforced:* `tools/check_score.py` refuses a package whose band names are not
integers, and reports bands with no alignment point.

---

## Where the three implementations differ

### 1. `measures.data` has two column orders in the wild

| writer | shape |
|---|---|
| `export_package` | `<measure>\t<seconds>` — 2 columns |
| `auto_sync V4` | `<seconds>\t<measure>\t<n>\t<n>` — 4 columns |

**VSS accepts only the 2-column form** and raises on anything else
(`if len(parts) != 2: raise ValueError`). **We accept both**, deciding per
file rather than by configuration: measure numbers are whole and ascend in
small steps, timestamps carry a fraction, so the entirely-integral column is
the measure.

A 4-column package therefore renders here and crashes in VSS. Do not
"simplify" `app/package.py:_read_measures` to match VSS — that would drop
every auto_sync package.

### 2. MLE has TWO `_fix_svg`, and they are not the same

MLE adapts Verovio output for cairosvg in a function called `_fix_svg`. There
is one in `digital_ingest_service.py`, which renders its own pages, and one
in `score_band_worker.py`, which exports the bands we consume. The first does
five things; the second does three.

| fix | in the bands we receive |
|---|---|
| strip `color="red"` from turns | yes — measured 0 occurrences |
| `xlink:href` → `href` | yes — 0 xlink, 689 `href` |
| remove `<g class="dir problem">` | yes — 0 |
| **magenta → grey** | **no** |
| **SMuFL text glyphs** | **no** |

The three that arrive already done are *asserted*, not repeated: a package
that regresses on one is refused rather than rendered wrong. A band still
carrying `xlink:href` would render **with no noteheads at all**, so that one
blocks; the others are reported.

*Enforced:* `app/smufl.py:KNOWN_ARTEFACTS`, checked by `check_score.py` on
three sampled bands. Extend that table, not the algorithm.

### 3. The two MLE leaves to us

**Editor marks.** Verovio colours editor-added notes magenta and labels them
`extra)`. MLE recolours them grey for its pages; the band exporter does not,
so a marked note would reach a finished video at full magenta. `app/svg.py:
adapt()` recolours at render time. Neither installed package contains any —
the MLE decision record says it appears on the Breitkopf editions of the NIFC
corpus, so this is waiting in the corpus, not absent.

**Music glyphs.** Verovio draws almost everything as `<path>`, which needs no
font, but emits a few marks as live text:

```html
<tspan font-family="Leipzig" font-size="503px">&#xECA7;</tspan>
```

U+ECA7 is SMuFL `metNote8thUp`. Verovio embeds Leipzig in the SVG as an
`@font-face` data URI — which is why the file is correct in any browser — and
**cairosvg does not honour `@font-face` at all**; it reads fontconfig and
nothing else. Measured on the render host: the real glyph is 386 dark pixels,
the fallback box is 608.

We unpack the font the score carries and install it. Not a substitution
table: MLE has one and it is right for a desktop app that must not touch a
stranger's operating system, but it covers eight metronome glyphs where the
embedded font carries **642**, and the characters it substitutes to
(U+1D15D and neighbours, the Musical Symbols block) are covered by **no font
on the render host** — half-note tempi would have swapped one box for
another.

Two consequences worth knowing:

- **The bytes come from the package**, never from us, so nothing here
  redistributes a font.
- **fontconfig is read once per process and cached.** Measured: installing a
  font halfway through a render changes nothing for that render. So it is
  unpacked at install time on the web box and installed **at boot** on a
  compute node, before the worker starts. `tools/selftest.py` asserts that
  ordering from the parsed cloud-config.

The general rule, which outlives the specific glyph: **live text in a font
the file does not embed cannot be drawn by anything here**, whatever the
codepoint, because the embedded font is the only one we can guarantee.
`app/smufl.py:undrawable()` reports it before install.

---

## Shipping a package from music_line_extractor

MLE writes these packages, so MLE is the right place to ship one from — a
button there beats an operator finding the folder and running a tool in
another repository. What that button must **not** do is grow a second copy
of what a package has to be. This file exists because three programs have
already drifted apart twice reading the same folder; a second
implementation of the export target would be the third.

**Call the checker. Do not reimplement the transfer.**

```
python tools/check_score.py "<exported package>" --json            # verify
python tools/check_score.py "<exported package>" --install --json  # ship it
```

`--json` prints nothing but JSON on stdout:

```json
{"ok": true, "blocking": 0,
 "publish_as": "Op.23_BALLADE_(Breitkopf)__023-1-BH",
 "recogniser_knows": true,
 "corrected_on_install": 1,
 "problems": [{"level": "BAD|fix|note", "text": "..."}],
 "installed": true}
```

Show `problems` and act on `ok`. Nothing on the MLE side needs to know what
a band filename means, that the reference audio is excluded, or that
`performance/` is renamed on the way up.

### Two reasons it goes over ssh and not straight to the bucket

**Credentials.** `--install` streams a tar to the web box and publishes
from there, so a desktop application needs no bucket key. `scorestore
.publish` states the trade outright: *"Tarring locally and uploading from
an operator's laptop would mean shipping the bucket key to the laptop,
which is not a trade worth making to save one hop."*

**bsdtar.** The `performance/` → `reference/` rename happens on the server
because Windows ships bsdtar, which has no `--transform` **and does not
fail loudly** — the first hand-install produced a package with both
folders and looked like it had worked.

### `publish_as` is the field that decides whether anyone reaches the score

A package is found by its folder name, exactly. `recogniser_knows` says
whether that name is one the recogniser can actually offer, and a request
for a missing score is matched to a published one by string equality on
that name — see `/api/wanted`. A flawless package under a name nobody asks
for is a score no visitor will ever reach, and nothing will report it as
wrong.

### What the export must contain

Read by `app/package.py`; the first path that exists wins.

| | |
|---|---|
| `score/lines/<first_measure>.svg` | one per system, and **the filename is the measure it starts at** |
| `score/export.json` | `band_w`, `band_h`, `options` |
| `score/source.krn` | metadata: `COM`, `OTL`, `OPS`, `PPR`, `PPP` |
| `score/chroma.npy` | the reference chroma — without it nothing can align |
| `reference/measures.data` | the alignment of the reference recording |
| `reference/audio.wav` | may be present; never shipped (111 MB, read by nothing) |

`performance/` is accepted in place of `reference/` and renamed on the
server. `score/measures.data` is created empty by the exporter and is never
read: an alignment belongs to a *performance*, not to a score.


## Why this repo does not reuse VSS's renderer

VSS has `services/overlay_video_encoding_service_dynamic.py`. It is not
reused, deliberately:

| | VSS | here |
|---|---|---|
| band source | `.jpg` only, via `cv2.imread` | `.svg`, rasterised to the band rectangle |
| band assembly | concat demuxer, one image per segment with a duration | a generated strip, seeked per band |
| presentation | fixed layout | aspect, band position, three panel modes, paper/ink colours, transparency, portrait offset, mark, edition credit |

It contains no `svg` and no `cairo`, so it could not have prevented the glyph
bug — and `scan_start_measures` accepts `.jpg` and nothing else, so adopting
it would reintroduce the soft-band-quality problem that vector bands fixed.

**A correction, recorded because the wrong version of it was believed for an
hour:** VSS is *not* a Python frame loop. It builds a band-only MP4 with the
concat demuxer and then does a single-pass ffmpeg overlay; `cv2` appears only
to read the frame rate. The reasons not to adopt it stand, but performance is
not one of them.

### What reading VSS's code was worth

Its commit messages are not a knowledge base — "fixed rabbitmq for big files"
touches a Dropbox token, and six commits called "fix ffprobe issue" are about
labels in a comparison report. The code is.

| what VSS does | us |
|---|---|
| `setpts=PTS-STARTPTS` on every input — a source whose first frame has a non-zero timestamp would drift against the band | already done |
| band video forced to the source frame rate | already done, and clamped to 23–60, which VSS does not do |
| `-t duration` hard cap from the probe | `shortest=1` on the band overlay |
| reads a stream's `rotate` tag | **was missing — see below** |
| — | reads `sample_aspect_ratio`, so anamorphic sources are not squeezed; VSS does not |

**The rotation one was a real bug.** A phone held upright records landscape
and says "turn me": the frames are stored 1920x1080 and the container carries
a display matrix. ffprobe reports the STORED size; ffmpeg applies the matrix
when it decodes. Measured on a 640x360 clip with a 90-degree matrix:

    our probe said   640x360, aspect 1.778  -> landscape
    ffmpeg decoded   360x640                -> portrait

So the layout was computed for one shape and the filter graph handed the
other, for the most ordinary upload there is. `render.quarter_turned()` now
reads `side_data_list` with the legacy tag as a fallback, and the pixel
aspect turns with the frame. Only a QUARTER turn changes the shape — 180
degrees is the same rectangle, and swapping its sides would be the same bug
mirrored.

**What we do share is the alignment**, which is the hard musical part:
`VSS_ROOT` feeds `--vss-root` to the sync runner in `app/sync.py`. That
engine reaches a compute node by rsync at boot, not through the image — see
`deploy/update-vss.sh`.

---

## The rule this file is really for

Every item above was found by reading the other repository **after** the
symptom appeared in a rendered video. The cheap version is to read it first.

When something renders wrong and the SVG looks right, the question is not
"what is wrong with our renderer" but **"what does the pipeline that made
this file already do that we do not"** — and then, specifically, *which copy*
of that pipeline, because MLE has two and they disagree.
