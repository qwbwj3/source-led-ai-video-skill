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

## Required visual review

Open the contact sheet, both covers, and full-resolution frames at:

- opening hook;
- every scene boundary;
- information-card entry, midpoint, and exit;
- every two-line caption type;
- final frame.

Reject:

- pixelated enlargement or jagged source footage;
- caption clipping, bad word breaks, UI overlap, or a caption over a dense information card;
- unreadable toolchain cards;
- X/GitHub marks, URLs, handles, QR codes, or external-platform UI in the public frame;
- weak/misspelled cover hooks or two covers derived from the same generation;
- speech that sounds clipped, rushed, emotionally wrong, or incorrectly pronounces a key tool name.

## Final delivery boundary

`review-runs/<build-key>/` is for private review and is not upload-ready. Do not call the bundle complete until `confirm-visual` records a specific pass, the verified approval binds `rights_clearance: confirmed`, and `finalize` atomically publishes `deliverables/<build-key>/`. A pending rights state returns `RIGHTS_PENDING`; `verify` also treats it as a bundle failure. Record a visual fail when a check is blocked or incomplete; failed or stale QA cannot be finalized.

## Reproducibility claim

Require exact hashes for normalized configuration, timeline, captions, PCM masters, fonts, and local runtime. Cloud TTS waveform and final MP4 bytes may differ after a provider or encoder change. In that case, require the same structural media properties and every quality gate above; never claim byte-identical cloud output.
