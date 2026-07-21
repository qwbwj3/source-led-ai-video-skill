# Optional post-publication Feishu share document

This module starts only after a video has been published and the user makes an
explicit decision for that exact published artifact. It is independent from
source collection, rendering, QA, release, cache identity, and analytics. A
successful upload, moderation pass, or traffic result never triggers it by
itself.

## Decision boundary

For every published video, ask or use the user's explicit instruction to choose
one state:

- `include`: prepare one concise public entry and sync it to the Feishu master
  document;
- `skip`: record the decision locally and make no Feishu write.

Use platform performance and moderation outcome only as evidence for the human
decision. Do not implement a views threshold or change `skip` to `include`
automatically.

The publication ID is a local, non-secret label for the published batch. When
the same video is published on Douyin and Xiaohongshu together, one value such
as `sample-002-douyin+xhs` may represent that batch. Bind the decision to the
SHA-256 of the exact published video. Supply a Build Key only when a genuine
Skill build produced that file; legacy videos leave it unset.

## Public entry contract

Start from `assets/share-doc-entry.example.json`. Keep the entry useful on a
phone and normally within 250–600 Chinese characters, excluding URLs. It must
contain:

- a concrete title and one-line takeaway;
- a short explanation of what the project or case does;
- a 3–5 step minimum toolchain;
- the repository URL when the entry is an open-source project;
- the original demonstration or primary source when available;
- only the most useful additional links or materials.

Supported project types are `open_source_project`, `community_demo`,
`ai_visual_case`, `model_capability_case`, `tool_workflow`, `prompt_method`, and
`other`. One entry has one primary type.

Use these exact Feishu `h2` headings; do not invent synonyms:

| `project_type` | Feishu category heading |
|---|---|
| `open_source_project` | 开源项目 |
| `community_demo` | 社区创作案例 |
| `ai_visual_case` | AI 视觉案例 |
| `model_capability_case` | 模型能力案例 |
| `tool_workflow` | 工具与工作流 |
| `prompt_method` | 提示词与方法 |
| `other` | 其他 |

The optional `prompt` object requires provenance:

- `author_public`: the author published the exact prompt;
- `author_summary`: the wording is a faithful summary of the author's public
  explanation;
- `editorial_reconstruction`: an editor inferred a reusable reference from the
  demonstrated method.

The generated heading labels these respectively as `原作者提示词`,
`作者说明整理`, and `参考写法`. If no defensible prompt exists, omit `prompt`
entirely. Do not insert an empty section, fabricate a prompt, or describe an
editorial reconstruction as the author's wording.

## Record the decision

For inclusion:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/share_doc.py" decide \
  --project "/absolute/path/to/video-project" \
  --publication-id "sample-002-douyin+xhs" \
  --video-sha256 "<sha256 of the published video>" \
  --decision include \
  --project-type open_source_project \
  --decided-by "Hanye" \
  --decision-note "已发布，确认作为长期资料分享" \
  --content-json "share-doc-entry.json"
```

Add `--build-key <20-lowercase-hex>` only when it is the authentic Build Key
for this video. `--content-json` is relative to the project root.

For a skipped entry:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/share_doc.py" decide \
  --project "/absolute/path/to/video-project" \
  --publication-id "sample-003-douyin" \
  --video-sha256 "<sha256 of the published video>" \
  --decision skip \
  --project-type community_demo \
  --decided-by "Hanye" \
  --decision-note "本次不进入长期资料库"
```

The module writes append-only state and deterministic XML below
`.source-led-ai-video/share-doc/`. It makes no Feishu API call. Repeating the
same decision is idempotent; changing an already recorded decision or editing
the generated XML is blocked.

## Feishu master document

Use one mobile-first master document. Create category headings only when the
first entry of that type is added, so readers do not see empty sections. The
shape is:

```text
文档标题
  H2 开源项目
    H3 某个项目标题
  H2 社区创作案例
    H3 某个案例标题
```

The XML entry already uses `h3` and contains a stable visible `资料编号` plus
`内容版本`. Use the `lark-doc` Skill with user identity and API v2. Keep the
master document URL/token in machine-local operator state, never in the Skill
repository or a public delivery bundle. Local sync receipts retain only SHA-256
fingerprints of the Feishu document and block identifiers.

Store the target on each Mac at
`~/.config/source-led-ai-video/share-doc-target.json`, mode `0600`:

```json
{
  "schema": 1,
  "document_url": "https://example.feishu.cn/docx/REPLACE_WITH_REAL_DOCUMENT"
}
```

