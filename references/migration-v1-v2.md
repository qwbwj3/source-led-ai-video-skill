# V1 to V2 migration

Keep an existing V1 project and its deliverables intact. Create a new V2 project
directory, copy only the source media that will still be used, and rebuild the
contracts. Do not change `version` in place and expect old review state to pass.

1. Run `init_project.py` without `--project-version`; V2 is the default.
2. Copy the clean base, BGM, selected source media, snapshots, and information
   card into the new project.
3. Write the Source Package and add `purpose`, `claim_ids`, `asset_ids`, and
   `caption_region` to every scene.
4. Copy the narration only after checking it against the new evidence bindings.
5. Write the semantic edit plan and verify its source ranges against the clean
   base boundaries.
6. Set the final hook, rerun the full publish precheck, and create a new
   approval. Old approval hashes do not migrate.
7. Generate or re-record two independent cover calls. Old cover files without
   prompt/result provenance do not satisfy V2.
8. Run a new build. A legacy TTS recording can be reused only with the explicit
   in-project regression-replay proof required by `workflow.py`.
9. Record schema-2 rights for the clean base, BGM, and every used source asset.
   Change asset rights state in the retained composite Source Package draft,
   rerun the package writer, update the semantic report to `素材授权：已确认`, and
   generate a new hash-bound approval.
10. Run or resume again. Rights-sensitive state changes the build identity even
    when the passed video core is reused.
11. Complete a new schema-3 visual/audio review against that exact new run and
    its generated evidence frames. A schema-2 review remains historical and
    cannot finalize V2.
12. Finalize and verify the returned `deliverables/<release-key>/` bundle.

V1 remains executable for old fixtures and controlled legacy maintenance. New
features such as Source Package binding, cover call provenance, per-scene caption
regions, timeline lock, source-asset rights, and schema-3 evidence review require
V2.

Keep an old deliverable as historical evidence. A pre-V1.2 bundle can lack the
packaged rights ledger, release identity, and schema-3 review required by the
current verifier, so historical preservation does not imply that the new
`workflow.py verify` command will accept it. Rebuild through a V2 review run when
the artifact must become upload-ready under the current contract.
