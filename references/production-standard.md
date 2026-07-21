# Source-led AI short-video production standard

## Contents

- Topic and evidence
- Script shape
- Source-led edit
- Information-card edit
- ChatCut handoff
- Publication package

## Topic and evidence

- Serve ordinary people, one person at a time. Reject enterprise-only stories unless an individual can immediately understand or use the result.
- Prefer a concrete community Builder result over an official product announcement: a surprising visual, a useful mini-demo, a new workflow, or an open-source project with obvious personal value.
- Score for timeliness, visual payoff, low comprehension threshold, usefulness, and save-worthiness.
- Collect the original post, relevant replies, author explanation, linked project page, and original video into private evidence. Use Chrome with the user's logged-in state when X access is needed; do not default to a headless browser.
- Verify ordinary factual claims. Do not deploy an open-source project merely to prove it works unless the user explicitly asks. Take the toolchain from the author's text first, then reliable linked material, then clearly marked inference.
- Preserve rights evidence. Attribution does not create permission. Material with pending authorization may appear only in a private review run; clear or remove it before final delivery.

## Script shape

- Finish within 90 seconds.
- Keep the normalized narration at 480 characters or fewer so an obviously overlong script is rejected before a paid TTS request; final audio still must pass the 90-second media check.
- Use the configured Volcengine female, knowledgeable-sounding voice at natural `1.0` speed. Do not synthesize scene by scene and do not time-stretch the narration.
- Include user-supplied or otherwise rights-cleared BGM in an upload-ready project. A private review run may retain explicitly pending music authorization; `finalize` must remain blocked until it is cleared. Omit music only for an explicitly approved no-music version or a technical smoke test.
- Run the bundled `yuwen-publish-precheck` lexical and semantic review on the exact final narration before any voice synthesis, including tests or retries. A script, hook, scope, commercial-flag, or scene-order change invalidates the approval.
- Open with the concrete result, conflict, or tool workflow. Front-load the tool recommendation and the short “how it was made” chain.
- Give the viewer one reason to save: a usable toolchain, prompt idea, workflow, or practical judgment.
- Use the author/source facts as evidence and add an original judgment. Do not translate the original post line by line.
- Avoid abstract AI hype, boundary lectures, and generic endings. Never use slogans such as “AI is changing everything,” “the future has arrived,” or “ordinary people must seize the opportunity.”
- Do not create a separate disclaimer scene. Tighten an unsupported claim inside the sentence where it appears.
- Create scene text and editorial captions together. Each caption must reconstruct the spoken scene exactly after punctuation and whitespace are ignored.

## Source-led edit

- Let cited source footage carry the visual proof; commentary explains what to notice and why it matters.
- Never enlarge a low-resolution source until edges become visibly jagged. Use fit, crop, restrained reframing, background treatment, or a different source shot.
- Remove external-platform UI, account handles, URLs, QR codes, and repository paths from the public frame. Do not remove ownership watermarks to misrepresent authorship.
- Keep full source attribution and evidence in project records; place public attribution in the post body or pinned comment when appropriate.
- Do not burn captions into the clean base. Do not keep its original audio. Export exactly one 1080x1920, CFR 30 fps, video-only H.264 MP4.

## Information-card edit

- For an open-source project or multi-step toolchain, make one readable HTML/HyperFrames information card and insert it into the clean base for roughly 6-12 seconds.
- Show the minimum useful chain, for example input -> code -> editable model -> verification -> output.
- Mark captions covering the dense information-card interval with `"show": false`; keep them in the full subtitle file and narration alignment.
- Avoid adding a second layer of labels or subtitles over the card. Verify the transition frame before and after the card.

## ChatCut handoff

- Use ChatCut as the preferred assembly editor.
- Load the installed ChatCut plugin's basics, asset-import, verification, and export Skills before editing. Import original source assets, preserve the editable timeline, and do not replace it with a locally concatenated or pre-flattened review file.
- Put source clips and the information card on visual tracks. Keep voice, BGM, and automatic captions out of the clean-base export.
- Verify the composed timeline with ChatCut project state and representative rendered frames, then create the clean base through ChatCut's documented cloud or local-CLI export route as appropriate for the asset state.
- Record scene boundaries in source timeline order. Use half-open ranges `[in, out)` and keep the order in the JSON array; never sort by scene ID.
- Treat the clean base as the deterministic boundary. The Skill handles review gating, TTS, alignment, scene retiming, captions, music, render, and QA after this point.

## Publication package

- Treat `review-runs/<build-key>/` as a private review artifact. TTS, rendering, and visual QA may proceed while material authorization is explicitly pending, but that directory is not upload-ready.
- Deliver the final MP4, 3:4 cover, 4:3 cover, full and platform subtitle files, narration masters, QA reports, contact sheet, review evidence, and provenance manifest in one atomic `deliverables/<build-key>/` bundle only after material authorization is confirmed.
- Keep source links and author credit outside the public video frame unless a specific reference is necessary and has been approved.
- Apply the AI-generated-content label required by the chosen platform workflow.
