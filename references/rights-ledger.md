# Rights ledger contract

Rights evidence controls release. Attribution is recorded separately and does
not substitute for permission. V2 finalization requires an active schema-2
confirmation for every asset used by the deliverable:

- `base_video`;
- `bgm` when configured;
- every `asset-*` referenced by a narration scene.

Source Package assets also need `rights_status: confirmed`. Pending assets may
enter a private `review-runs/` build and cannot enter `deliverables/`.

## Evidence and scope

Place evidence under `private/rights-evidence/`. A confirmation binds:

- exact asset ID and current file SHA-256;
- source ID, author, and source description;
- Douyin, mainland-China region, and commercial use;
- rights basis and optional expiry;
- whether credit is required, its body/pinned-comment placement, and exact text;
- evidence path and SHA-256;
- reviewer and UTC confirmation time.

Do not place credentials, login exports, private contact details, or unrelated
personal data in the evidence file. Preserve the minimum artifact needed to
show the permission or license decision.

```bash
python3 scripts/rights_ledger.py confirm \
  --config /absolute/project/project.json \
  --asset-id asset-01 --source-id source-01 \
  --author "Author" --source "Original post and attached video" \
  --rights-basis "Written permission for Douyin commercial publication" \
  --evidence-path private/rights-evidence/asset-01.txt \
  --confirmed-by "Reviewer" \
  --attribution-placement body_or_pinned_comment \
  --attribution-text "Source: author and project name"
```

Use `base_video` with source ID `source-base-video`, and `bgm` with source ID
`source-bgm`. An `asset-*` source ID must exactly match its Source Package
declaration. If attribution is not required, use
`--attribution-placement none` and omit attribution text. Add `--expires-at`
when permission is time-limited.

## Revocation and supersession

The ledger is append-only. Never edit an old confirmation to simulate a new
state. Append a revocation when permission, source ownership, allowed scope, or
file identity changes:

```bash
python3 scripts/rights_ledger.py revoke \
  --config /absolute/project/project.json \
  --asset-id asset-01 \
  --reason "Permission withdrawn" \
  --confirmed-by "Reviewer"
```

For schema 2, the latest valid event wins for the `(asset_id, asset_sha256)`
identity. Schema-1 records retain digest-only compatibility. A later revocation
invalidates the matching earlier confirmation until a new confirmation is
recorded. Expired, non-commercial, non-Douyin, or non-CN scope is inactive.

During finalization, the ledger and its referenced evidence are copied into the
atomic deliverable and verified again. A changed asset hash needs a new record.
The full historical ledger remains auditable, so every confirmation's evidence
path must remain available. Save changed evidence at a new versioned path;
overwriting one path would invalidate the older confirmation hash.

The release key binds the build key and the exact rights-ledger snapshot copied
into the bundle. A new valid confirmation, evidence version, or attribution
requirement therefore creates a new immutable release directory. A revocation
makes `latest.json` stale and blocks publication until the affected asset is
validly confirmed again; it does not create a publishable revoked release.
