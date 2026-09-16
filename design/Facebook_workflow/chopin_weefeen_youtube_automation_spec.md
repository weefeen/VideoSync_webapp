# Chopin.Weefeen.com — YouTube + Automated Performance Library
## Product and Technical Implementation Specification

**Document status:** Implementation specification  
**Target:** Claude Code / engineering implementation  
**Product:** Chopin.Weefeen.com  
**Date:** 2026-09-16  
**Primary objective:** Extend the existing Chopin video-upload service into a unified Chopin performance synchronization platform that supports uploaded videos, pasted YouTube links, automatic discovery from the Frédéric Chopin Facebook Group, and background YouTube catalogue enrichment.

---

# 1. Executive Summary

Chopin.Weefeen.com currently provides a workflow centered on an uploaded video: a user uploads a complete Chopin performance, the system identifies/confirms the Chopin work, synchronizes the performance with the score, and produces a rendered output video.

The new architecture must preserve that existing workflow while adding a second major experience:

**Watch a YouTube Chopin performance with an interactive synchronized score.**

A YouTube performance is not re-rendered into a new video. Instead, the original YouTube video is embedded through the official YouTube player and synchronized in real time with the score displayed by Chopin.Weefeen.

The system must also automatically build a growing synchronized Chopin performance library from multiple acquisition sources:

1. User-pasted YouTube links.
2. YouTube links discovered in the Frédéric Chopin Facebook Group.
3. Trusted Chopin-oriented YouTube channels, including institutional and competition sources.
4. YouTube search/discovery used as background enrichment when higher-priority work is not waiting.
5. Existing user-uploaded performances.

The central architectural principle is:

> A **Performance** and its **Synchronization** are the canonical assets.  
> An uploaded file, a YouTube video, or a future media source is only a media source attached to that Performance.

The existing rendered video becomes one possible output for uploaded media, rather than the central data model.

This design creates two complementary assets:

- A public synchronized Chopin performance library.
- A growing technical corpus of audio/video-to-score alignments that can be used for validation, benchmarking, improving piece recognition, improving alignment robustness, and later musical interpretation analysis.

---

# 2. Product Vision

## 2.1 Vision statement

Build the most useful score-centered library of Chopin performances by automatically connecting public performances to the correct digital score and precise musical timing.

A visitor should ultimately be able to:

- upload their own performance and create the current rendered score video;
- paste a YouTube URL and immediately watch it with the synchronized Chopin score;
- browse a Chopin work and see multiple synchronized performances;
- click any measure and jump the video to that exact musical position;
- later compare the same passage across different pianists.

The system should continuously enrich itself in the background.

---

# 3. Product Goals

## 3.1 Primary goals

### G1 — Preserve the current upload product

Do not break the existing uploaded-video workflow.

Uploaded videos must continue to support:

- video upload;
- Chopin work recognition / confirmation;
- synchronization;
- visual output configuration;
- rendered video generation;
- existing downstream delivery behavior.

### G2 — Add YouTube as a first-class media source

A user must be able to paste a YouTube URL.

The system must:

- normalize the URL;
- extract the YouTube video ID;
- validate that the video exists and is usable;
- check whether it is already known;
- identify the Chopin work if needed;
- synchronize it if needed;
- open a web player containing:
  - embedded YouTube video;
  - synchronized score;
  - bidirectional navigation between score position and video time.

### G3 — Build a reusable Chopin performance database

The system must store each unique performance independently of where it was discovered.

Examples of discovery sources:

- manual user paste;
- Facebook Group discovery;
- Chopin Institute / trusted channel discovery;
- YouTube search;
- direct administrative insertion.

The same YouTube video discovered several times must not be synchronized several times.

### G4 — Fully automate acquisition

The long-term design should require no administrator click for each Facebook or YouTube discovery.

New qualifying videos should automatically enter a processing queue.

### G5 — Prioritize work intelligently

The synchronization worker should prioritize:

1. videos currently requested by a real user;
2. newly discovered Facebook Group videos;
3. trusted/curated YouTube sources;
4. high-value/popular Chopin videos;
5. catalogue-coverage enrichment.

### G6 — Create learning/evaluation data

Each synchronization should become reusable structured data:

- source media ID;
- Chopin work;
- score edition / score ID;
- measure timing;
- optional note/onset alignment;
- confidence metrics;
- quality-control result;
- provenance.

The system should retain this independently from presentation/rendering.

---

# 4. Non-Goals for Initial Release

The first implementation does **not** need to solve all future cases.

Do not block V1 on:

- native Facebook-hosted video playback;
- arbitrary non-YouTube streaming services;
- multi-work recital videos;
- masterclasses containing speech and multiple excerpts;
- incomplete fragments if the existing alignment system requires complete pieces;
- perfect performer identification;
- automatic musical interpretation scoring;
- cross-platform copyright acquisition;
- real-time/live score following;
- user accounts for basic YouTube viewing;
- large-scale comparison analytics.

These can be added later without changing the core Performance model.

---

# 5. Core Product Model

## 5.1 Canonical concept: Performance

A **Performance** represents one musical performance of one Chopin work.

It should not be synonymous with a file.

Conceptually:

```text
Performance
    |
    +-- Chopin Work
    |
    +-- Performer metadata
    |
    +-- one or more Media Sources
    |      +-- Uploaded file
    |      +-- YouTube video
    |      +-- future source
    |
    +-- Synchronization
    |      +-- score ID / edition
    |      +-- measure timings
    |      +-- optional note timings
    |      +-- confidence / QC
    |
    +-- Outputs
           +-- interactive synchronized player
           +-- rendered MP4, when applicable
```

## 5.2 Critical design rule

Do **not** create one data model for uploads and an unrelated model for YouTube.

Both should converge to the same Performance + Synchronization objects.

---

# 6. User-Facing Product Structure

The homepage/product should present two primary actions.

## 6.1 Action A — Create a score video

This is the current upload experience.

Suggested wording:

**Create a score video**  
Upload your Chopin performance and create a synchronized video with the score.

Primary CTA:

`Upload a performance`

## 6.2 Action B — Watch with score

New experience.

Suggested wording:

**Watch with the score**  
Paste a Chopin YouTube performance and follow it with the synchronized score.

Components:

```text
[ Paste a YouTube URL........................ ]
[ Watch with score ]
```

If the video has already been synchronized, the result should open immediately.

