# Bundled publish-precheck gate

This directory is a portable snapshot of the user-provided `yuwen-publish-precheck` standard. Its MIT license is included as `LICENSE`.

## Required sequence before TTS

1. Run `review_gate.py prepare --config <project.json>`. Review `review/review-content.md`.
2. Run `review_gate.py scan --config <project.json>`. This command derives platform, commercial status, and medical/finance scope from the same prepared configuration and writes the hash-bound `review/lexical-scan.json`; do not call the low-level scanner directly.
3. Read `judgment.md` and `rules-common.md` every time.
4. Read `rules-commercial.md` because unknown commercial status is treated as commercial.
5. Read `platform-douyin.md` for this workflow. Add medical or finance references only when the actual content enters those domains.
6. Apply `my-rules.md`, `profile.md`, and `expressions.md` as Hanye's standing content standard.
7. Write `review/publish-precheck.md` using the structured report contract below. The scope, lexical counts, and prepared payload hash must match the generated evidence exactly.
8. If anything changed, rerun preparation, lexical scan, semantic review, and include `复检：通过` in the final report.
9. Run `review_gate.py approve`. Only `pass` or `revised-pass` can create `review/approval.json`.

## Hard boundaries

- A lexical hit is only a review candidate; zero hits do not prove semantic safety.
- Preserve the hook, creator judgment, and information density while repairing genuine risks.
- Never use homophones, misspellings, split characters, emoji, masking, or coded language to evade review.
- Do not promise platform approval. “可以发” means no blocker was found in the checked scope.
- Do not begin any test or production TTS until the approval file exists and matches the exact narration, cover hook, platform, commercial flag, and scene order.

## Scope sentinel

`prepare`, `scan`, `approve`, and `verify` inspect the narration together with the cover hook before trusting `project.json` scope declarations. Clear medical or financial subject anchors require the matching `review_scope.industries` entry. Clear sales, lead-generation, paid-course, sponsorship, or purchase actions cannot use `commercial: false`. Fix `project.json` and rerun preparation and scan when the sentinel stops the workflow.

The sentinel deliberately ignores ordinary attribution such as “评论区告知来源” or “评论区放项目地址”. It is a narrow fail-closed guard for obvious contradictions, not an industry or commercial classifier and not a substitute for Agent judgment.

## Structured semantic report contract

Use these exact level-two section names once each. Every section must contain a real review result; do not copy this structure and mark it complete without reading the content, scan output, and applicable rules.

```text
# 抖音发布前语义复核

## 审核范围
- 平台：抖音
- 商业属性：有
- 强监管行业：无
- 审核内容：口播文案、封面钩子
- Review-Payload-SHA256: <review-input.json 中的值>

## 逐平台结论
- 抖音：可以发

结论：可以发

## 词面候选复核
- Lexical-Review: complete; candidates=0; personal_hits=0; myth_advisories=0; warnings=0
- 复核声明：已逐条复核全部词面候选；词面候选不等于违规结论，零候选不等于语义安全。

## 必改
- 无

## 建议改
- 无

## 仅提示
- 无

## 无法判定
- 无

## 发布前检查单
- AI生成内容标注：需要
- 虚构演绎标注：不适用
- 营销信息标注：需要
- 转载与来源标注：需要
- 事实证据：已确认
- 素材授权：需要（发布前确认）

## 边界声明
- 审核声明：已由 Agent 阅读完整口播、封面钩子、词面扫描结果和适用规则并完成语义判断；机器门禁只校验结构、范围一致性和证据绑定，不能证明语义判断本身正确。
- 边界声明：“可以发”仅表示本次检查范围内未发现阻断项，不承诺平台审核通过，也不替代事实、资质、版权和授权核验。
```

When individual dispositions are preferable, replace the complete lexical declaration with one line per scanner item in this form: `- Lexical-Disposition: candidates[0] | 必改 | <具体判断>` (also supports `personal_hits` and `myth_advisories`), plus `- Lexical-Warnings-Reviewed: <count>`. Every indexed item must be covered exactly once.

For `revised-pass`, use `- 抖音：改后可发`, a supported overall conclusion, at least one `已修复` entry under `必改`, and one standalone `复检：通过` line. An approval cannot retain unresolved items under `无法判定`.

`事实证据` must be `已确认` before approval. `素材授权` may be `已确认` or `需要（发布前确认）`; approval binds these as `rights_clearance: confirmed` or `pending`. Pending permits TTS, rendering, and private review, while `finalize` and bundle verification require confirmed. It deliberately keeps rights clearance as a visible release action when semantic script review happens before final assets exist and does not waive the check required before publication.

## Trust boundary

The machine gate proves that the report is hash-bound to the current content and policy, that declared scope agrees with obvious anchors, that lexical evidence was acknowledged, and that required sections are present and internally consistent. It cannot prove that the Agent read carefully, chose the right rule, assessed context correctly, or verified an external fact, qualification, right, or authorization. `reviewer=yuwen-publish-precheck` is therefore an attestation by the reviewing Agent, not an automated moderation verdict. Fabricating the attestation defeats the safety gate and is prohibited.
