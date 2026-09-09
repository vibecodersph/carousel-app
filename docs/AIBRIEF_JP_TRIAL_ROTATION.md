# AI Brief JP fixed Trial Reel rotation

The operational candidate source is
`config/aibrief_jp_trial_rotation.json`. Its enabled entries are the complete
pool. Analytics reports, ordinary scheduled Reels, evidence tiers, and legacy
selector memory cannot add candidates. Change the pool only by explicitly
editing that file.

## Rotation rules

- Schedule one additive Instagram Trial Reel per day at 19:00 Asia/Tokyo.
- Walk enabled pool entries in configured order and continue from the last
  launched pool parent.
- Temporarily skip an entry when its parent is not published, lacks a media ID
  or render asset, its source family overlaps another Trial's half-open 72-hour
  observation window, the daily Trial slot is occupied, or the slot is less
  than 12 hours away.
- A skipped entry remains in the pool and becomes available on a later pass.
  The Boris Cursor/Anthropic entry is pinned to the July 23 refreshed render
  (`cf6d2db6…`) of the exact Lenny interview segment: Boris left Anthropic,
  joined Cursor, and returned two weeks later. Newer published Boris clips are
  not substitutes unless their transcript covers that career move. This render
  is currently publication-ineligible and must not be represented as a
  published parent until the ledger has a real publication and media ID.
- A published Trial Reel may be reused only when its exact pool entry sets
  `allow_trial_parent: true`. In that case, use the Trial's own content hash,
  media, hook, and caption; do not collapse it to the Trial's original parent.
- Reusing a published parent is allowed only with
  `trial-add-from-published --rotation-pool config/aibrief_jp_trial_rotation.json`.
  Output media hashes and experiment IDs must still be new.

## Variant rules

- Author a new Japanese opening hook grounded in the selected clip's notes or
  transcript.
- Preserve each entry's `required_hook_phrases` verbatim in every opening hook
  variant. For 「中国製AIは天安門事件に回答、なのに米国AIは翻訳すら拒絶する皮肉」
  (`37868f7e…`, rotation position 18), always keep the exact phrase 「中国製AI」.
  The selector includes these constraints in its recommendation; batch rendering
  and additive Trial scheduling reject hooks that omit a required phrase.
- Rerender the hook overlay while preserving body footage, Japanese subtitles,
  decoded audio, and the parent's exact caption.
- Require 720x1280 output, decoded-audio equivalence, central-video similarity,
  and visual review of the opening and midpoint before scheduling.
- Store the experiment as `successful_post_variant` with a non-empty pool
  parent. Never create a `scheduled_conversion`, displace an ordinary queued
  Reel, or mirror an additive Trial to Facebook.

## Operational commands

The selector is read-only and writes the next recommendation to
`out/trial_candidates/latest.json` and `.md`:

```sh
python scripts/select_aibrief_jp_trial_candidates.py \
  --channel aibrief_jp \
  --db state/reels.db \
  --config config/aibrief_jp_trial_rotation.json
```

Bulk retirement is also dry-run-first:

```sh
python reel_scheduler.py trial-retire-unpublished \
  --channel aibrief_jp \
  --db state/reels.db
```

The recurring Codex planner runs daily at 06:30 JST and uses this pool as its
sole authority. It must back up both platform ledgers before its first mutation
and verify that regular queues and Facebook Trial isolation remain intact.