If it is not yet synchronized, the interface should clearly show processing status.

## 6.3 Future Action C — Explore Chopin

Later, but the data architecture must support it now.

Examples:

- browse by work;
- browse by genre;
- browse by opus;
- show synchronized performances per work;
- choose a performer;
- compare a passage.

---

# 7. Recommended Page Architecture

## 7.1 Homepage

Keep the current brand and visual identity.

Replace a single upload-only concept with two clear paths.

Example structure:

```text
THE SCORE, MOVING WITH YOUR CHOPIN.

[ Create ]
Upload your own performance and create a synchronized score video.
[ Upload performance ]

[ Watch ]
Paste a YouTube Chopin performance and follow it with the score.
[ YouTube URL................................ ][ Open ]
```

Do not force YouTube users through the existing render-oriented four-step wizard.

## 7.2 Upload workflow pages

Preserve the current user journey as much as possible.

Potential route structure:

```text
/create
/create/upload
/create/identify
/create/appearance
/create/render
/create/result/{performanceId}
```

Existing routes can remain if changing them creates unnecessary migration risk.

## 7.3 YouTube workflow pages

Recommended:

```text
/watch
/watch/{performanceId}
```

or

```text
/p/{publicId}
```

The canonical result page should be source-neutral.

Example:

```text
/p/{publicPerformanceId}
```

The page determines whether the media player is:

- a local/upload player;
- YouTube IFrame player;
- future supported player.

## 7.4 Work page

Prepare now for:

```text
/work/{workSlug}
```

Example:

```text
/work/ballade-no-1-op-23
```

Contains:

- title;
- catalogue information;
- canonical score;
- list of available synchronized performances.

This does not need full V1 UI, but the data model should support it.

---

# 8. YouTube User Workflow

## 8.1 User pastes a URL

Supported inputs should include at least common YouTube URL forms:

```text
https://www.youtube.com/watch?v=VIDEO_ID
https://youtu.be/VIDEO_ID
https://www.youtube.com/shorts/VIDEO_ID
https://www.youtube.com/embed/VIDEO_ID
```

Normalize all forms to:

```text
provider = youtube
external_media_id = VIDEO_ID
```

## 8.2 Lookup before processing

Immediately query by:

```text
(provider = youtube, external_media_id = VIDEO_ID)
```

Possible cases:

### Case A — Already synchronized

Open synchronized player immediately.

### Case B — Known and currently processing

Show current job status.

Example:

```text
We are preparing this performance.
Identifying the work...
Synchronizing with the score...
```

The page should poll or subscribe to status changes.

### Case C — Known but failed

Show a meaningful status and optionally permit retry depending on failure type.

### Case D — Completely new

Create MediaSource + discovery record + processing job.

Do not require the user to wait on the HTTP request.

Return a performance/job ID immediately.

---

# 9. YouTube Validation

Use official YouTube metadata APIs where appropriate.

Validation should determine:

- valid YouTube video ID;
- video exists;
- current availability;
- title;
- description;
- channel ID;
- channel title;
- thumbnail;
- published date;
- duration if available;
- whether embedding is permitted where reliably exposed;
- live/upcoming state when relevant.

Persist a metadata snapshot and a `last_checked_at` timestamp.

Do not make the display page depend on a metadata API request on every view.

---

# 10. YouTube Playback + Score Synchronization

## 10.1 Player

Use the official YouTube IFrame Player API.

Required capabilities:

- load video by ID;
- play;
- pause;
- read current playback time;
- seek to a specific time;
- receive state-change events.

## 10.2 Score follows video

While video is playing:

1. read current video time periodically;
2. map current time to synchronization segment;
3. determine current measure / score position;
4. highlight current measure;
5. scroll or page the score when necessary.

Avoid over-aggressive auto-scroll.

Recommended behavior:

- current measure visually highlighted;
- score auto-scrolls only when the active measure is approaching the visible boundary;
- manual user scroll temporarily suppresses auto-scroll for a short period unless playback moves far outside the visible area.

## 10.3 Video follows score

When user clicks a measure:

```text
measure -> synchronization timestamp -> player.seekTo(timestamp)
```

If timing contains a range:

```text
measure.start_time
```

should normally be used.

## 10.4 Timing resolution

V1 minimum:

- measure-level timing.

Preferred if already produced by the synchronization engine:

- onset/note-level timing retained internally.

Even if V1 UI only displays measure-level following, do not throw away finer timing.

---

# 11. Unified Upload + YouTube Architecture

## 11.1 Upload source

```text
MediaSource
provider = upload
storage_uri = ...
```

## 11.2 YouTube source

```text
MediaSource
provider = youtube
external_media_id = ...
external_url = canonical URL
```

## 11.3 Shared downstream pipeline

Both sources converge to:

```text
MediaSource
    ->
audio representation
    ->
work identification
    ->
score resolution
    ->
synchronization
    ->
QC
    ->
Performance
    ->
Synchronization
```

## 11.4 Output differences

Uploaded video:

```text
Interactive page
+
optional/current rendered MP4 output
```

YouTube:

```text
Interactive page only
```

Do not download/re-render a YouTube video as the product behavior.

---

# 12. Proposed Data Model

Names may be adapted to the existing schema.

## 12.1 `chopin_work`

```text
id
slug
title
subtitle
opus
catalogue_number
genre
key
movement
canonical_score_id
active
created_at
updated_at
```

If an existing work/piece table already exists, reuse it.

## 12.2 `score`

```text
id
work_id
edition_name
source
version
score_asset_uri
musicxml_uri
mei_uri
pdf_uri
metadata_json
is_canonical
created_at
updated_at
```

Only store fields relevant to the current score stack.

## 12.3 `performance`

```text
id
public_id
work_id nullable until identified
performer_name nullable
performance_date nullable
title
status
visibility
primary_media_source_id nullable
created_at
updated_at
published_at nullable
```

Suggested `status`:

```text
DISCOVERED
VALIDATING
IDENTIFYING
NEEDS_WORK_CONFIRMATION
READY_FOR_SYNC
SYNCHRONIZING
QC
READY
FAILED
REJECTED
UNAVAILABLE
```

## 12.4 `media_source`

```text
id
performance_id nullable during early discovery
provider                 # upload | youtube | future
external_media_id        # YouTube video ID
canonical_url
storage_uri              # uploaded content
original_filename
mime_type
duration_ms
title
description
channel_id
channel_name
thumbnail_url
metadata_json
availability_status
last_checked_at
created_at
updated_at
```

