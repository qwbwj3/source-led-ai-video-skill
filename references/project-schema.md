# Project JSON contract

`project.json` is the production configuration source. Version 2 adds evidence,
semantic edit, layout, and cover-provenance references without creating a
parallel `script.json`. Version 1 remains accepted for existing projects and
regression fixtures, but new projects default to V2.

## Required structure

- `version`: `2` for new projects; `1` is legacy-compatible.
- `project_name`: non-empty.
- `platform`: `douyin`.
- `commercial`: default to `true` when unknown.
- `review_scope.industries`: sorted unique list containing only `medical` and/or
  `finance`; leave empty for ordinary AI content.
- `canvas`: fixed at `1080x1920`, `30` fps.
- `paths.base_video`: relative path to a clean, video-only MP4.
- `paths.bgm`: relative path to rights-cleared music. It may be absent or empty
  only for an explicit no-music version or technical smoke test.
- `narration.scenes`: ordered scene array. Preserve array order even if IDs look
  sortable.
- `source_timeline.scene_boundaries_seconds`: `scene count + 1` half-open
  boundaries beginning at `0` and ending at the clean-base duration.
- `source_timeline.retime_ratio_limits`: fixed at `[0.8, 1.2]`.

The normalized narration is capped at 480 characters before review or paid TTS.
Final audio duration remains the authoritative 90-second gate.

## Version 2 scene contract

Each scene keeps the existing `id`, `text`, and `captions` fields and adds:

```json
{
  "id": "scene-01",
  "purpose": "结果钩子",
  "text": "一句完整口播。",
  "claim_ids": ["claim-01"],
  "asset_ids": ["asset-01"],
  "caption_region": "bottom",
  "captions": ["一句完整口播。"]
}
```

- `purpose`: non-empty editorial purpose.
- `claim_ids`: non-empty, unique lowercase `claim-*` references into
  `source-package/claims.jsonl`.
- `asset_ids`: non-empty, unique lowercase `asset-*` references into
  `source-package/assets.json`.
- `caption_region`: `bottom`, `top`, or `hidden`. Keep one region stable for the
  entire scene.
- A caption may be a string or `{ "text": "...", "show": false }`. Hidden
  captions remain aligned and exported but are not burned into the video.
- `caption_style.margin_v` controls the bottom style. Optional
  `caption_style.top_margin_v` controls the top style and must remain inside the
  canvas safe area.

Concatenated captions must reconstruct the scene text after punctuation and
whitespace are ignored. `narration_payload` includes the V2 evidence and layout
fields, so changing them invalidates hash-bound review state.

## Version 2 artifact references

```json
{
  "source_package": {
    "manifest": "source-package/manifest.json"
  },
  "edit": {
    "plan": "edit/edit-plan.json",
    "timeline_lock": "edit/timeline.lock.json"
  },
  "cover": {
    "prompt_record": "covers/cover-prompt.json"
  }
}
```

These paths are fixed. Writers reject aliases, alternate destinations, and any
attempt to target `project.json`, review approval, or another contract file.
Their schemas are in `source-package.md` and `edit-plan.md`.

## Cover contract

- Use one shared 4-20 character hook plus separate ImageGen sources and final
  paths for `3x4` and `4x3`.
- `cover.headline_lines` contains one or two exact display lines whose spoken
  characters reconstruct `cover.hook`.
- New projects use `cover.text_mode: imagegen`. Version-1 configs without the
  field retain deterministic rendering compatibility.
- Optional per-ratio `text_mode` overrides are allowed only for the independent
  text-free fallback source of that ratio.
- `cover.allow_people` defaults to `false`.
- V2 `cover.prompt_record` binds the exact hook, line breaks, prompts, model,
  generation attempt, result ID, generated-source path, and content hash.

## Path rules

- Keep every project path relative to `project.json`.
- Reject `..`, absolute paths, control characters, shell fragments represented
  as paths, symlink escapes, and missing declared inputs.
- Source Package, edit-plan, timeline-lock, and cover-prompt writers validate
  the fixed destination tree before atomic replacement.
- Support spaces and Chinese characters by passing subprocess arguments as
  arrays; never build shell command strings.

Start new work from `assets/project-template.json`. Use
`init_project.py --project-version 1` only for a deliberate legacy fixture.
