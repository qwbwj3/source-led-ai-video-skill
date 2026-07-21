# Operations, cache, and efficiency

## Reliable execution boundary

Run one project per focused Agent task. The structured Source Package, edit
plan, prompt record, QA files, and append-only events are the handoff surfaces.
Keep raw source material available, but do not feed the full source set into
every reasoning stage.

`workflow.py status` is a read-only diagnostic. It reports:

- missing media, approval, or V2 contracts;
- stage attempts and latest build elapsed seconds;
- active review run and visual result;
- rights readiness;
- the next safe action.

It treats a review run as active only when its manifest, current configuration,
approval, implementation, V2 contracts, and automatic QA still validate. A
newer stale directory cannot make `status` recommend finalization.

Use `resume` after a failure. Its supported recovery boundaries are explicit:

1. Reuse an intact completed build.
2. Reuse a video core that passed automatic QA when cover or release-only state
   changed.
3. Safely rerun earlier unfinished stages.

Staging and stage events aid diagnosis. They are not a promise that every
internal stage resumes from its final instruction.

## Cache identity

The video-core key binds script approval, clean base, BGM, source content,
semantic edit plan, TTS profile, aligner/runtime, and renderer implementation.
The cover key binds both generated files and the completed prompt provenance.
The build key binds rights-sensitive Source Package state, release
implementation, review policy, and cover state. The separate release key binds
that build key plus the exact rights-ledger hash.

Consequences:

- a cover-only change can reuse a passed video core and rerun cover/release QA;
- changing rights status can reuse unchanged content rendering, then rebuild
  review/release provenance; a new run still needs its own schema-3 review;
- script, clean-base, BGM, source media, edit-plan, TTS, aligner, or renderer
  changes invalidate video reuse;
- analytics and creator feedback never invalidate media.

Corrupt or incomplete cache entries are treated as misses. Video cache is
published only after automatic QA passes.

`deliverables/latest.json` maps both identities to the current verified bundle.
Never infer a release path from a build key; use the `release_key` returned by
`finalize`. `status` compares that record with current live rights. A later
revocation or ledger change marks it stale; do not upload an older bundle merely
because its private snapshot still verifies historically.

Finalization takes the rights snapshot while holding the append lock, derives
the release key from the copied ledger bytes, and rechecks that identity inside
the bundle. Failed pre-publication staging is removed. A corrupt existing release
or failed post-publication verification is moved to a hidden quarantine and
`latest.json` is not left pointing at it.

## Time and token measurement

Do not set 10–20 minutes or a fixed token count as a guaranteed SLA. ChatCut,
ImageGen, Volcengine, network latency, source complexity, and human review can
dominate wall time. Use the first 10 real projects as the baseline.

For each project, record:

- source collection, source understanding, script/review, ChatCut, covers,
  TTS/render, and QA elapsed time;
- cache hits and retries;
- model input/output/processed tokens when the host exposes them;
- external credit cost and human review minutes.

Report median, P75, and failure/retry rate. Separate cold projects from cache
rebuilds. A useful operational target is chosen after these measurements.

## Model policy

A single Terra High Agent can execute the normal project when it works from the
compact contracts and opens raw evidence only on demand. Start a fresh task for
each video so prior project media and reasoning do not pollute context. Escalate
model strength or use a second reviewer for disputed fact interpretation,
script judgment, unfamiliar rights scope, or repeated QA failure.

Scripts own deterministic work: schema/path/hash validation, cache identity,
TTS adapter, alignment, retiming, subtitles, audio mix, render, evidence-frame
extraction, rights state, manifests, and mechanical QA. Agents own source
judgment, script, shot choice, ChatCut decisions, ImageGen prompt/inspection,
semantic publish review, and full visual/audio review. Do not automate those
judgment points into unchecked pass markers.