Unique constraint for YouTube:

```text
UNIQUE(provider, external_media_id)
```

## 12.5 `synchronization`

```text
id
performance_id
score_id
algorithm_version
status
confidence
quality_score
started_at
completed_at
created_at
updated_at
```

## 12.6 `synchronization_measure`

```text
id
synchronization_id
measure_index
measure_label
score_position
start_ms
end_ms
confidence
metadata_json
```

Index:

```text
(synchronization_id, measure_index)
```

## 12.7 Optional `synchronization_event`

For note/onset-level data:

```text
id
synchronization_id
event_type
score_event_id
time_ms
confidence
metadata_json
```

## 12.8 `discovery`

A performance can be discovered multiple times.

```text
id
media_source_id
source_type
source_reference
source_url
discovered_at
priority
metadata_json
```

`source_type` examples:

```text
USER_PASTE
USER_UPLOAD
FACEBOOK_GROUP
TRUSTED_YOUTUBE_CHANNEL
YOUTUBE_SEARCH
ADMIN
```

This is important because discovery provenance must not be confused with media identity.

The same YouTube video may have:

```text
discovery #1 = YOUTUBE_SEARCH
discovery #2 = FACEBOOK_GROUP
discovery #3 = USER_PASTE
```

but only one:

```text
media_source(provider=youtube, external_media_id=...)
```

and one synchronization.

## 12.9 `processing_job`

```text
id
performance_id
job_type
priority
status
attempt_count
max_attempts
available_at
started_at
finished_at
worker_id
error_code
error_message
metadata_json
created_at
updated_at
```

Possible `job_type`:

```text
VALIDATE_MEDIA
IDENTIFY_WORK
EXTRACT_AUDIO
SYNCHRONIZE
QUALITY_CHECK
PUBLISH
REFRESH_METADATA
```

If an existing Camunda/process engine or queue architecture already manages these stages, use that rather than duplicating orchestration.

---

# 13. Deduplication

Deduplication is mandatory.

## 13.1 YouTube identity

Canonical identity:

```text
youtube:{videoId}
```

Do not deduplicate by full URL because the same ID may appear in multiple URL formats.

## 13.2 Discovery deduplication

Multiple Facebook posts may share the same YouTube video.

Keep all discovery events if useful, but synchronize only once.

## 13.3 Concurrency protection

Two requests arriving simultaneously for the same new video must not create two media records.

Use:

- database unique constraint;
- transaction / upsert;
- idempotent ingestion endpoint.

---

# 14. Priority System

## 14.1 Priority principle

The next unit of expensive synchronization capacity should always go to the highest-value candidate.

Suggested ranking:

```text
P0 — Interactive user request currently waiting
P1 — Newly discovered Facebook Group video
P2 — Trusted institutional / curated channel
P3 — High-value YouTube discovery
P4 — Catalogue coverage enrichment
```

The exact integer scale can be chosen by implementation.

Example:

```text
1000 = waiting user
800  = Facebook
600  = trusted channel
400  = popularity discovery
200  = catalogue gap
```

These are implementation examples, not immutable business values.

## 14.2 Dynamic reprioritization

If a background YouTube video is queued at low priority and then a user pastes it, increase the existing job priority rather than creating another job.

Similarly, if a background-discovered video later appears in the Facebook Group, update priority.

---

# 15. Processing Pipeline

Recommended orchestration:

```text
DISCOVER
  |
  v
DEDUPLICATE
  |
  v
VALIDATE MEDIA
  |
  v
CLASSIFY / QUALIFY
  |
  v
IDENTIFY CHOPIN WORK
  |
  +--> confidence insufficient -> manual/admin review queue
  |
  v
RESOLVE SCORE
  |
  v
PREPARE AUDIO
  |
  v
SYNCHRONIZE
  |
  v
AUTOMATED QUALITY CONTROL
  |
  +--> pass -> READY
  |
  +--> uncertain -> REVIEW
  |
  +--> fail -> FAILED
```

---

# 16. Work Identification

The identification system should determine:

- Is this Chopin?
- Which work?
- Which movement, if applicable?
- Is it likely a complete supported performance?
- Confidence.

Use all inexpensive evidence before running expensive audio processing:

- YouTube title;
- description;
- channel;
- known catalogue vocabulary;
- opus/catalogue numbers;
- duration plausibility;
- existing metadata models.

Audio-based identification can be used where required.

## 16.1 Confidence thresholds

Use configurable thresholds, not hard-coded values.

Conceptually:

```text
HIGH confidence -> automatic
MEDIUM confidence -> deeper verification
LOW confidence -> review/reject
```

Persist:

```text
identified_work_id
identification_confidence
identification_method
identification_model_version
```

This makes future evaluation possible.

---

# 17. Qualification Rules

For the first production version, prioritize content compatible with the current synchronization engine.

A candidate should preferably be:

- one Chopin work or movement;
- sufficiently complete;
- primarily musical performance;
- suitable audio quality;
- not a long compilation;
- not an interview/masterclass dominated by speech.

Use a reason code when skipping:

```text
NOT_CHOPIN
MULTIPLE_WORKS
PARTIAL_UNSUPPORTED
SPEECH_DOMINATED
VIDEO_UNAVAILABLE
EMBED_UNAVAILABLE
DUPLICATE
SYNC_UNSUPPORTED
OTHER
```

A skipped candidate should remain recorded so the discovery system does not repeatedly retry it.

---

# 18. Synchronization Engine Contract

The web product must not depend directly on implementation details of the current alignment algorithm.

Create a clear service contract.

Input:

```json
{
  "performance_id": "...",
  "work_id": "...",
  "score_id": "...",
  "media_source": "...",
  "algorithm_profile": "..."
}
```

Output conceptually:

```json
{
  "status": "SUCCESS",
  "algorithm_version": "...",
  "confidence": 0.0,
  "measures": [
    {
      "measure_index": 1,
      "start_ms": 0,
      "end_ms": 5300,
      "confidence": 0.0
    }
  ],
  "events": [],
  "quality_metrics": {}
}
```

Do not make callers parse arbitrary log files.

Persist algorithm version with every result.

---

# 19. Automatic Quality Control

Before a synchronization becomes publicly READY, run automatic validation.

Possible checks:

