# VideoSync_webapp

Creates synced score videos: a performance video goes in, and a video with the
matching score band burned in — advancing in time with the playing — comes out.

## How it relates to the other repos

Three repositories, joined by **file contracts, not imports**.

| Repo | Role | Coupling |
|---|---|---|
| `music_line_extractor` | prepares score packages; performs alignment | called as a **subprocess** via its own CLI and interpreter |
| `VideoScoreSync` | the original pipeline | **not used at all** |
| **this repo** | creates the video | — |

Nothing here imports either of the others, and neither is ever modified.

## Input contract

A score package is a folder. Only its `score/` subfolder is read:

```
score/source.krn     the score — the only alignment input
score/lines/         band images, named <first_measure>.svg or .png
score/export.json    manifest (band_w, band_h, options)

reference/           OPTIONAL — a curated reference recording
  audio.wav
  measures.data
```

Two deliberate exclusions:

- **The package's `.spj` is never opened.** It is a large opaque bundle, and
  `score/` already holds everything needed. Each job builds a throwaway
  project from `source.krn` inside its own job folder.
- **`score/measures.data` is never read.** The exporter creates it but never
  fills it — it is always 0 bytes. Alignment belongs to a *performance*, not
  to a score.

Packages are produced by `music_line_extractor`; see `export_package`.

## Two modes

Every job aligns the recording it was given — a package's own alignment
describes its reference performance, not a user's upload.

| Mode | Aligns against | Needs |
|---|---|---|
| `auto` | the score | `score/source.krn` only |
| `reference` | the reference recording, audio-to-audio | a `reference/` folder |

Both route to `music_line_extractor`'s production aligner, selected by
`ACTIVE_ALIGNER` in `services/auto_sync_harness.py`. The workflow names carry
no algorithm version, so promoting a different aligner there needs no change
here. Without a `reference/` folder, `auto` is the only available mode.

## Setup

```bash
pip install -r requirements.txt      # cairosvg is optional; see below
cp .env.example .env                 # then edit the paths
python tools/doctor.py               # checks config, tools and corpus
```

`.env` declares where score packages live, where jobs are written, and where
`music_line_extractor` and its interpreter are. `ffmpeg` and `ffprobe` are
external binaries, not Python packages.

Without `cairosvg`, `.svg` bands cannot be rasterised and the `.png` twins
alongside them are used instead. Installing it lets vector bands render at
exactly the output resolution, which matters at 4K and for 9:16.

## Usage

```bash
python tools/make_video.py --list
python tools/make_video.py --score "Op.39" --video path/to/performance.mp4
python tools/make_video.py --score "Op.39" --video x.mp4 --mode auto \
    --aspect 9/16 --position top --band-fg "#663893"
```

`--seconds N` renders only the first N seconds. Useful for checking style
quickly — but note that truncating the audio degrades alignment badly, so
never judge sync quality from a clipped run.

## Layout

```
app/settings.py   .env-driven configuration
app/package.py    reads a score package (svg or png bands)
app/autosync.py   alignment, via music_line_extractor's CLI
app/render.py     rasterise + recolour bands, build the strip, composite
app/pipeline.py   end-to-end job, mode selection, per-job isolation
app/jobs.py       background job registry for the web layer
app/routes.py     HTTP surface
tools/            doctor, CLI renderer, alignment comparison, mockup import
```

## Verification

`tools/compare_alignments.py` diffs two `measures.data` files measure by
measure. Running `auto` mode against a package whose `reference/measures.data`
was produced by the same workflow should reproduce it **byte for byte** —
that is the correctness bar, not "close enough". A tolerance would hide drift.

Note that validating `reference` mode this way is circular: that mode reads
`reference/measures.data` as an input.

## Nothing is written to a score package

All output goes to the job folder. A package's `reference/` holds the
recording that reference mode aligns against — overwriting it destroys the
reference.
