# Imagegen cover standard

## Mandatory outputs

Generate two independent finished cover compositions with the built-in `image_gen` tool:

1. 3:4 portrait source, finalized at 1080x1440.
2. 4:3 landscape source, finalized at 1440x1080.

Do not crop one generated image into both ratios. Save both generated sources under `covers/source/`, then run `scripts/render_cover.py` to normalize exact dimensions and create final PNGs under `covers/`.

Files under `covers/source/` are independent ImageGen masters. They may already contain the final typography, but user-facing previews, project-root copies, and delivery bundles still use the normalized `cover.*.final` paths.

## Hook standard

- Use one hook, 4-20 Chinese characters, derived from the video's concrete result, method, or surprise.
- Store its exact one- or two-line display structure in `cover.headline_lines`. The lines must reconstruct the hook and must be copied verbatim into both ImageGen prompts.
- Make the hook understandable without knowing the model name.
- Prefer “一句话画出机械臂”, “玩具照片变成手机游戏”, or an equally concrete result.
- Reject generic copy such as “AI太强了”, “未来已来”, “颠覆想象”, “科技改变生活”, or “普通人一定要学”.
- Put the hook in both filenames: `封面-3x4-<钩子>.png` and `封面-4x3-<钩子>.png`.

## Imagegen prompt contract

Use case: `ads-marketing`.

Include:

- Asset type and exact aspect ratio.
- The real visual result from the video as the subject.
- A composition that stays legible at phone-feed size.
- The exact reviewed hook verbatim, using the exact line breaks from `cover.headline_lines`, plus placement, typography, and highlighted keywords. Require no other text.
- A conclusion-led social headline treatment: very large Chinese type, thick outline, dark shadow, one high-contrast keyword, and enough safe margin. Avoid a lower-third subtitle strip.
- A faceless composition. Set `cover.allow_people` to `false`; prohibit people, faces, avatars, silhouettes, and human hands. Permit them only after an explicit user override for that project.
- Deliberate space where the headline and result can dominate together: normally upper-left for 3:4 and the left side for 4:3.
- The video's material and color cues.
- Constraints: exact hook only; no extra text, logos, watermarks, X/GitHub UI, URLs, account handles, QR codes, or unrelated objects.

Make one built-in imagegen call per ratio. The built-in tool needs no OpenAI API key. If it is unavailable, stop and report `IMAGEGEN_UNAVAILABLE`; do not silently substitute HTML, SVG, or an unapproved paid CLI.

## QA

- Inspect both generated sources at full size. Confirm every Chinese character, line break, highlight, subject detail, and safe margin.
- Inspect both at 25% feed-preview size. The conclusion, highlighted keyword, and subject must read immediately.
- If direct ImageGen text is wrong, cropped, weak, or accompanied by extra text, make one targeted regeneration for that ratio and recheck. If the second result still fails, make one independent text-free clean master for that failed ratio, save it at a new source path, and set only `cover.<ratio>.text_mode` to `deterministic`. Require no text, letters, or numbers in this fallback prompt. Keep a passing ratio in `imagegen` mode. Never draw deterministic text over an ImageGen headline.
- Verify the final dimensions with FFprobe.
- Verify the hook is exact, readable, inside the safe area, and not covered by a high-detail subject.
- Inspect a 25%-scale preview as well as the full-size files. At feed size, the result silhouette and the complete hook must remain immediately readable; reject layouts that look like a corporate poster, product catalogue, report cover, or subtitle strip.
- Compare hashes: the two generated sources must differ.
- Open both final covers at full size. Reject misspelled text, extra model-generated text, any person when `allow_people` is false, distorted objects, unrelated logos, weak contrast, or a hook disconnected from the video.