- timestamps monotonically increase;
- no impossible negative timing;
- expected score coverage;
- final measure occurs within plausible distance from media end;
- no massive unexplained gaps;
- duration ratio plausible;
- alignment confidence above configurable minimum;
- estimated skipped sections within acceptable limit;
- consistency with known work duration distribution once enough corpus data exists.

Do not use only one global score.

Persist individual QC metrics.

This corpus will later make QC progressively stronger.

---

# 20. Interactive Result Page

## 20.1 Desktop layout

Recommended default:

```text
------------------------------------------------------------
Chopin — Ballade No. 1 in G minor, Op. 23
Performer / source metadata
------------------------------------------------------------
|                         |                                |
|       VIDEO PLAYER      |          SCORE VIEWER          |
|                         |                                |
|                         |        active measure          |
|                         |                                |
------------------------------------------------------------
Playback / score controls
Other performances of this work (future)
------------------------------------------------------------
```

## 20.2 Mobile layout

Stack vertically:

```text
Video
Score
```

Keep video visible/sticky where practical while score scrolls.

Do not attempt a tiny two-column layout on narrow screens.

## 20.3 Current measure

The current measure should have:

- visible highlight;
- optional playhead marker;
- accessible state.

## 20.4 Measure click

Clicking/tapping a measure:

1. resolve `start_ms`;
2. seek YouTube/local player;
3. update highlight;
4. continue playback based on current player state / chosen UX rule.

## 20.5 Share

Each READY performance receives a stable public URL.

Example:

```text
https://chopin.weefeen.com/p/{publicId}
```

Do not expose sequential internal DB IDs if avoidable.

---

# 21. User Status UX

Processing may take time.

The UI must not appear frozen.

States should map to user-friendly text.

Example:

```text
VALIDATING
Checking the video…

IDENTIFYING
Identifying the Chopin work…

SYNCHRONIZING
Synchronizing the performance with the score…

QC
Checking the synchronization…

READY
Ready.

FAILED
We could not synchronize this performance automatically.
```

If the system already knows the work:

```text
Synchronizing Chopin — Ballade No. 1…
```

Prefer Server-Sent Events/WebSocket if already available; otherwise polling is acceptable for V1.

---

# 22. API Design

Exact route conventions should follow the existing Symfony application.

## 22.1 Submit YouTube URL

```http
POST /api/performances/youtube
```

Request:

```json
{
  "url": "https://www.youtube.com/watch?v=..."
}
```

Response if known and ready:

```json
{
  "performanceId": "...",
  "publicId": "...",
  "status": "READY",
  "redirectUrl": "/p/..."
}
```

Response if queued:

```json
{
  "performanceId": "...",
  "publicId": "...",
  "status": "VALIDATING"
}
```

Must be idempotent by YouTube video ID.

## 22.2 Get performance state

```http
GET /api/performances/{publicId}
```

Response:

```json
{
  "id": "...",
  "status": "SYNCHRONIZING",
  "work": {
    "id": "...",
    "title": "..."
  },
  "media": {
    "provider": "youtube",
    "externalMediaId": "..."
  }
}
```

## 22.3 Get synchronization

```http
GET /api/performances/{publicId}/synchronization
```

Return only when allowed/ready.

## 22.4 Internal ingestion API

For automated collectors:

```http
POST /api/internal/discoveries
```

Authenticated internal request:

```json
{
  "provider": "youtube",
  "externalMediaId": "...",
  "canonicalUrl": "...",
  "discovery": {
    "sourceType": "FACEBOOK_GROUP",
    "sourceReference": "...",
    "sourceUrl": "..."
  }
}
```

Must be idempotent.

Protect it with an internal service credential/signature.

---

# 23. Facebook Group Automatic Discovery

## 23.1 Important platform constraint

As of the current design date, Meta no longer provides the historical Facebook Groups API capabilities that previously allowed an app to access Group content through the former Groups API permissions. Therefore, do not design the implementation around an official “new Group post” webhook.

The Facebook acquisition component must be isolated from the core product because this is the least stable integration.

## 23.2 Functional requirement

The collector should inspect the **published Group feed**, not only the moderation/pending queue.

Reason:

Some members can have content published without an individual approval event. Monitoring only pending posts would miss these.

## 23.3 Collector responsibilities

The collector must do as little as possible:

```text
open/observe group
    ->
find newly published posts
    ->
extract YouTube URLs/video IDs
    ->
emit discovery events
```

It must **not** contain:

- work recognition;
- synchronization logic;
- score logic;
- user-facing state.

Those belong to the core application.

## 23.4 Collector isolation

Recommended separate service/component:

```text
facebook-collector
```

Output:

```http
POST /api/internal/discoveries
```

If Facebook changes UI/behavior, only this component should require repair.

## 23.5 Reliability strategy

Do not rely solely on “the newest single post.”

Maintain an overlap scan.

Conceptual behavior:

```text
scan recent published items
compare with seen post references
extract candidate URLs
upsert discoveries
```

The overlap prevents missed posts after:

- collector restart;
- temporary failure;
- feed reordering;
- network interruption.

## 23.6 State

Persist at least:

```text
facebook_post_reference
facebook_post_url if obtainable
first_seen_at
last_seen_at
youtube_video_id
ingested_at
```

## 23.7 Account/session safety

Do not embed personal Facebook credentials in source code.

Session secrets belong in a secret store/environment-specific secure storage.

Provide:

- health state;
- “authentication/session expired” alert;
- collector disabled switch.

## 23.8 Compliance gate

Because the Facebook component depends on accessing Facebook’s web interface rather than the removed Groups API, treat deployment of automated collection as a component requiring explicit review against current Meta terms before production rollout.

This compliance gate must not block implementation of the core YouTube product or generic discovery API.

---

# 24. YouTube Background Discovery

Background YouTube enrichment should operate independently from the Facebook collector.

## 24.1 Trusted channel ingestion

Maintain a table/configuration:

```text
trusted_youtube_channel
id
youtube_channel_id
name
priority
enabled
last_scan_at
```

For known channels, enumerate uploads using official YouTube APIs.

Prefer channel upload playlist enumeration rather than repeatedly doing a generic search for the channel.

## 24.2 Search discovery

Use official YouTube search capabilities to find candidates such as:

```text
Chopin Ballade No. 1
Chopin Op. 23
Chopin Etude Op. 10 No. 4
```

