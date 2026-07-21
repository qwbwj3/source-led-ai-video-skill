# Source Package contract

The Source Package preserves deep source understanding once and gives later
agents a compact, evidence-addressable input. It replaces repeated bulk reads
of the same post, replies, README, and documentation. It contains exactly:

```text
source-package/
  manifest.json
  claims.jsonl
  assets.json
  toolchain.json
  brief.md
```

Raw snapshots may live elsewhere in the project, conventionally under
`source-package/raw/`. All referenced files remain project-relative.

## Manifest

`manifest.json` uses version 1 and records:

- `source_type`: `x`, `github`, `builder`, or `prepared`;
- canonical `source_url`, timezone-aware `captured_at`, `analyzer_version`, and
  `schema_version: 1`;
- optional revision metadata such as commit, release, branch, or X snapshot;
- one or more sources with a stable `source-*` ID, source kind, URL, capture
  time, snapshot path, and exact SHA-256;
- `package_sha256`, computed from the manifest without this field plus claims,
  assets, toolchain, and the brief content hash.

The manifest is written last and acts as the package commit marker. A partial
write cannot validate as a cache hit.

## Claims and evidence

`claims.jsonl` contains one JSON object per non-blank line. Each claim has:

- a unique lowercase `claim-*` ID;
- statement;
- classification: `author_statement`, `corroborated_fact`,
  `editorial_inference`, or `unverified`;
- confidence: `high`, `medium`, or `low`;
- timezone-aware `verified_at`;
- at least one evidence item containing a known `source_id` and precise
  `locator`. `excerpt_sha256` is optional.

Unknown source IDs and evidence-free claims are invalid. A
`corroborated_fact` needs evidence from at least two distinct source IDs. A
public project scene cannot cite an `unverified` claim.

## Assets

`assets.json` uses version 1. Every item has a unique `asset-*` ID, kind, known
`source_id`, project-relative local path, exact SHA-256, and rights state
`pending`, `confirmed`, or `rejected`. Video assets also require a positive
integer `frame_count`; width and height, when present, are positive integers.

Every V2 scene must cite asset IDs that exist in this file. A rejected asset
cannot enter a scene or edit plan. Pending assets may enter a private review run;
final delivery requires `confirmed` plus an active rights-ledger record.

## Toolchain and brief

`toolchain.json` uses version 1. Every ordered step has a `step-*` ID, tool,
action, and one or more valid claim references. `brief.md` is non-empty and is
the default low-token reading surface. Script, edit, and cover work read the
brief plus only the cited structured records. Agents reopen a raw snapshot when
a claim conflicts, lacks enough evidence, or needs freshness verification.

## Commands

Choose stable claim and asset IDs first, then bind those IDs in every
`project.json` narration scene. Prepare a JSON draft with keys `manifest`,
`claims`, `assets`, `toolchain`, and `brief`; omit
`manifest.package_sha256` because the writer computes it. The writer validates
the project bindings, so writing the package before the scene IDs are in place
is intentionally rejected.

`init_project.py` copies a complete editable skeleton to
`source-package-draft.json`. Replace its example URLs, timestamps, statements,
locators, paths, dimensions, frame count, and zero hashes with the captured
values. Exact source `kind` values are `x_post`, `x_reply`, `github_readme`,
`github_release`, `documentation`, `project_page`, and `prepared_note`; exact
asset kinds are `video`, `image`, `audio`, and `document`.

```bash
python3 scripts/source_package.py write \
  --config /absolute/project/project.json \
  --draft /absolute/project/source-package-draft.json

python3 scripts/source_package.py validate \
  --config /absolute/project/project.json
```

The writer validates all evidence, paths, IDs, versions, referenced file hashes,
and scene bindings before publishing package files atomically. The manifest is
the cache commit marker. Keep the composite draft as the editable source of
truth. To change an asset's rights state, edit that asset in the draft, confirm
that `manifest.package_sha256` is still absent, and rerun `write`; never patch
`source-package/assets.json` alone. A rights-only rewrite changes the full
package hash while keeping the source-content video cache eligible when all
content fields and media hashes remain unchanged.
