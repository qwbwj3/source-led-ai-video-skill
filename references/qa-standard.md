# RE and QA release standard

## Automatic review-run blockers

- Script review approval comes from the bundled `yuwen-publish-precheck` contract, matches the exact narration, cover hook, Douyin scope, commercial flag, and scene order, and includes hash-bound lexical plus semantic evidence. Its bound material-rights state may be `pending` for TTS and a private review run.
- Clean base is video-only, 1080x1920, CFR 30 fps, and reaches the configured final source boundary.
- TTS is decodable, non-empty, and normalized to 48kHz mono 24-bit PCM.
- Forced alignment reconstructs 100% of the narration. Captions are monotonic, non-overlapping, and within the audio duration.
- Every scene uses its full configured source range and stays within the configured retime-factor limits.
- Final file has exactly one H.264 yuv420p 1080x1920 CFR30 stream and one AAC 48kHz stereo stream.
- Final decoded loudness is `-14 ±0.5 LUFS`; true peak is at most `-1.4 dBFS`.
- Full decode succeeds. No undeclared full-black interval of at least 0.30 seconds and no undeclared silence of at least 0.80 seconds.
- Both covers exist, use independent imagegen sources, have the exact dimensions, and include the hook in their filenames.
- V2 Source Package, edit plan, completed cover prompt record, and post-alignment
  timeline lock remain hash-bound to the run.

## Required visual review

Automatic QA writes real full-resolution JPEGs under
`qa/evidence-frames/` and binds them in `qa/review-points.json` by video hash,
frame number, timestamp, path, and file hash. Open the contact sheet, both covers,
and every listed evidence frame. Required points include:

- opening hook;
- every scene boundary;
- information-card entry, midpoint, and exit;
- the first representative two-line caption;
- final frame.

Listen to the complete rendered video. A passing `confirm-visual` record uses
schema 3 and must declare an actual listened duration covering the video plus an
`--evidence-frame SECONDS:LABEL` entry matching every required review point.
Missing, changed, traversing, or unbound evidence files make the review stale.
Schema-2 records remain readable for legacy diagnosis and cannot finalize a V2
delivery.

Reject:

- pixelated enlargement or jagged source footage;
- caption clipping, bad word breaks, UI overlap, or a caption over a dense information card;
- unreadable toolchain cards;
- X/GitHub marks, URLs, handles, QR codes, or external-platform UI in the public frame;
- weak/misspelled cover hooks or two covers derived from the same generation;
- speech that sounds clipped, rushed, emotionally wrong, or incorrectly pronounces a key tool name.

For a failed `confirm-visual`, repeat `--failed-check` with one or more exact
machine-readable values: `no_pixelated_upscale`, `no_caption_ui_overlap`,
`no_caption_clipping`, `info_card_readable`,
`no_visible_url_or_external_ui`, `covers_readable_and_hooked`, or
`voice_and_bgm_acceptable`.

## Final delivery boundary

`review-runs/<build-key>/` is immutable private-review evidence and remains in
place after finalization. Do not call the bundle complete until schema-3 visual
review passes, approval binds `rights_clearance: confirmed`, every required V2
rights record is active, and `finalize` atomically publishes
`deliverables/<release-key>/`. The release key binds the build and current
rights ledger. Finalization builds in an unpredictable private staging
directory, validates it, atomically publishes read-only files, verifies the
published contracts again, quarantines a failed publication, and updates
`latest.json` only after success. Failed or stale QA cannot finalize.

If a rights-state change produces a different build run, complete a fresh
schema-3 visual/audio review against that run's exact evidence before
finalization, even when the video-core cache was reused.

## Reproducibility claim

Require exact hashes for normalized configuration, timeline, captions, PCM masters, fonts, and local runtime. Cloud TTS waveform and final MP4 bytes may differ after a provider or encoder change. In that case, require the same structural media properties and every quality gate above; never claim byte-identical cloud output.
