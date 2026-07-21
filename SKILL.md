---
name: source-led-ai-video
description: Produce a source-led Chinese AI short video for Douyin from an X post, Builder demo, open-source project, or prepared source folder. Use when the user wants topic verification, a sub-90-second creator-side script, ChatCut assembly, an HTML or HyperFrames toolchain card, mandatory yuwen publish precheck before Volcengine TTS, forced-aligned captions, BGM mix, two independent imagegen covers in 3:4 and 4:3, render QA, or a ready-to-upload local delivery bundle.
---

# Source-led AI video

Build the upload-ready local bundle. Keep the source footage as visual proof and add a concise Chinese explanation, a practical toolchain, and a creator judgment. Stop before platform upload unless the user separately asks.

This release supports Apple Silicon macOS. It locks the local FFmpeg, font, aligner model revision, project contract, and QA gates. Cloud TTS output and platform moderation can change; never promise byte-identical cloud audio or guaranteed approval.

## Load the production contract

Read these before starting a new project:

- `references/production-standard.md`
- `references/environment-contract.md`
- `references/project-schema.md`
- `references/cover-standard.md`
- `references/qa-standard.md`

For the mandatory narration review, also read:

- `references/publish-precheck/source-skill-contract.md`
- `references/publish-precheck/workflow.md`
- `references/publish-precheck/judgment.md`
- `references/publish-precheck/rules-common.md`
- `references/publish-precheck/rules-commercial.md`
- `references/publish-precheck/platform-douyin.md`
- `references/publish-precheck/my-rules.md`
- `references/publish-precheck/profile.md`
- `references/publish-precheck/expressions.md`

Load the medical or finance rule file only when the actual content enters that domain.

## Fixed decisions

- Audience: ordinary people interested in useful and visually striking AI work.
- Topic priority: a community Builder result, concrete workflow, or fast-growing project. Official launch news alone is too weak.
- Format: source footage first, commentary second; under 90 seconds.
- Script hook: front-load the concrete result and the short toolchain or “how it was made” chain.
- Fact check: verify ordinary claims from the post, replies, linked material, and reliable sources. Local deployment is not required unless the user asks.
- Public frame: no X or GitHub interface, external URL, repository path, handle, QR code, or unrelated platform mark. Keep complete attribution and rights evidence in private project records; use body text or a pinned comment for public credit when appropriate.
- Editor: use ChatCut for source-led assembly. Use HTML/HyperFrames for one readable information card when an open-source project or multi-step workflow needs explanation; load the mandatory HyperFrames entry Skill before using HyperFrames.
- Voice: use the configured Volcengine female, knowledgeable-sounding voice (the example defaults to `zh_female_vv_uranus_bigtts`) in one full narration request at `speed_ratio=1.0`; never stretch speech.
- Music: normal publishable projects include the user's supplied or otherwise rights-cleared BGM. An empty BGM path is only for a technical smoke test or when the user explicitly waives music.
- Covers: two independent built-in imagegen calls, one 3:4 and one 4:3. This account defaults to faceless finished covers with the exact reviewed hook generated directly into each composition. Cropping one generation into two sizes is a blocker.
- Publication: prepare local files; do not automatically publish.

## Stage 1: collect and decide

When X or another logged-in website is needed, use the user's Chrome state through the Chrome-control skill. Do not switch to a headless browser. Leave login, 2FA, and permission decisions to the user.

Collect privately:

- original post and source video;
- relevant replies, especially the author's explanation of how it was made;
- linked project or product material;
- source attribution and rights evidence;
- a short fact-check note separating author statements, corroborated facts, and inference.

If the agent selected the topic, tell the user the chosen subject before editing. If the user supplied the source link, treat that as the selected subject and proceed unless the source is unusable or materially false.

Reject or reframe topics that lack a visible payoff, an ordinary-person angle, a usable takeaway, or enough evidence. Do not over-verify by deploying every project.

## Stage 2: write and assemble the clean base

Write the narration and editorial captions together. Every scene caption sequence must reconstruct its scene narration after punctuation and whitespace are ignored.

Use this order when it fits the evidence:

1. Show the result and name the toolchain.
2. Explain the minimum “how it was made” chain.
3. Let the strongest source clips prove the result.
4. Add one useful limitation, method, or creator judgment.
5. End on a concrete takeaway worth saving.

Avoid generic AI hype, line-by-line translation, boundary lectures, and a separate disclaimer scene.

Before editing, load the installed ChatCut plugin's basics, asset-import, verification, and export Skills. Keep the original media as timeline assets and preserve the editable ChatCut project; do not locally concatenate or flatten the source edit as a substitute for the ChatCut timeline.

In ChatCut:

- keep original/source clips as the main visual layer;
- remove the source audio, old subtitles, and visible external-platform UI from the public cut;
- do not enlarge low-resolution footage until it becomes jagged;
- insert a 6–12 second HTML/HyperFrames information card for an open-source toolchain when useful;
- verify the composed timeline at representative frames, then export a clean video-only H.264 MP4 at 1080x1920, CFR 30 fps through ChatCut's documented export route;
- record ordered source-scene boundaries in seconds.

