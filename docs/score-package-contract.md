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

## Why this repo does not reuse VSS's renderer

VSS has `services/overlay_video_encoding_service_dynamic.py`. It is not
reused, deliberately:

| | VSS | here |
|---|---|---|
| band source | `.jpg` only, via `cv2.imread` | `.svg`, rasterised to the band rectangle |
| compositing | `cv2.VideoCapture` frame loop in Python | one ffmpeg `-filter_complex` chain |
| presentation | fixed layout | aspect, band position, three panel modes, paper/ink colours, transparency, portrait offset, mark, edition credit |

It contains no `svg` and no `cairo`, so it could not have prevented the glyph
bug — and `scan_start_measures` accepts `.jpg` and nothing else, so adopting
it would reintroduce the soft-band-quality problem that vector bands fixed.
On a node billed by the hour, a Python frame loop over ~18,000 frames is not
a detail.

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
