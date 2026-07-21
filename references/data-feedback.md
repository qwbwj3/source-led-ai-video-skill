# Data feedback contract

The feedback layer measures production cost and post-publication performance.
It never changes source claims, script approval, media hashes, or Build Keys.
Events are appended to `.source-led-ai-video/analytics/events.jsonl`.

## Model usage

Record counts supplied by the host after each meaningful reasoning stage. Use
`null` when the host does not expose a count; do not estimate and label it as
measured.

```bash
python3 scripts/analytics.py model-usage \
  --project /absolute/project \
  --model "gpt-5.6-terra-high" --stage "source-understanding" \
  --input-tokens 12000 --output-tokens 2600 --processed-tokens 14600
```

The three token fields accept non-negative integers or the literal `null`.
Processed tokens cannot be lower than known input plus output.

## Platform snapshots

Record the same video at `2h`, `24h`, `72h`, and `7d` when data is available.
Supported fields are views, 2s/5s retention, average watch seconds, completion
rate, likes, comments, shares, favorites, follows, cover impressions, cover
clicks, and cover CTR.

```bash
python3 scripts/analytics.py platform-snapshot \
  --project /absolute/project \
  --publication-id "douyin-publish-001" \
  --build-key "0123456789abcdefabcd" \
  --video-sha256 "<64-character sha256>" \
  --cover-variant 3x4 --window 24h \
  --views 18200 --retention-2s 0.71 --retention-5s 0.48 \
  --average-watch-seconds 31.4 --completion-rate 0.22 \
  --favorites 730 --shares 210 \
  --cover-impressions 24000 --cover-clicks 18200 \
  --cover-ctr 0.7583333333
```

Counts are integers. Rates use `0..1`. If CTR is supplied, it must equal clicks
divided by positive impressions. Omitted metrics are written as explicit
`null`, so unavailable never becomes zero. Publication ID, build key, video
hash, and chosen cover variant bind the snapshot to the exact published
artifact; do not reuse an ID after replacing a video or cover.

## Creator feedback

Capture a short production observation plus the next testable decision:

```bash
python3 scripts/analytics.py creator-feedback \
  --project /absolute/project \
  --build-key "0123456789abcdefabcd" \
  --feedback "观众在工具链卡片后留存下降，封面点击正常" \
  --decision "下一条把卡片从9秒压到6秒"
```

Keep feedback about the artifact and test. Do not record URLs, handles, email
addresses, phone numbers, identity/account numbers, credentials, tokens, or
secret-like strings. Numeric inputs reject NaN and infinity.

## Review questions

After at least 10 comparable posts, examine medians and ranges instead of one
viral outlier:

- Did concrete-result hooks improve 2s and 5s retention?
- Did a front-loaded toolchain raise favorites without hurting completion?
- Which source visual formats preserved average watch time?
- Did cover wording improve CTR while staying credible?
- Which QA failures and production stages consumed the most elapsed time?

Turn a repeated finding into a candidate rule first. Promote it into the Skill
only after the pattern survives several comparable projects and a human review.

Platform data may inform the separate post-publication Feishu-library decision,
but it never triggers inclusion or exclusion by itself. The user explicitly
chooses `include` or `skip` for each published artifact; see `share-doc.md`.