The clean ChatCut export is the reproducible input boundary for the local pipeline.

## Stage 3: initialize the project

Set the Skill root from the current `SKILL.md`, never from the caller's working directory:

```bash
VIDEO_SKILL_ROOT="/absolute/path/to/source-led-ai-video"
python3 "$VIDEO_SKILL_ROOT/scripts/init_project.py" \
  --project "/absolute/path/to/project" \
  --name "项目名"
```

Fill `project.json` from `assets/project-template.json`. Keep every media path relative to `project.json`. Preserve scene array order. Use `show:false` only for captions that would collide with a dense information card.

## Stage 4: configure and bootstrap once per Mac

Run this before cover rendering or TTS. Copy an existing six-field Volcengine env file into the private default location without printing values:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/install_volc_config.py" \
  --from-env "/absolute/path/to/volc.env"
python3 "$VIDEO_SKILL_ROOT/scripts/bootstrap_runtime.py"
```

The source env file and installed config must use mode `0600`. The default destination is `~/.config/source-led-ai-video/volc.env`. Required names are listed in `assets/volc-tts.env.example`; never print their values, add them to a project, or package them with this Skill.

Bootstrap verifies the bundled Apple Silicon FFmpeg and OFL Chinese font, creates a pinned Python aligner environment, and downloads the locked forced-aligner model on first use.

## Stage 5: mandatory narration review gate

This gate is inherited from the user-provided `yuwen-publish-precheck` Skill and is bundled here as a pinned portable snapshot. It must finish before any TTS request, including a draft voice, test sentence, sample render, retry, or direct `volc_tts.py` call. Do not use another TTS entry point to bypass it.

Prepare the exact current narration and run the lexical scan:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/review_gate.py" prepare \
  --config "/absolute/path/to/project/project.json"

python3 "$VIDEO_SKILL_ROOT/scripts/review_gate.py" scan \
  --config "/absolute/path/to/project/project.json"
```

Set `review_scope.industries` in `project.json` before `prepare` when the actual content enters medical or finance. The scan command derives commercial and industry scope from the same hash-bound config; do not pass free-standing flags that can drift from it.

Then perform the semantic review using the required publish-precheck references. Write `review/publish-precheck.md` with:

- the exact structured sections and scope fields required by `references/publish-precheck/workflow.md`;
- a Douyin-specific conclusion plus `结论：可以发` or a supported revised-pass conclusion;
- an exact lexical-review declaration or a disposition for every scanner candidate;
- `必改`, `建议改`, `仅提示`, and `无法判定`, using `- 无` where genuinely empty;
- the complete publishing checklist and both trust-boundary statements; mark `事实证据` as `已确认`, while `素材授权` may remain `需要（发布前确认）` until final assets exist and must still be cleared before publication;
- every real blocker at its exact location with rule ID and minimum replacement.

Lexical zero hits do not count as semantic approval. If the narration, cover hook, platform scope, commercial flag, or scene order changes, rerun prepare, scan, semantic review, and recheck. A repaired report must contain the standalone line `复检：通过`.

Only after the report is truthful and complete may the Agent create the hash-bound approval:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/review_gate.py" approve \
  --config "/absolute/path/to/project/project.json" \
  --report "/absolute/path/to/project/review/publish-precheck.md" \
  --result pass \
  --reviewer "yuwen-publish-precheck"
```

Use `--result revised-pass` after a repaired draft and passed recheck. Never fabricate a pass marker to unlock TTS. `workflow.py run` verifies all review hashes before it can call Volcengine; one changed character makes the approval stale.

The approval binds `rights_clearance` to the report: `需要（发布前确认）` becomes `pending`, and `已确认` becomes `confirmed`. Both states permit TTS and a private review run. Only `confirmed` permits final delivery. Editing `approval.json` cannot upgrade this state because verification reparses the hash-bound semantic report.

## Stage 6: generate both covers

Choose one concrete 4–20 character hook tied to the video's result or method. Put it in `project.json` and in both final filenames. Set `cover.headline_lines` to the exact one- or two-line display structure; the concatenated lines must reconstruct the hook.

Set `cover.text_mode` to `imagegen` and `cover.allow_people` to `false` for this account. Use the built-in `imagegen` Skill/tool twice. Make two separate prompts following `references/cover-standard.md`:

1. A complete 3:4 portrait cover with the exact hook integrated into the image.
2. A complete 4:3 landscape cover with the exact hook integrated into the image.

Give ImageGen the reviewed hook verbatim, copying the line breaks from `cover.headline_lines`. Request conclusion-led big Chinese type, thick outline and shadow, one highlighted keyword, and no other text. Prohibit people, faces, avatars, silhouettes, and human hands while `cover.allow_people` is false. Also prohibit logos, watermarks, X/GitHub UI, URLs, handles, and QR codes. Set `allow_people` to true only after an explicit user override. Save the independent outputs to the exact `cover.*.generated_source` paths inside the project. If built-in imagegen is unavailable, stop with `IMAGEGEN_UNAVAILABLE`; do not substitute a crop, SVG, HTML screenshot, or paid third-party generator.

Inspect both generated sources at full size and at 25% feed-preview size. Confirm the hook is character-perfect, complete, uncropped, dominant, and free of extra text. Make one targeted regeneration for a failed ratio. If it still fails exact-text or readability QA, make a new independent text-free master for only that ratio, save it to a new `generated_source` path, and set `cover.<ratio>.text_mode` to `deterministic`. The fallback prompt must prohibit all text, letters, and numbers. Keep the passing ratio in `imagegen` mode. Never overlay a second copy of the hook on an ImageGen-composed headline.

Run the renderer in the effective mode for each ratio. `imagegen` only normalizes the independent master to exact dimensions; `deterministic` adds the reviewed hook to that ratio's clean text-free master as a controlled fallback.

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/render_cover.py" \
  --config "/absolute/path/to/project/project.json" --ratio 3x4
python3 "$VIDEO_SKILL_ROOT/scripts/render_cover.py" \
  --config "/absolute/path/to/project/project.json" --ratio 4x3
```

