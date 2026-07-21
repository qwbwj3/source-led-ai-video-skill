# Edit plan, timeline lock, and cover provenance

## Semantic edit plan

`edit/edit-plan.json` is a version-1 semantic plan for a V2 project. It does not
set final narration timing. It uses 30 fps and contains every `project.json`
scene in the same order.

Each scene repeats its exact `scene_id`, `purpose`, and `caption_region`, then
declares one or more shots:

```json
{
  "asset_id": "asset-01",
  "source_in_frame": 30,
  "source_out_frame": 180,
  "timeline_weight": 1,
  "track_id": "V1",
  "crop_mode": "fit",
  "allow_scale_up": false
}
```

The asset must be a video declared by both the project scene and Source Package.
Every asset declared by a scene must be used by that scene's plan. Source ranges
are half-open integer frames and cannot exceed the asset's recorded frame count.
Their summed duration must match the intended clean-base scene length at 30 fps.
An optional information card references a project-relative HTML path and its
exact SHA-256; traversal, symlinks, missing files, and stale hashes are rejected.

This semantic plan guides the editable ChatCut assembly. ChatCut follows the
source ranges, shot order, crop policy, information card, and clean-base scene
boundaries, then exports the clean video-only base. Final narration timing is
still unknown at this point.

## Integer-frame timeline lock

After TTS and forced alignment, the local workflow derives ordered integer scene
durations:

```json
{
  "version": 1,
  "fps": 30,
  "alignment_sha256": "<sha256>",
  "scenes": [
    {"scene_id": "scene-01", "duration_frames": 150}
  ]
}
```

The compiler distributes each scene duration across its shots using positive
integer weights, then writes `edit/timeline.lock.json`. The lock records integer
source and timeline frames, track IDs, crop/upscale policy, retime ratios, and
hashes of the edit plan, duration input, forced alignment, clean base, and Source
Package. Scenes and clips must be contiguous, non-empty, and within the
configured retime range.

```bash
python3 scripts/edit_plan.py write-plan --config project.json --draft edit-plan-draft.json
python3 scripts/edit_plan.py validate-plan --config project.json
python3 scripts/edit_plan.py compile --config project.json --durations scene-durations.json
python3 scripts/edit_plan.py validate-lock --config project.json
```

The production workflow performs the compile step automatically after alignment.
The standalone commands support contract tests and diagnosis. The final lock is
a deterministic record of local post-alignment retiming; it is not a pre-ChatCut
input.

## Cover prompt provenance

`covers/cover-prompt.json` uses version 1 and binds the current project hook and
exact line breaks. It contains independent `3x4` and `4x3` generation records.

Initialization creates a version-1-compatible pending record from the project
template. After setting the real hook, filenames, visual settings, and text
modes, run `sync-cover`. It upgrades the record to prompt version 2 and stores a
separate semantic revision for each ratio.

Each completed or failed call appends an attempt to the current ratio revision.
An attempt records the prompt, model, time, status, source path, and provider
result ID when available. When the built-in tool discloses no model, omit
`--model` and the writer records `builtin-imagegen`. When it discloses no result
ID, omit `--result-id`; the writer creates a clearly local `local-attempt:*` ID
while keeping `result_id` null. A completed attempt reads and hashes the
configured source. A failed attempt requires a retry reason.

After a semantic change, `sync-cover` resets only affected ratios. It archives
their prior revision and full attempt list, preserving an unaffected completed
ratio. Shared hook, headline, people, or visual-brief changes affect both ratios;
a per-ratio source path or effective text mode affects only that ratio. A
production validation needs two current completed records with distinct call
identities and distinct source hashes.

```bash
python3 scripts/edit_plan.py init-cover --config project.json
python3 scripts/edit_plan.py sync-cover --config project.json
python3 scripts/edit_plan.py record-cover --config project.json --ratio 3x4 \
  --status completed --prompt "full prompt" --model "actual model" \
  --result-id "unique call id"
python3 scripts/edit_plan.py record-cover --config project.json --ratio 4x3 \
  --status completed --prompt "full prompt" --model "actual model" \
  --result-id "different call id"
python3 scripts/edit_plan.py validate-cover --config project.json
```

`init_project.py` creates the pending provenance file automatically for V2.
Run `sync-cover` again after changing one ratio for deterministic fallback and
before recording its next attempt.
