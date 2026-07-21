---
name: source-led-ai-video
description: Produce a source-led Chinese AI short video for Douyin from an X post, Builder demo, GitHub/open-source project, or prepared source folder. Use for source evidence packaging, a sub-90-second creator script, ChatCut assembly, mandatory yuwen publish precheck before Volcengine TTS, forced-aligned captions, BGM, two independent ImageGen covers with Chinese hooks, rights evidence, QA, analytics, a ready-to-upload local bundle, and an optional post-publication Feishu resource entry after an explicit include decision.
---

# Source-led AI video

Turn one selected source into a private review project and, after rights and QA pass, an upload-ready local bundle. Keep source footage as the main proof. Add a short Chinese explanation, the practical toolchain, and a creator judgment. Stop before platform upload unless the user separately asks.

This release targets Apple Silicon macOS. It locks the local FFmpeg, font, aligner revision, schemas, and release gates. Cloud TTS, ImageGen, ChatCut, and platform moderation remain external services; never promise byte-identical cloud output or guaranteed approval.

## Read the relevant contract

Always read:

- `references/production-standard.md`
- `references/project-schema.md`
- `references/source-package.md`
- `references/edit-plan.md`
- `references/cover-standard.md`
- `references/qa-standard.md`
- `references/rights-ledger.md`
- `references/operations.md`

Read `references/environment-contract.md` on a new Mac. Read `references/data-feedback.md` when recording cost, platform data, or creator feedback. Read `references/share-doc.md` only after publication when the user is deciding whether that exact video should enter the fan-facing Feishu resource library.
Read `references/migration-v1-v2.md` before upgrading an existing project; do
not flip a V1 version field in place.

Before approving copy, read the bundled publish-precheck files listed in `references/publish-precheck/source-skill-contract.md`. Load medical or finance rules only when the content enters that domain.

## Fixed editorial decisions

- Audience: ordinary people interested in useful, visually striking AI work.
- Topic: prefer a community Builder result, workflow, or open-source project with visible payoff. Official launch news alone is weak.
- Format: source footage first, commentary second; final duration at most 90 seconds.
- Hook: show the result and front-load the short “how it was made” toolchain.
- Verification: check ordinary facts against the post, replies, linked material, and reliable sources. Local deployment is optional unless the user asks.
- Public frame: remove X/GitHub UI, URLs, repository paths, handles, QR codes, and unrelated platform marks. Keep source identity and rights evidence in private records; put credit in the body or pinned comment when required.
- Editor: use ChatCut for the editable source-led assembly. Use one HTML/HyperFrames information card when a project or workflow needs a compact explanation.
- Voice: use the configured Volcengine knowledgeable female voice in one full-narration request at natural speed. Do not stretch speech.
- Fixed CTA: every narration must include this exact spoken sentence once, with matching captions: “文稿我已经整理好，评论区自取”。Place it in the final save/share beat, and keep it inside the 480-character preflight limit.
- Music: use supplied or otherwise rights-cleared BGM for a publishable version. An empty BGM path is limited to a smoke test or explicit no-music version.
- Covers: make separate 3:4 and 4:3 ImageGen calls. Each final composition contains the exact reviewed Chinese hook. Cropping one generation into two sizes is blocked.
- Publication: create files locally; do not upload automatically.
- Resource library: after publication, record a separate explicit `include` or `skip` decision. Never let upload success, moderation, or traffic trigger a Feishu write automatically.

## 1. Initialize a V2 project

Resolve `VIDEO_SKILL_ROOT` from this `SKILL.md`, not from the caller's directory.

```bash
VIDEO_SKILL_ROOT="/absolute/path/to/source-led-ai-video"
python3 "$VIDEO_SKILL_ROOT/scripts/init_project.py" \
  --project "/absolute/path/to/project" \
  --name "项目名"
```

New projects use schema V2. `--project-version 1` exists only for legacy regression work. Keep all configured paths relative to `project.json`; the Source Package, edit plan, timeline lock, and cover prompt record use their fixed paths.