The script does not read this file; the Agent reads it before calling
`lark-cli`. On a new Mac, log in to Feishu again and recreate this one target
file. Do not copy OAuth or access tokens.

### First document creation

Create a short `create-master.xml` in the current working directory. Do not add
empty categories:

```xml
<title>AI 项目资料库｜视频同款工具、源码与提示词</title>
<p>这里整理视频里真正值得收藏的项目、最短工具链和原始资料。只收录已经发布、并确认值得长期保留的内容。</p>
<p>提示词和方法会标明来源；没有找到原作者完整提示词时，不会自行补写成作者原文。</p>
```

Create it in the user's personal library and save the returned URL to the
machine-local target file:

```bash
lark-cli docs +create --api-version v2 --as user \
  --parent-position my_library \
  --content @create-master.xml --format json
```

Keep the document private for review. Public-link permissions are a later,
separate action.

### Exact idempotent insertion commands

Before every write:

1. Read the generated entry XML and its `资料编号` and `内容版本`.
2. Fetch the target document by the exact `资料编号` keyword with block IDs:

   ```bash
   lark-cli docs +fetch --api-version v2 --as user \
     --doc "<document_url>" --scope keyword \
     --keyword "<SLAV-material-id>" --context-before 1 \
     --detail with-ids --format json
   ```

3. If one matching entry has the same version, treat the operation as already
   synced and do not insert a duplicate. The block immediately before the
   marker paragraph must be the entry's `h3`; use that `h3` ID as the receipt
   block ID.
4. If the same number has another version, or more than one match exists, stop
   for manual review. Never overwrite a possibly hand-edited public entry.
5. If no match exists, fetch the outline and find the unique exact `h2`
   category:

   ```bash
   lark-cli docs +fetch --api-version v2 --as user \
     --doc "<document_url>" --scope outline --max-depth 2 \
     --detail with-ids --format json
   ```

   If the category is missing, append it once using the `revision_id` returned
   by that fetch, then refetch the outline:

   ```bash
   lark-cli docs +update --api-version v2 --as user \
     --doc "<document_url>" --command append \
     --revision-id "<latest_revision_id>" \
     --content '<h2>开源项目</h2>' --format json
   ```

   Replace `开源项目` only with the exact mapped category from the table.
6. From the fresh outline, take the unique category `h2` block ID and current
   revision. Insert the complete generated XML after that heading. Run from the
   video project root so the `@file` path stays relative:

   ```bash
   lark-cli docs +update --api-version v2 --as user \
     --doc "<document_url>" --command block_insert_after \
     --block-id "<category_h2_block_id>" \
     --revision-id "<latest_revision_id>" \
     --content @.source-led-ai-video/share-doc/entries/<entry-file>.xml \
     --format json
   ```

7. Repeat the keyword fetch from step 2. Continue only when exactly one match
   exists, its `内容版本` is unchanged, and the immediately preceding block is
   the expected entry `h3`. Save that `h3` block ID.
8. Record the receipt:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/share_doc.py" mark-synced \
  --project "/absolute/path/to/video-project" \
  --publication-id "sample-002-douyin+xhs" \
  --video-sha256 "<same published video sha256>" \
  --document-id "<Feishu document id>" \
  --block-id "<inserted top block id>"
```

Again, pass `--build-key` only when the include decision used one. Inspect
local state with:

```bash
python3 "$VIDEO_SKILL_ROOT/scripts/share_doc.py" status \
  --project "/absolute/path/to/video-project"
```

Feishu authentication and public-link permissions are external state. Create
or edit with `--as user`; do not use bot-owned documents for the fan-facing
library. Keep a new document private until its content has been checked. Public
sharing permission requires a separate explicit user decision and a final
anonymous-access test.

## Hard boundaries

- Do not add share-document fields to `project.json`.
- Do not call this module from `workflow run`, `resume`, `finalize`, or upload.
- Do not copy share-document state into `review-runs/` or `deliverables/`.
- Do not use analytics data as an automatic include/skip rule.
- Do not put credentials, private URLs, access tokens, or secret-bearing text
  into public entry JSON. The validator blocks common secret patterns, but the
  Agent must still inspect the final XML.
- Add `.source-led-ai-video/` to the video project's `.gitignore` whenever that
  project is version-controlled. Decision logs can contain complete public
  prompts and source URLs even though Feishu identifiers are fingerprinted.
- Do not claim that Feishu sync succeeded until the post-write keyword fetch
  confirms exactly one matching entry and `mark-synced` records the receipt.
