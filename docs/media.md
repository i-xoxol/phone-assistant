# Public media and screenshot provenance

[← Project overview](../README.md)

## Actual UI screenshots

The JPEG monitor images were captured in Chrome from this repository's
`app/web/index.html`, `app/web/style.css`, and `app/web/app.js`. The isolated preview
adds a visible documentation-demo banner and supplies fictional JSON and SSE
heartbeats. It does not load a service configuration, credentials, private
database, caller history, or calendar. All write routes return HTTP 405.

The pickup-hours conversation and inbox message are invented documentation
fixtures. The only displayed number is in the reserved US `202-555-0100` through
`202-555-0199` example range. No call was placed and no appointment was made.
These screenshots show interface layout, not a successful telephone test.

Reproduce the preview with the Python standard library alone:

```bash
python -m scripts.preview_docs_ui
```

Open `http://127.0.0.1:8765/live` for the call view or append `?view=inbox` for the
message view. The server binds only to localhost. Stop it with Ctrl+C.
`monitor-desktop.jpg` and `monitor-inbox.jpg` are full-page captures at the normal
browser width; `monitor-mobile.jpg` is a viewport capture at 390 × 844. The mobile
capture is scrolled to the conversation and persistent hangup control. No
redaction or compositing was needed. UI source files are unchanged by this update.

## Owner-provided NotebookLM videos

- **Your Phone Finally Gets an AI Brain** — short-form overview, 1:18, portrait.
- **Phone Assistant: The Anatomy of a Perfect Call** — cinematic overview,
  5:12, landscape.

Both are AI-generated illustrative overviews with conceptual graphics and
owner-run test examples. They are not recordings of the product UI, verified
real student calls, or independent reliability evidence. Narrated scenarios can
simplify implementation details; use the current guides for supported behavior,
permissions, provider dependencies, and limits.

The reviewed original MP4s are prepared for GitHub attachment hosting. Publication
and README video links are pending while this pull request remains a draft. No private Drive URL, signed download URL, review transcript, or sharing
permission is published. The preview JPEGs are unmodified video frames from
approximately two seconds into each video.

| Asset | SHA-256 of reviewed original |
|---|---|
| Short-form MP4 | `0dc2809e47ab8561ced8b57b4dfc92f3b9f573849caed77ba5d87a6b350d8d35` |
| Cinematic MP4 | `f5eccf1ae1ef9e09a28df1b02f93fa06617f50d6844de13b127be0d03aede7b5` |

The privacy review covered full narration via ASR, scene-change frames, and regular
4–5 second visual samples. It found no credentials, private contact details, or
sensitive student/family/medical information. It was not a frame-by-frame review.
Every JPEG published here was separately inspected. Re-review media before
replacing it; a checksum records a reviewed version and is not a privacy guarantee.

`docs/assets/media-review.json` records the exact reviewed JPEG hashes and
provenance. The release audit accepts only matching JPEGs in `docs/assets`;
unlisted, changed, or other binary files still require review and fail the gate.

## Approved owner-run call examples

The owner explicitly approved publishing the [Gregory booking test](examples/gregory-booking-test.md)
and [follow-up verification test](examples/gregory-verification-test.md). In both,
the owner role-played the caller; these are not verified real student calls.
The complete saved automatic transcripts preserve all supplied utterances,
timestamps, recognition errors, and overlapping fragments. They were not
reconstructed from audio. Private call URLs and internal identifiers are omitted.

The verification test initially gives the wrong weekday and corrects it only
after the caller challenges it. Both the error and correction remain visible.
The booking transcript's spoken confirmation is not independent proof of a
successful calendar write. These examples illustrate observed speech in two
tests, not general reliability.

Audio retrieval and review remain pending for both examples. No public audio link
is provided until the actual recording is available and cleared for publication.