## 2. Collect sources, bind scenes, and write the Source Package

When X needs a logged-in session, use the user's Chrome state through the Chrome-control Skill. Do not switch to a headless browser. Leave login, 2FA, and permission decisions to the user.

Collect privately:

- the original post, source media, and relevant replies;
- the author's explanation of how it was made;
- linked project, product, or documentation material;
- source snapshots, media hashes, and rights evidence;
- claims labelled as author statement, corroborated fact, editorial inference, or unverified;
- the shortest defensible toolchain.

Do not deploy a project merely to prove a normal claim. Reopen raw evidence only when a compact claim conflicts, lacks support, or needs freshness checking.

Choose the selected angle plus stable `claim-*` and `asset-*` IDs. Author the
draft and the `project.json` scene bindings together: every scene must already
reference IDs present in the draft before the writer runs. The writer validates
those bindings. Prepare one composite draft matching
`references/source-package.md`, with `manifest.package_sha256` omitted, then
write and validate it:

Start from the generated `source-package-draft.json` skeleton. Replace every
example value and zero hash; do not add `manifest.package_sha256`.

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/source_package.py" write \
  --config "/absolute/path/to/project/project.json" \
  --draft "/absolute/path/to/project/source-package-draft.json"

python3 "$VIDEO_SKILL_ROOT/scripts/source_package.py" validate \
  --config "/absolute/path/to/project/project.json"
```

Later agents read `source-package/brief.md`, the structured claims, toolchain, and asset index first. Raw snapshots stay available for exceptions.

## 3. Finish the script and semantic edit plan

Refine `project.json` from the selected angle. Every scene must cite at least one supported claim and one declared source asset. A public scene cannot cite an `unverified` claim or a rejected asset. If scene bindings change, rerun Source Package validation before writing the edit plan.

A useful short-video order is:

1. Show the concrete result and name the toolchain.
2. Explain the minimum “how it was made” chain.
3. Let the strongest source clips prove the claim.
4. Add one useful limitation, method, or creator judgment.
5. End on a concrete takeaway worth saving.

Avoid generic AI hype, line-by-line translation, boundary lectures, and separate disclaimer scenes. Create narration and captions together; captions must reconstruct their scene narration after punctuation and whitespace are ignored.

Set `source_timeline.scene_boundaries_seconds` to the intended clean-base scene boundaries. Create any information-card HTML first, including its SHA-256, then write the semantic edit plan:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/edit_plan.py" write-plan \
  --config "/absolute/path/to/project/project.json" \
  --draft "/absolute/path/to/project/edit-plan-draft.json"

python3 "$VIDEO_SKILL_ROOT/scripts/edit_plan.py" validate-plan \
  --config "/absolute/path/to/project/project.json"
```

The semantic plan defines source frame ranges, shot order, caption regions, fit/fill policy, and information cards. It guides ChatCut. It carries no final TTS timing.

## 4. Build the clean base in ChatCut

Load the ChatCut plugin basics, asset-import, verification, and export Skills before editing. Import the original media and retain an editable ChatCut timeline.

Follow the semantic edit plan:

- keep source clips as the main visual layer;
- mute source audio and exclude final captions, voice, and BGM;
- remove visible external-platform UI without erasing ownership marks to misrepresent authorship;
- avoid enlarging low-resolution media until edges become jagged;
- use the declared caption region and keep dense information-card scenes uncluttered;
- export a video-only H.264 MP4 at 1080x1920, CFR 30 fps;
- preserve the exact scene order and clean-base boundaries in `project.json`.

Save the export at `paths.base_video`. The local pipeline will retime this clean base after forced alignment. `edit/timeline.lock.json` is generated at that later point to record the final integer-frame retime; ChatCut does not consume that post-alignment lock.

## 5. Bootstrap each Mac once