The pipeline checks dimensions, filename hooks, and distinct source hashes before TTS. For review and delivery, show and copy the rendered `cover.*.final` files after opening both at full size and at 25% feed-preview size, confirming the result and complete hook remain obvious.

## Stage 7: synthesize, align, retime, mix, and render

Production run:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" run \
  --config "/absolute/path/to/project/project.json"
```

`--reuse-tts` is allowed only for regression replay or an explicitly approved existing narration recording. It requires `--reuse-tts-provenance` pointing to a matching in-project proof file and never bypasses the narration review gate.

The run creates an immutable `review-runs/<build-key>/` containing the review video, full and platform subtitle files, voice masters, covers, provenance, automatic QA, and contact sheet. This directory is a private review artifact, including when material authorization is pending; it is not upload-ready and must not be presented as a deliverable. A failed stage removes its temporary build and publishes no deliverable.

## Stage 8: visual and audio QA

Open both final covers and the contact sheet. Inspect full-resolution frames at the opening, every scene boundary, every information-card entry/midpoint/exit, representative two-line captions, and the ending. Listen to the full narration with BGM.

Reject pixelated enlargement, subtitle/UI overlap, bad line breaks, captions over a dense card, external-platform marks, weak cover text, bad pronunciation, clipped pauses, rushed speed, or an emotionally wrong voice. Do not record a pass from the contact sheet alone when a problem requires full-motion or audio review.

After a real review, record specific notes:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" confirm-visual \
  --config "/absolute/path/to/project/project.json" \
  --run-dir "/absolute/path/to/project/review-runs/<build-key>" \
  --reviewer "Agent name" \
  --notes "具体说明检查了哪些镜头、字幕、封面和声音" \
  --result pass
```

If any check fails or cannot be completed, record `--result fail` and add one `--failed-check <check-name>` for each blocker. Never record a pass for an unlistened soundtrack or an uninspected full-resolution frame. `finalize` will reject a failed or stale visual record.

## Stage 9: finalize and verify

Before `finalize`, the semantic report must say `素材授权：已确认`, and the regenerated approval must bind `rights_clearance: confirmed`. Changing the report or approval invalidates the previous review run, so rerun the build after confirming rights. `finalize` returns `RIGHTS_PENDING` for a pending approval. Only a successfully finalized and verified `deliverables/<build-key>/` bundle is upload-ready.

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" finalize \
  --config "/absolute/path/to/project/project.json" \
  --run-dir "/absolute/path/to/project/review-runs/<build-key>"

python3 "$VIDEO_SKILL_ROOT/scripts/workflow.py" verify \
  --bundle "/absolute/path/to/project/deliverables/<build-key>"
```

Report the final video, 3:4 cover, 4:3 cover, subtitles, review evidence, QA paths, and retained rights evidence. State any remaining attribution, upload, or platform-label actions the user must perform.

## Stop conditions

Stop before TTS when any of these is true:

- narration approval is missing, failed, incomplete, or stale;
- one of the two independent imagegen cover sources or final covers is missing;
- the clean base has audio, the wrong canvas/FPS, or visible pixelated enlargement;
- captions do not reconstruct the narration;
- source evidence cannot support the material claim;
- a scene would need a retime factor outside the configured limits.

Pending material authorization does not block TTS or a private `review-runs/` build. Stop before `finalize` when a required right or authorization is still pending, or when automatic QA or full visual/audio review fails. Fix the real source, script, timeline, cover, or mix; confirm rights in the structured report and rebuild before delivery. Do not weaken the gate.

## Release self-test

After copying or extracting this Skill on another Mac, run the offline contract suite before the first project:

```bash
python3 -m unittest discover \
  -s "$VIDEO_SKILL_ROOT/scripts/tests" -v

SOURCE_LED_RUNTIME_DIR="/absolute/path/to/temporary-runtime" \
python3 "$VIDEO_SKILL_ROOT/scripts/bootstrap_runtime.py" --skip-aligner
```

Then run the normal bootstrap without `--skip-aligner` once. The test suite must pass before live TTS or project rendering.
