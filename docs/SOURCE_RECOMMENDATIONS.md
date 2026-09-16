# Source recommendations that learn from reel outcomes

Every canonical Moneyball refresh now writes
`out/reel_report.moneyball.source_feedback.json` and `.md`. The existing
three-day Moneyball task runs this refresh, so priorities update as fixed-age
checkpoints arrive. No additional scheduler or external API is required.

## What is measured

- Exact YouTube source → published Instagram media ID → real 24h and 7d snapshot.
- Regular, Trial and unknown distribution modes remain separate. A Trial
  snapshot after graduation is excluded because its exposure mixes modes.
- Each reel is compared against earlier publications from the previous 28 days,
  excluding the same source, in the same duration bucket and distribution mode.
  Each metric needs at least ten baseline clips. No lifetime substitutions,
  future publications, guessed missing values or inferred duration fallbacks.
- The balanced evidence is the equal-weight mean of the five directional
  percentiles: lower skip, higher views/reach, saves/reach, shares/views and reach.
  This is retrospective evidence, not a new candidate quality score or prediction.
- Rates show raw support. Reach/views below 100 cannot qualify the balanced
  decision. Action counts below five remain flagged, and cannot establish a
  save/share recommendation hit or miss.
- Repeated releases of one clip count once within each mode: the earliest
  measured release is retained. Source priority requires three distinct
  comparable clips. Uploader/topic priority requires six across three sources.
  At least 70% of measured distinct clips must have a valid comparison, so a
  small eligible subset cannot determine the whole source's priority.
- Within the latest 90 publication days, `REDUCE` requires at least two thirds
  of comparable clips below balanced P40 **and** below their matched reach
  baseline. `PREFER` requires two thirds above P60 **and** above reach baseline.
  Otherwise evidence is mixed or insufficient. These are conservative screening
  thresholds, not statistical significance or causal source effects.
- Only 7d evidence changes source priority. The separate 24h board is an early
  diagnostic. Source-specific evidence takes precedence over uploader/topic
  priors; conflicting broader signals stay exploratory.
- A source video, its uploader, and its speaker are different identities.
  Speaker and topic labels are explicitly supplied with recommendations;
  unknown historical labels stay unknown. Source/uploader evidence can be
  backfilled without claiming that past recommendations were recorded.

Policy is versioned in `source_feedback.POLICY`. Changing the policy requires
regenerating the feedback; recommendation recording rejects other versions.
No follower conversion or production efficiency is inferred.

## Required workflow for new source recommendations

Before a new AI Brief JP source search or shortlist, read the current feedback
Markdown, inspect relevant sources **including REDUCE and MIXED rows**, and read
the prospective recommendation audit. Use source-specific clip evidence to
distinguish weak selection/hook/payoff from a weak source. Do not blacklist a
speaker based on one source video. Explicit user-selected production remains
governed by that user instruction; this workflow applies to recommendations.

1. Refresh Moneyball when feedback is more than 96 hours old. Check the scheduler
   lock as described in `ops/codex-aibrief-jp-moneyball-pulse.md`. The refresh
   reads local ledgers; it does not collect new external analytics.
2. Research source candidates using the user's source/date/credibility criteria.
   Prefer exact historical evidence and independently verify new source content.
   The feedback system ranks supplied candidates; it does not browse or invent
   new videos. Keep a larger candidate pool than the requested shortlist.
3. Write a local JSON input with a `candidates` array in editorial preference
   order. Every entry must have these fields:

   ```json
   {
     "source_url": "https://www.youtube.com/watch?v=VIDEO_ID_11",
     "title": "Exact observed title",
     "uploader": "Exact uploader name",
     "speaker": "Verified speaker name",
     "topic": "stable_topic_identifier",
     "distribution_mode": "regular",
     "rationale": "Specific mechanism, clip opportunity and why this source fits",
     "expected_metric": "save_rate"
   }
   ```

   `expected_metric` is one of `skip`, `looping`, `save_rate`, `share_rate`,
   `reach`; expectation means above the directional median of the matched
   baseline. Mode is `regular` or `trial`. Preserve existing topic identifiers
   for comparable topics instead of renaming them to evade negative evidence.
   Do not infer speaker identity from the uploader. For a reduced source, a
   bounded retest also needs a concrete `changed_hypothesis` explaining the new
   selection, hook, payoff, or context that addresses the earlier miss.
4. Record the shortlist **before presenting the recommendation or producing
   recommended clips**:

   ```sh
   UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/source_recommendations.py \
     recommend --input out/source_candidates.json \
     --batch-id YYYYMMDD-source-search-01 --count 5
   ```

   This consults the latest measured evidence, selects mature supported sources,
   reserves at most `ceil(20% × requested count)` slots (at least one) for
   exploration/retests, and writes the selected and unselected candidates with
   their evidence, rationale, expectation and timestamp. If evidence supports
   fewer sources, `PARTIAL_EVIDENCE_LIMITED` returns a smaller shortlist. Research
   additional sources; do not silently expand the exploration quota or present
   excluded candidates as recommended. This cap applies to each shortlist;
   don't create repeated batches to evade it.
5. Present the selected sources with the batch ID and link to
   `out/source_recommendations.latest.md`. Explain material priority changes
   and the specific evidence behind them. Existing candidate-review reports now
   append regular/Trial source context automatically; a strong creative score
   does not cancel repeated negative source outcomes.

## Durable accountability

`state/source_recommendations/<channel>/<batch-id>.json` is an immutable batch.
Its referenced, content-addressed feedback is preserved under `evidence/`.
Do not edit or delete these files to revise recommendations; record a new batch
with a new ID and rationale. Hash validation detects changed entries or dates.

Only selected recommendations can receive attribution. A published reel joins
its exact sealed production binding for newly recorded batches. These batches
set `exact_binding_required`; source/time coincidence cannot claim a release.
See `ops/codex-aibrief-jp-continuous-loop.md` for planning, QA, queuing and health
checks. For legacy batches without that flag, a published reel joins
the latest matching recommendation recorded **before** publication, for the
same exact source and mode, within 30 days. A release belongs to one batch.
Historical releases before tracking remain unattributed. Unpublished sources,
missing windows and immature recommendations remain explicit, never failures.

Subsequent Moneyball refreshes audit the expected metric as `SUPPORTED`, `MISS`,
`MIXED` or `INSUFFICIENT_EVIDENCE`, separately at 24h and 7d, and recompute source
priorities from all qualifying source outcomes. A new recommendation records
the previous recommendation's priority for comparison. No model scores are
retrospectively relabeled as forecasts, and source popularity is not a substitute
for our own results.

To regenerate just this feedback from an existing Moneyball JSON:

```sh
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/source_recommendations.py refresh
```

This is an analytics/recommendation loop. It does not change a source transcript,
render clips, publish, pause or reorder the content queue.