Install the six-field Volcengine configuration without printing its values, then prepare the locked runtime:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/install_volc_config.py" \
  --from-env "/absolute/path/to/volc.env"
python3 "$VIDEO_SKILL_ROOT/scripts/bootstrap_runtime.py"
```

The source env and installed file must use mode `0600`. The default destination is `~/.config/source-led-ai-video/volc.env`. Never print, copy into a project, or commit real credentials.

## 6. Pass copy review before TTS

Set the exact final narration, scene order, commercial scope, industry scope, cover hook, and headline lines before review. This gate covers both narration and cover text. It must pass before any TTS request, including test audio, retries, or a direct adapter call.

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/review_gate.py" prepare \
  --config "/absolute/path/to/project/project.json"

python3 "$VIDEO_SKILL_ROOT/scripts/review_gate.py" scan \
  --config "/absolute/path/to/project/project.json"
```

Perform the semantic review with the bundled `yuwen-publish-precheck` contract. Write `review/publish-precheck.md` with its required sections, exact blocker locations, minimum replacements, trust-boundary statements, and a Douyin conclusion. Lexical zero hits do not count as approval. If copy changes, rerun the full review. A repaired draft needs the standalone line `复检：通过`.

Create the hash-bound approval only after a truthful pass:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/review_gate.py" approve \
  --config "/absolute/path/to/project/project.json" \
  --report "/absolute/path/to/project/review/publish-precheck.md" \
  --result pass \
  --reviewer "yuwen-publish-precheck"
```

Use `--result revised-pass` after a repaired draft. The report may mark material rights pending for a private review run. Final delivery requires confirmed rights.

## 7. Render the video core immediately after copy approval

Once copy review passes, run the video core without waiting for either cover. It produces the narrated, captioned video and automatic QA for visual review; it does not authorize release.

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" run \
  --config "/absolute/path/to/project/project.json"
```

If the core build fails, stop and fix the named blocker. Do not ask the user to approve routine TTS, assembly, or render steps after copy review has passed.

## 8. Generate and record both covers after the core render

Use the built-in ImageGen capability twice: one complete 3:4 composition and one complete 4:3 composition. Copy the exact reviewed hook and line breaks into both prompts. Follow `references/cover-standard.md`; this account defaults to a faceless cover with large conclusion-led Chinese type and one highlighted keyword.

Synchronize provenance after the final hook, filenames, visual settings, and
per-ratio text modes are set. This safely replaces the initialized placeholder
state. Later changes reset only affected ratios and archive their attempts:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/edit_plan.py" sync-cover \
  --config "/absolute/path/to/project/project.json"
```

Save each result to its configured `generated_source`, then record the actual prompt, available provider metadata, file hash, and generation version:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/edit_plan.py" record-cover \
  --config "/absolute/path/to/project/project.json" \
  --ratio 3x4 --status completed \
  --prompt "完整的3:4生成提示词" \
  --model "实际模型名" --result-id "本次调用的唯一ID"

python3 "$VIDEO_SKILL_ROOT/scripts/edit_plan.py" record-cover \
  --config "/absolute/path/to/project/project.json" \
  --ratio 4x3 --status completed \
  --prompt "完整的4:3生成提示词" \
  --model "实际模型名" --result-id "另一次调用的唯一ID"

python3 "$VIDEO_SKILL_ROOT/scripts/edit_plan.py" validate-cover \
  --config "/absolute/path/to/project/project.json"
```

Omit `--model` only when the built-in tool does not disclose its provider model;
the record then uses `builtin-imagegen`. Omit `--result-id` when the tool returns
none; the script creates a visibly local `local-attempt:*` ID and leaves the
provider result ID null. Never invent provider metadata.

Inspect both at full size and 25% feed size. The characters, line breaks, margins, and highlighted keyword must be correct. If a generation fails, record `--status failed --retry-reason "..."`, regenerate only that ratio, and record the new attempt. Attempts append to the current semantic revision. After changing a ratio's source path or `text_mode` for deterministic fallback, run `sync-cover` before retrying; the changed ratio is reset and archived while the other ratio remains valid. Current completed call identities and source hashes must be distinct.