Search can also use popularity/order metadata where appropriate.

Do not equate high view count with high value.

## 24.3 Catalogue coverage discovery

Create coverage metrics by Chopin work:

```text
work_id
ready_performance_count
trusted_source_count
recent_count
```

Background discovery can focus on underrepresented works.

## 24.4 Background scheduler

Pseudo-policy:

```text
if P0/P1 queue exists:
    do not start new low-priority discovery sync
else:
    select highest-value unsynchronized candidate
    enqueue
```

Do not overload compute/storage simply because more candidates exist.

## 24.5 Candidate scoring

Possible features:

- source trust;
- Facebook relevance;
- active user request;
- work coverage gap;
- view count/popularity;
- title confidence;
- complete-work likelihood;
- embeddability/availability;
- duplication;
- expected processing cost.

Keep weights configurable.

---

# 25. Trusted Chopin Sources

The implementation should support configuring high-value channels rather than hard-coding names.

Examples may include:

- Fryderyk Chopin Institute official sources;
- Chopin competition channels;
- recognized archives;
- curated performance channels.

Store stable YouTube channel IDs, not only channel display names.

Admin should be able to:

- add/remove trusted source;
- enable/disable;
- set source priority.

---

# 26. “Already Prepared” Behavior

A major desired benefit is precomputation.

Scenario:

1. Background discovery synchronizes YouTube video `ABC`.
2. Weeks later a Facebook member posts `ABC`.
3. Collector detects `ABC`.
4. Upsert finds existing media.
5. New discovery provenance is stored.
6. No new synchronization is run.
7. Performance is immediately usable.

Likewise:

1. A user pastes `ABC`.
2. Database finds READY.
3. Result page opens immediately.

This should be a primary acceptance test.

---

# 27. Performance Library

Even if the full browse UI is delayed, implement queries needed for it.

Required relation:

```text
work -> performances
```

Future API:

```http
GET /api/works/{workId}/performances
```

Potential filters:

- READY only;
- source;
- performer;
- channel;
- date;
- trusted source.

---

# 28. Future Passage Comparison

Do not implement in initial milestone unless trivial, but preserve the capability.

Use case:

User is at measure 83 of Ballade No. 1.

UI can later show:

```text
Compare measure 83

Performance A
Performance B
Performance C
```

Selecting another performance:

```text
same work
same measure
-> otherSynchronization.measure[83].start_ms
-> load/seek other player
```

This is one of the major reasons measure-based synchronization data must be normalized.

---

# 29. Existing Rendered Video Workflow

Do not remove rendering.

Instead, model it as an Output/Artifact.

Possible schema:

```text
render_output
id
performance_id
synchronization_id
layout_profile
storage_uri
status
created_at
```

The current upload flow can continue to automatically create this output.

A YouTube source should normally **not** request a rendered-video output.

This prevents YouTube behavior from becoming entangled with current render logic.

---

# 30. Migration from Current Application

Claude Code should begin by inspecting the existing codebase before changing schema.

## 30.1 Discovery tasks

Identify:

- current upload controller/routes;
- current user/session model;
- current `piece/work` database model;
- score asset model;
- synchronization job implementation;
- current processing orchestration;
- current render job implementation;
- current upload status model;
- existing REST/AJAX conventions;
- frontend stack used on Chopin.Weefeen;
- storage mechanism;
- email notification logic;
- process engine / Camunda integration if active in this application;
- existing video/audio conversion services.

## 30.2 Reuse first

Do not duplicate existing concepts.

If the current application already has:

```text
Video
Piece
SynchronizationJob
Score
RenderJob
```

prefer migrations/refactoring/adapters rather than replacing everything at once.

## 30.3 Backward compatibility

All current uploaded-video records must remain usable.

If introducing `performance` and `media_source`, migrate existing video records or provide a compatibility mapping.

Existing public URLs must continue working unless there is a deliberate redirect strategy.

---

# 31. Queue / Orchestration

The pipeline is asynchronous.

Do not run synchronization inside a web request.

Recommended separation:

```text
web application
    ->
job enqueue
    ->
worker/orchestrator
    ->
status persisted
    ->
UI observes status
```

If the current system already uses Camunda for multi-stage workflows, evaluate whether this pipeline should be represented in the existing orchestration layer rather than introducing another workflow framework.

Requirements regardless of implementation:

- retries;
- idempotency;
- priority;
- timeout handling;
- state persistence;
- failure reason;
- worker observability.

---

# 32. Retry Strategy

Different failures require different retry behavior.

Examples:

## Transient

- network failure;
- API timeout;
- temporary media fetch error;
- worker crash.

Retry automatically with backoff.

## Permanent

- deleted YouTube video;
- unsupported content;
- confidently non-Chopin;
- no matching score.

Do not infinitely retry.

## Algorithmic uncertainty

- alignment failed;
- identification ambiguous.

Mark for review / future retry with newer algorithm version.

Persist:

```text
failure_class
error_code
algorithm_version
```

---

# 33. Versioning

Every synchronization should retain:

```text
algorithm_version
score_version
identification_version
qc_version
```

Reason:

When the alignment algorithm improves, the system should be able to select older low-quality synchronizations for reprocessing.

Future operation:

```text
reprocess all syncs where
algorithm_version < X
and quality_score < threshold
```

---

# 34. Corpus / Learning Layer

The database should support using READY synchronizations as an evaluation corpus.

For each synchronization retain:

- work ID;
- exact score version;
- source media;
- source provenance;
- timing;
- algorithm version;
- QC metrics;
- human corrections if any;
- final accepted status.

Do not automatically train models directly from every machine-produced alignment.

Distinguish:

```text
AUTO
AUTO_VERIFIED
HUMAN_CORRECTED
GOLD
```

This is important to avoid teaching future systems from unverified errors.

---

# 35. Admin Interface

Minimum admin tools:

## Performance list

Columns/filters:

- title;
- work;
- provider;
- discovery source;
- status;
- quality;
- created/discovered time;
- algorithm version.

## Performance detail

Show:

- YouTube/upload metadata;
- discovery provenance;
- identification result;
- synchronization status;
- QC metrics;
- error logs;
- retry/reprocess button;
- work override;
- reject/unpublish action.

## Review queue

Items requiring:

- work confirmation;
- unsupported content decision;
- alignment inspection.

## Trusted channels

CRUD:

