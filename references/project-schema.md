# Project JSON contract

## Required structure

- `version`: `1`.
- `project_name`: non-empty.
- `platform`: `douyin`.
- `commercial`: default to `true` when unknown.
- `review_scope.industries`: sorted unique list containing only `medical` and/or `finance`; leave empty for ordinary AI content. It binds the lexical and semantic review scope.
- `canvas`: fixed at `1080x1920`, `30` fps.
- `paths.base_video`: relative path to a clean, video-only MP4.
- `paths.bgm`: relative path to music. Normal publishable projects use user-supplied or otherwise rights-cleared BGM; remove the key or set it empty only for an explicit no-music version or a technical smoke test.
- `narration.scenes`: ordered scene array. Preserve array order even if IDs look sortable.
- The normalized narration is capped at 480 characters before review or paid TTS. This is a conservative preflight for the fixed 90-second format; final audio duration remains the authoritative QA gate.
- Each scene has a lowercase hyphenated `id`, exact spoken `text`, and editorial `captions`.
- A caption may be a string or `{ "text": "...", "show": false }`. Hidden captions remain aligned and exported but are not burned into the video.
- `source_timeline.scene_boundaries_seconds`: `scene count + 1` half-open boundaries beginning at `0` and ending at the clean-base duration.
- `source_timeline.retime_ratio_limits`: default `[0.8, 1.2]`. Re-edit the clean base when a scene falls outside this range.
- `cover`: one shared hook plus separate imagegen sources and final paths for `3x4` and `4x3`.
- `cover.headline_lines`: one or two exact display lines whose concatenated spoken characters reconstruct `cover.hook`. New projects must set it so every Agent gives ImageGen the same line breaks and the deterministic fallback uses the same structure. Legacy configs may omit it and use the renderer's semantic wrapping.
- `cover.text_mode`: new projects use `imagegen`. The independently generated source already contains the exact reviewed hook; `render_cover.py` only normalizes dimensions. Use `deterministic` only after one targeted ImageGen text correction still fails visual QA, or when the user explicitly requests deterministic typography. Legacy version-1 configs without this field retain `deterministic` rendering for compatibility.
- `cover.3x4.text_mode` and `cover.4x3.text_mode`: optional per-ratio overrides. Use one only when that ratio needs the deterministic fallback. Before setting it, replace that ratio's text-bearing failed master with a newly generated, independent, text-free clean master at a new `generated_source` path. Never draw deterministic text over an ImageGen headline.
- `cover.allow_people`: default `false` for this faceless account. Set `true` only when the user explicitly changes the account or project direction.

## Path rules

- Keep every project path relative to `project.json`.
- Reject `..`, absolute paths, shell fragments, newlines, and missing inputs.
- Support spaces and Chinese characters by passing subprocess arguments as arrays; never build shell command strings.

## Caption reconstruction

After removing punctuation, spaces, and hyphens, concatenating a scene's captions must exactly reconstruct the scene text. This prevents missing words and stale captions before the paid TTS call.

Start from `assets/project-template.json`; do not copy an old sample's absolute paths or fixed caption-mute timestamps.