Normalize the passing sources to final dimensions:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/render_cover.py" \
  --config "/absolute/path/to/project/project.json" --ratio 3x4
python3 "$VIDEO_SKILL_ROOT/scripts/render_cover.py" \
  --config "/absolute/path/to/project/project.json" --ratio 4x3
```

## 9. Attach covers, inspect status, and resume safely

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" status \
  --config "/absolute/path/to/project/project.json"

python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" run \
  --config "/absolute/path/to/project/project.json"
```

The first run validates V2 source/edit contracts before loading TTS credentials. It synthesizes one narration, force-aligns it, creates the final integer-frame timeline lock, retimes the clean base, burns captions by scene region, mixes BGM, renders the video, and produces automatic QA plus full-resolution evidence frames. After both covers are recorded and normalized, run it again to attach them; the valid video core is reused.

After failure, inspect `status` and use:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" resume \
  --config "/absolute/path/to/project/project.json"
```

Recovery has two dependable boundaries: a valid completed build can be reused in full, and a video core that already passed automatic QA can be reused when only cover/release state changes. Earlier-stage failures are rerun safely. The event log records attempts and elapsed time; it does not imply arbitrary per-stage checkpoint continuation.

`--reuse-tts` is limited to regression replay or an explicitly approved in-project recording and requires matching provenance. It never bypasses copy review.

## 10. Complete evidence-based visual and audio QA

Open both final covers, the contact sheet, and every frame listed in `qa/review-points.json`. The generated points include opening, scene boundaries, ending, the first two-line caption, and each information-card entry/midpoint/exit. Listen to the full rendered video with BGM.

Record every review-point timestamp and the actual listened duration. Use values from `qa/review-points.json` and `qa/auto-qa.json`; do not invent them.

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" confirm-visual \
  --config "/absolute/path/to/project/project.json" \
  --run-dir "/absolute/path/to/project/review-runs/<build-key>" \
  --reviewer "Agent name" \
  --notes "具体说明检查了哪些镜头、字幕、封面、口播与音乐" \
  --result pass \
  --listened-seconds 87.20 \
  --evidence-frame "0.0:opening" \
  --evidence-frame "5.0:scene-02-boundary" \
  --evidence-frame "87.166667:ending"
```

The numbers above only show argument shape. Read the current JSON and repeat
`--evidence-frame` for every point it contains, including any two-line-caption
and information-card points. A pass must cover every required point and the full
duration. A failure names each blocker with `--failed-check`. Reject pixelated enlargement, subtitle/UI overlap, clipped captions, unreadable cards, visible external-platform marks, cover errors, bad pronunciation, clipped pauses, rushed delivery, or poor BGM balance.

Allowed failure names are `no_pixelated_upscale`,
`no_caption_ui_overlap`, `no_caption_clipping`, `info_card_readable`,
`no_visible_url_or_external_ui`, `covers_readable_and_hooked`, and
`voice_and_bgm_acceptable`.

## 10. Confirm rights and finalize

V2 finalization requires an active schema-2 rights record for `base_video`, `bgm` when present, and every used Source Package asset. Put evidence files under `private/rights-evidence/`, then record each asset:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/rights_ledger.py" confirm \
  --config "/absolute/path/to/project/project.json" \
  --asset-id "asset-01" --source-id "source-01" \
  --author "原作者" --source "原始来源说明" \
  --rights-basis "授权或可用依据" \
  --evidence-path "private/rights-evidence/asset-01.txt" \
  --confirmed-by "审核人" \
  --attribution-placement body_or_pinned_comment \
  --attribution-text "来源：作者与项目名"