- channel ID;
- label;
- enabled;
- priority;
- last scan.

## Discovery controls

- enable/disable Facebook collector;
- enable/disable background YouTube discovery;
- pause background synchronization;
- max background concurrency.

---

# 36. Security

## 36.1 Internal endpoints

Collector/worker endpoints must not be publicly writable.

Use:

- service authentication;
- signed requests or API token;
- TLS;
- secret rotation capability.

## 36.2 URL validation

Never blindly fetch arbitrary user-supplied URLs.

For the YouTube entry field:

- parse URL;
- accept only supported YouTube hosts/forms;
- extract video ID;
- call known APIs/services.

Avoid server-side generic URL fetching to reduce SSRF risk.

## 36.3 Upload security

Preserve existing upload protections:

- size limits;
- MIME validation;
- safe filenames;
- malware/security controls where applicable;
- storage outside executable web paths.

## 36.4 Public IDs

Use non-guess-sensitive public identifiers where practical.

---

# 37. Privacy / Provenance

Store only Facebook information actually required for provenance.

For V1, preferably:

- Group/source identifier;
- post reference/permalink if needed;
- discovery timestamp;
- YouTube ID.

Avoid copying Facebook member personal data unless required.

The valuable media identity is the YouTube ID, not the Facebook user profile.

---

# 38. Observability

Expose metrics such as:

```text
discoveries_total{source}
new_media_total{provider}
deduplicated_discoveries_total
jobs_queued{type,priority}
jobs_completed{type}
jobs_failed{type,error_code}
sync_duration
sync_success_rate
identification_success_rate
qc_failure_rate
ready_performances_by_work
collector_last_success_timestamp
youtube_api_errors
```

Dashboards/alerts should answer:

- Is discovery running?
- Is queue growing?
- Are workers healthy?
- Are synchronizations failing more than usual?
- Did Facebook collection stop?
- Is YouTube API quota/auth failing?

---

# 39. Logging

Every processing operation should have a correlation ID.

Example:

```text
performance_id
media_source_id
processing_job_id
correlation_id
```

Avoid logging tokens, cookies, credentials, or sensitive session material.

---

# 40. Configuration

Do not hard-code operational policy.

Configurable values should include:

- priority weights;
- background concurrency;
- scan intervals;
- qualification thresholds;
- identification confidence thresholds;
- QC thresholds;
- supported URL hosts;
- trusted channels;
- feature flags.

---

# 41. Feature Flags

Recommended:

```text
youtube_input_enabled
interactive_player_enabled
facebook_discovery_enabled
youtube_background_discovery_enabled
trusted_channel_discovery_enabled
catalogue_gap_discovery_enabled
public_library_enabled
comparison_enabled
```

This permits staged release.

---

# 42. Suggested Delivery Phases

## Phase 0 — Codebase assessment

Claude Code must first document:

- existing architecture;
- reusable components;
- schema impact;
- safest migration path.

Output before large refactor:

```text
IMPLEMENTATION_PLAN.md
```

## Phase 1 — Unified Performance foundation

Implement/adapt:

- Performance abstraction;
- MediaSource;
- YouTube provider;
- deduplication;
- processing status;
- shared synchronization relation.

No Facebook dependency.

## Phase 2 — Paste YouTube URL

Implement:

- homepage/Watch UI;
- YouTube URL parsing;
- metadata validation;
- DB lookup;
- new processing job;
- status page.

## Phase 3 — Interactive player

Implement:

- YouTube IFrame integration;
- score display;
- measure timing loading;
- video -> score following;
- score -> video seeking;
- responsive layout.

At this point the main user-facing feature is useful.

## Phase 4 — Automated discovery API

Implement:

```text
POST /api/internal/discoveries
```

Plus provenance and priority updates.

## Phase 5 — Trusted YouTube enrichment

Implement:

- channel configuration;
- upload enumeration;
- candidate ingestion;
- background low-priority processing.

## Phase 6 — Facebook collector prototype

Separate collector:

- observe published Group feed;
- extract YouTube IDs;
- send discovery event;
- state/health monitoring.

Deploy only after applicable platform/compliance review.

## Phase 7 — Catalogue-aware enrichment

Implement:

- per-work coverage;
- gap selection;
- YouTube search candidates;
- background scheduler.

## Phase 8 — Public library

Implement:

- work pages;
- performance lists;
- browse/search.

## Phase 9 — Comparison

Implement:

- same-measure switching;
- compare selected performances.

---

# 43. MVP Definition

The smallest release that validates the new product is:

1. User opens Chopin.Weefeen.
2. User can choose current upload flow OR paste YouTube link.
3. User pastes a supported Chopin YouTube URL.
4. System deduplicates by video ID.
5. If already synchronized, opens immediately.
6. If new:
   - validates;
   - identifies work;
   - runs existing synchronization;
   - persists measure timing;
   - QC;
   - marks READY.
7. Result page displays YouTube and synchronized score.
8. Clicking a score measure seeks the video.
9. Playing the video updates the active score measure.
10. Existing upload/render workflow continues working.

Facebook automation and background discovery should be built after this core path is stable, because they are acquisition mechanisms rather than the fundamental product.

---

# 44. Acceptance Criteria — YouTube Entry

## AC-YT-01

Given a valid supported YouTube URL, submitting it creates or returns one canonical YouTube MediaSource.

## AC-YT-02

Submitting different URL forms for the same video does not duplicate the media.

## AC-YT-03

Submitting an already READY video returns the existing public performance.

## AC-YT-04

Two simultaneous submissions of the same new YouTube video result in one canonical media record and no duplicate expensive synchronization.

## AC-YT-05

Invalid/non-YouTube URL returns a clear validation error.

## AC-YT-06

Unavailable video is recorded with an appropriate status and does not repeatedly re-enter synchronization.

---

# 45. Acceptance Criteria — Synchronization

## AC-SYNC-01

A successful sync stores its score ID and algorithm version.

## AC-SYNC-02

Measure timing is persisted independently from rendered output.

## AC-SYNC-03

Timings are monotonic or fail QC.

## AC-SYNC-04

A READY performance can be loaded without rerunning synchronization.

## AC-SYNC-05

A new discovery of a READY video does not trigger another sync.

---

# 46. Acceptance Criteria — Interactive Player

## AC-PLAYER-01

YouTube video embeds successfully for a supported video.

## AC-PLAYER-02