```

Use asset/source pairs `base_video` / `source-base-video` and `bgm` /
`source-bgm` for those files. Source assets use the Source Package source ID.
Use `--attribution-placement none` only when no attribution is required. If permission changes, append a revocation:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/rights_ledger.py" revoke \
  --config "/absolute/path/to/project/project.json" \
  --asset-id "asset-01" --reason "授权已撤回" --confirmed-by "审核人"
```

Update used Source Package asset states to `confirmed` in the retained composite
`source-package-draft.json`, keep `manifest.package_sha256` omitted, and rerun
`source_package.py write`; never patch `assets.json` alone. Update the semantic
report to `素材授权：已确认`, regenerate its approval, and rerun/resume. The
rights-only change may reuse the passed video core, but the resulting run still
requires a fresh schema-3 visual/audio confirmation against its exact evidence.
The release bundle and provenance are rebuilt.

Finalize and verify:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" finalize \
  --config "/absolute/path/to/project/project.json" \
  --run-dir "/absolute/path/to/project/review-runs/<build-key>"

python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" verify \
  --bundle "/absolute/path/to/project/deliverables/<release-key>"
```

Read `release_key` and `bundle` from `finalize` output or
`deliverables/latest.json`; do not infer them from the build key. Only a
verified `deliverables/<release-key>/` directory is upload-ready. Report the video, both covers, subtitles, QA evidence, rights evidence, and any body/pinned-comment attribution the uploader must add.

## 11. Record cost and performance without changing the build

Use `scripts/analytics.py` for model usage, 2h/24h/72h/7d platform snapshots, and short creator feedback. Missing metrics stay `null`. Analytics files never enter the video Build Key. Do not put URLs, handles, phone numbers, email addresses, account IDs, or secrets in analytics text. See `references/data-feedback.md`.

For low-cost operation, use one focused project per task and let a Terra High single Agent read the compact Source Package and structured intermediates. Use a stronger model only for a disputed source interpretation, script judgment, or failed QA diagnosis. Measure real tokens and elapsed time before setting a production target; do not claim a fixed token count or 10–20 minute finish without recorded runs.

## 12. Optionally add the published video to the Feishu resource library

This stage is independent from production and upload. Run it only after the user
has explicitly chosen `include` or `skip` for the exact published video. Read
`references/share-doc.md` before acting.

For `include`, prepare a concise project-relative JSON from
`assets/share-doc-entry.example.json`. It must contain a concrete one-line
takeaway, a 3–5 step toolchain, useful project links, and only source-supported
prompt material. Prompt provenance is mandatory; omit the entire prompt section
when no defensible prompt is available.

Record the decision with `scripts/share_doc.py`. The script writes only local,
append-only state and deterministic Feishu XML. It does not call Feishu. Use the
`lark-doc` Skill separately to search the visible material ID, insert the entry
under its unique category heading, and verify exactly one matching entry after
the write. Only then record `mark-synced`.

A skipped item causes no Feishu operation. Share-document state never changes a
Build Key, release, media cache, analytics event, or delivery bundle. Keep a new
master document private until the user separately approves public-link
permissions and anonymous access has been tested.

## Stop conditions

Stop before TTS when copy approval is missing or stale, source evidence cannot support a public claim, the clean base violates the media contract, captions do not reconstruct narration, or planned retime falls outside limits. Missing covers do not block the video core; they block final visual approval and release.

Pending material rights permit a private review run. Stop before finalization when required rights, full listening, evidence-frame coverage, automatic QA, or visual QA remain incomplete. Fix the source, script, edit, cover, voice, or rights record; do not weaken the gate.

## Release self-test

After copying or extracting this Skill on another Mac:

```bash
env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s "$VIDEO_SKILL_ROOT/scripts/tests" -p 'test_*.py' -v

SOURCE_LED_RUNTIME_DIR="/absolute/path/to/temporary-runtime" \
python3 "$VIDEO_SKILL_ROOT/scripts/bootstrap_runtime.py" --skip-aligner
```

Run normal bootstrap once afterward. The offline suite must pass before live TTS or project rendering.