During playback, active score measure changes according to stored timing.

## AC-PLAYER-03

Clicking a score measure seeks to the expected YouTube timestamp.

## AC-PLAYER-04

Pause/play does not destroy synchronization state.

## AC-PLAYER-05

Page works on desktop and mobile.

## AC-PLAYER-06

The page uses the same synchronization representation for uploaded media and YouTube media.

---

# 47. Acceptance Criteria — Priority Queue

## AC-Q-01

An active user request is processed before queued low-priority background enrichment when capacity becomes available.

## AC-Q-02

A Facebook discovery can increase priority of an already queued YouTube candidate.

## AC-Q-03

Discovering a candidate multiple times does not create duplicate sync jobs.

## AC-Q-04

Background enrichment can be paused independently.

---

# 48. Acceptance Criteria — Facebook Discovery

## AC-FB-01

Collector observes published Group content, not only pending moderation items.

## AC-FB-02

A published post containing a supported YouTube URL results in an internal discovery event.

## AC-FB-03

Pre-approved/direct-published member posts are discoverable through the same published-feed process.

## AC-FB-04

Same YouTube video shared in multiple Facebook posts is synchronized once.

## AC-FB-05

Collector restart does not lose recent posts because an overlap/reconciliation scan exists.

## AC-FB-06

Collector failure does not affect existing Chopin.Weefeen playback/upload functionality.

---

# 49. Acceptance Criteria — Background YouTube Discovery

## AC-BG-01

Trusted channels can be configured without source-code changes.

## AC-BG-02

New uploads from a configured channel can create candidate discoveries.

## AC-BG-03

Already-known video IDs are deduplicated.

## AC-BG-04

Background candidates have lower priority than active user/Facebook requests.

## AC-BG-05

Catalogue coverage can be queried per work.

---

# 50. Testing Strategy

## Unit tests

- YouTube URL parser;
- YouTube ID normalization;
- dedup/upsert logic;
- priority calculation;
- work identification mapping;
- synchronization lookup by timestamp;
- measure -> timestamp resolution;
- state transitions.

## Integration tests

- YouTube metadata adapter with mocked API;
- submit URL -> queue;
- queue -> synchronization adapter;
- synchronization -> READY;
- READY -> player payload;
- discovery API idempotency.

## End-to-end tests

### E2E 1 — new YouTube

Paste new URL -> processing -> READY -> playback -> score follows.

### E2E 2 — prepared YouTube

Paste existing READY URL -> immediate result.

### E2E 3 — duplicate URLs

Submit watch URL and youtu.be variant -> one media.

### E2E 4 — score seek

Click later measure -> player seeks accordingly.

### E2E 5 — upload regression

Existing upload -> render still works.

### E2E 6 — discovery convergence

Background discovers video -> later Facebook/user discovers same ID -> no new sync.

---

# 51. Failure-State UX

Examples:

## Not a supported URL

> Please paste a YouTube video link.

## Unavailable

> This YouTube video is currently unavailable.

## Not recognized

> We could not confidently identify the Chopin work in this video.

## Unsupported structure

> This video appears to contain multiple pieces or an unsupported excerpt.

## Alignment failure

> We identified the work, but could not synchronize this performance reliably yet.

Avoid exposing raw backend errors.

---

# 52. Availability Rechecking

YouTube content can later disappear or change availability.

For READY YouTube performances:

- do not validate through API on every view;
- detect player errors;
- optionally refresh metadata periodically;
- mark `UNAVAILABLE` after verified failure;
- retain synchronization data even if source media becomes unavailable.

This preserves the corpus/history.

---

# 53. Cache Strategy

Cache relatively static result payloads:

- work metadata;
- score metadata;
- synchronization timing;
- performance metadata.

Do not cache dynamic processing status too aggressively.

If using CDN/public caching, avoid exposing private upload sources.

---

# 54. Performance Considerations

Synchronization timing files may contain many events.

For V1 measure-only player:

- return compact measure timing payload.

If note-level events are large:

- separate endpoint;
- compressed payload;
- lazy load only where needed.

The player should not need the full raw synchronization debug output.

---

# 55. Database Indexing

At minimum index:

```text
media_source(provider, external_media_id) UNIQUE
performance(work_id, status)
synchronization(performance_id, status)
synchronization_measure(synchronization_id, measure_index)
discovery(media_source_id, source_type)
processing_job(status, priority, available_at)
```

Review actual query plans after implementation.

---

# 56. API Rate / External Dependency Strategy

Wrap YouTube APIs behind an adapter.

Example:

```text
YouTubeMetadataProvider
YouTubeDiscoveryProvider
```

Do not scatter direct HTTP calls across controllers/workers.

Implement:

- quota/error handling;
- timeout;
- retry;
- caching where appropriate;
- centralized credentials.

---

# 57. Frontend Component Model

Suggested logical components, adapted to current frontend technology:

```text
YouTubeUrlInput
PerformanceStatus
PerformanceHeader
MediaPlayer
YouTubeMediaPlayer
UploadedMediaPlayer
ScoreViewer
ActiveMeasureOverlay
PerformancePlayerLayout
```

The parent player should interact with a provider-neutral interface:

```text
play()
pause()
seekTo(ms)
getCurrentTime()
onTimeUpdate()
onStateChange()
```

This prevents score logic from depending directly on YouTube.

---

# 58. Media Player Abstraction

Define something equivalent to:

```ts
interface MediaPlayerAdapter {
    play(): void;
    pause(): void;
    seekTo(milliseconds: number): void;
    getCurrentTime(): number;
    getDuration(): number | null;
    onTimeUpdate(callback): Unsubscribe;
    onStateChange(callback): Unsubscribe;
}
```

Implement:

```text
YouTubePlayerAdapter
UploadedVideoPlayerAdapter
```

Then synchronization UI works with either source.

---

# 59. Synchronization Mapping Service

Frontend or backend shared concept:

```text
timeToMeasure(timeMs)
measureToTime(measureIndex)
```

For performance efficiency:

- synchronization measures are sorted;
- use binary search or pointer progression for playback;
- do not linearly scan a long array on every animation frame.

---

# 60. Suggested Processing State Machine

```text
DISCOVERED
    |
VALIDATING
    |
    +---- invalid -----------------> REJECTED
    |
IDENTIFYING
    |
    +---- ambiguous ---------------> NEEDS_WORK_CONFIRMATION
    |
READY_FOR_SYNC
    |
SYNCHRONIZING
    |
    +---- technical failure -------> FAILED
    |
QC
    |
    +---- insufficient quality ----> REVIEW / FAILED
    |
READY
```

State transitions should be explicit, validated, and logged.

Avoid arbitrary status strings being changed from many places.

---

# 61. Manual Review without Becoming Manual Operations

The product objective is automation.

Manual review is only an exception path.

The system should not require Julien/admin action for every performance.

A review queue exists only for:

- uncertain piece;
- failed/low-confidence sync;
- unusual structure;
- content requiring exceptional handling.

High-confidence normal Chopin performances should flow end-to-end automatically.

---

# 62. Operational Scheduling

Conceptual worker behavior:

```text
while capacity_available:
    job = highest_priority_runnable_job()
    process(job)
```

Background candidate discovery can run on a schedule, but synchronization demand should be queue-driven.

Do not create a design that processes exactly N videos/day as a hard product rule.

Instead use configurable:

- compute budget;
- concurrency;
- rate limits;
- queue priority.

---

# 63. Catalogue Coverage Strategy

Create a report/query:

```text
Chopin Work
Total READY performances
Trusted READY performances
Failed candidates
Last discovery date
```

Use this to choose enrichment candidates.

The system should gradually move from:

```text
popular Chopin videos
```

toward:

```text
balanced coverage of Chopin's repertoire
```

while still prioritizing actual user/Group demand.

---

# 64. Success Metrics

Product metrics:

- YouTube URLs submitted;
- percentage already prepared;
- median time from new URL to READY;
- percentage successfully identified;
- percentage successfully synchronized;
- performance page starts;
- score interactions;
- measure-click seeks;
- returning users;
- works with at least one READY performance;
- median/mean READY performances per work.

Corpus metrics:

- total unique YouTube performances;
- total synchronized performances;
- coverage by work;
- trusted-source coverage;
- human-verified alignments;
- algorithm-version quality improvement.

Do not optimize only for number of ingested links.

---

# 65. Key Architectural Decisions — Must Preserve

Claude Code should treat the following as core design decisions unless codebase discovery reveals a strong technical reason to adjust implementation details.

### Decision 1
One canonical `Performance` model.

### Decision 2
Media source is separate from synchronization.

### Decision 3
YouTube is embedded, not re-rendered as the default behavior.

### Decision 4
Rendered MP4 remains an output of the existing upload workflow.

### Decision 5
Deduplicate YouTube by `videoId`.

### Decision 6
Discovery provenance is many-to-one with media.

### Decision 7
All expensive work is asynchronous.

### Decision 8
User/Facebook demand outranks background enrichment.

### Decision 9
Facebook integration is isolated from the core application.

### Decision 10
Synchronization data is reusable/versioned and must survive media/discovery changes.

### Decision 11
The system should operate automatically; manual review is an exception.

### Decision 12
Do not discard fine-grained alignment data even if the first UI uses only measures.

---

# 66. Implementation Instruction to Claude Code

Before writing significant code:

1. Inspect the existing Chopin.Weefeen application.
2. Map this specification onto existing entities/services/routes.
3. Identify what can be reused.
4. Identify schema migrations required.
5. Identify backward-compatibility risks.
6. Produce an implementation plan divided into small deployable changes.
7. Add tests before/refactor alongside major model changes.
8. Do not rewrite the existing upload pipeline unnecessarily.
9. Keep new acquisition providers loosely coupled.
10. Implement the YouTube user flow before Facebook automation.

Recommended implementation order:

```text
A. Codebase assessment
B. Performance/media abstraction
C. YouTube URL ingestion + dedupe
D. Async processing integration
E. Interactive YouTube + score player
F. Regression test upload/render
G. Discovery API
H. Trusted YouTube discovery
I. Facebook collector
J. Catalogue enrichment
K. Library / comparison
```

---

# 67. Definition of Done for the First Major Release

The release is complete when:

- the current upload/render path still works;
- homepage clearly exposes upload and YouTube paths;
- user can paste YouTube link;
- system deduplicates by video ID;
- known synchronized links open immediately;
- new compatible links enter the existing identification/synchronization engine automatically;
- sync result is persisted independently of rendering;
- a public performance page embeds YouTube and displays the synchronized score;
- video playback updates the active score position;
- score measure click seeks the video;
- mobile layout is usable;
- errors are understandable;
- status is observable;
- architecture supports discovery provenance and priority queues;
- internal discovery API exists for later automated collectors;
- tests cover the main flow and upload regression.

---

# 68. External Platform Facts to Account For

These implementation constraints were verified for this specification.

## YouTube

The official YouTube IFrame Player API supports embedded playback control, including seeking to a specified time. This is suitable for measure-click -> video seek and synchronized score following.

The YouTube Data API supports video/channel/search discovery. Search can be ordered by view count, but generic search should not be treated as the authoritative way to enumerate the newest uploads of a known channel; channel upload playlist enumeration should be preferred for trusted channels.

## Facebook Groups

Meta deprecated the historical Groups API capabilities in Graph API v19 and removed the affected Groups API permissions/features and group-app installation ability on April 22, 2024. The architecture therefore must not depend on a supported Group-post webhook.

The core Chopin.Weefeen product and discovery ingestion API must remain completely functional even if the Facebook collector is disabled or needs replacement.

---

# 69. Final Product Mental Model

Do not think:

```text
Upload -> Generate video
```

Think:

```text
                 CHOPIN PERFORMANCE
                         |
          +--------------+--------------+
          |                             |
      Uploaded                        YouTube
          |                             |
          +--------------+--------------+
                         |
                   Identify work
                         |
                    Synchronize
                         |
              Store canonical timing
                         |
            +------------+------------+
            |                         |
      Interactive page          Rendered video
      video <-> score           where applicable
            |
       Performance library
            |
       Passage comparison
            |
       Learning/evaluation corpus
```

The synchronized Performance is the product foundation.

Facebook and YouTube discovery are acquisition channels that continuously feed it.

---

# 70. One-Sentence Product Requirement

> Every qualifying Chopin performance, whether uploaded by a user, pasted from YouTube, discovered in the Frédéric Chopin Facebook Group, or found through background YouTube discovery, should converge into one reusable, versioned Performance object whose media can be played against a precisely synchronized Chopin score.

