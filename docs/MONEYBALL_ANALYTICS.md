# Moneyball Analytics

Moneyball Analytics is an additive strategy layer for `aibrief_jp` Reel
insights. It keeps the existing HTML, Markdown, and JSON analytics intact and
adds age-matched analysis of follower conversion, intent, attention, and
production efficiency. Instagram and Facebook are independent analytical
lanes: each keeps its own published media ID, publication clock, observations,
denominators, and coverage. Missing source data stays unavailable; the report
does not turn an unavailable metric into zero or estimate it from a proxy.

## Repository ownership

The repository handoff is easy to miss:

- `/Users/aiagent/GitHub/reel-app` creates the local Reel assets and generation
  metadata.
- `/Users/aiagent/GitHub/carousel-app` owns the queue, Instagram and Facebook
  publishing, insight synchronization, the separate `state/reels.db` and
  `state/facebook.db` ledgers, and analytics reports.

Run the commands in this guide from
`/Users/aiagent/GitHub/carousel-app`. Moneyball may reuse reliable generation
metadata referenced by a ledger row, but it does not move insight ownership
back into `reel-app`.

## Sync metrics and preserve observations

Fetch current lifetime totals from Instagram with the existing command:

```sh
uv run python reel_scheduler.py sync-insights \
  --platform instagram \
  --channel aibrief_jp
```

On Instagram, the same command also collects account-level follower analytics
once per channel:

- current `followers_count` and `media_count`;
- `follows_and_unfollows` for explicit, non-overlapping completed UTC days; and
- account-level `reach` for each identical daily interval.

The first run fills missing days up to Meta's 90-day account-insights window.
Later runs preserve older days and re-fetch the most recent three completed
days because Meta can revise delayed values. Control this behavior explicitly:

```sh
# Force all available daily intervals to be fetched again.
uv run python reel_scheduler.py sync-insights \
  --platform instagram \
  --channel aibrief_jp \
  --account-follow-backfill-days 90 \
  --account-follow-refetch-days 90

# Sync only Reel metrics for an operational exception.
uv run python reel_scheduler.py sync-insights \
  --platform instagram \
  --channel aibrief_jp \
  --no-account-insights
```

Backfill and re-fetch values must be between `0` and `90`. Graph credentials,
paging URLs, and cursors are sanitized before account payloads are logged or
stored.

Facebook-native Reels must be synchronized separately. Facebook is no longer
treated as an Instagram crosspost total:

```sh
uv run python reel_scheduler.py sync-insights \
  --platform facebook \
  --channel aibrief_jp
```

The platform flag selects `state/facebook.db` automatically. That ledger
contains the Facebook Video ID and Facebook `published_at`; it does not reuse
the Instagram media ID or Instagram publication time. A matching
`content_hash` can link the two platform records for navigation and content
metadata, but it never fuses their performance metrics.

The `insights` table in `state/reels.db` is the historical Instagram Reel
source. The equivalent append-only observations for Facebook live in the
`insights` table in `state/facebook.db`.
`account_insight_snapshots` stores follower-stock observations and
`account_follow_flows` stores fetched revisions of daily account flows. Each
successful fetch appends an observation; a later fetch does not replace an
older observation. Exact duplicate observations are idempotent, while a later
revision of the same day is retained. Reports use the newest fetched revision
for each exact interval. The account-level follower tables are Instagram
sources and are not attributed to Facebook posts.

Instagram returns cumulative values at fetch time. A lifetime total fetched
when a post is seven days old is not evidence of its two-hour performance.
Moneyball therefore selects fixed maturity windows only when a real snapshot
exists:

- at or after the configured target age;
- no later than the target plus its configured tolerance; and
- nearest to the target when more than one valid observation exists.

The report retains the actual observation age. It does not interpolate between
snapshots, subtract later totals to invent earlier totals, or compare posts
from different maturity windows in one leaderboard. It also never age-matches
a Facebook observation against the Instagram publication clock, or vice
versa. A missing window is reported as unavailable. New snapshot collection
improves future reports but cannot reconstruct historical windows that were
never captured.

Window targets and tolerances live in
[`config/moneyball_analytics.json`](../config/moneyball_analytics.json).

The prospective `aibrief_jp` checkpoint recorder also syncs each platform by
its exact native media ID. It records +1-hour, +3-hour, +24-hour, +72-hour, and
now +7-day observations; the +7-day acceptance window is 168–192 hours:

```sh
UV_CACHE_DIR=state/uv-cache uv run --frozen python \
  scripts/run_aibrief_jp_reel_checkpoints.py --report-version 2
```

Adding the +7-day checkpoint only improves future coverage. It cannot recreate
a historical seven-day observation from a newer lifetime total.

## Add durable Reel annotations

Edit [`data/reel_annotations.json`](../data/reel_annotations.json). The active
file intentionally starts empty. Copy the shape of
[`data/reel_annotations.example.json`](../data/reel_annotations.example.json),
but do not copy its placeholder IDs into the active file.

Use the Instagram `media_id` as the preferred identity for an
Instagram-specific annotation. `content_hash` is an acceptable fallback for
an unpublished, pre-publication, or cross-platform content annotation. At
least one stable identity must be present, and `account` should normally be
`aibrief_jp`. When an Instagram and Facebook row share the same
`content_hash`, the report may reuse that content annotation while preserving
the separate platform identities and insight clocks.

Any field explicitly present in a manual annotation takes precedence over
generation-pipeline and inferred metadata, including an explicit `null` used
to block a weaker classification. Generation metadata may fill fields that
the annotation omits. Weak caption-based classification, when enabled, may
only fill a remaining gap and must be labeled:

```json
{
  "metadata_source": "inferred",
  "metadata_confidence": "low"
}
```

Normal operator entries should use `"metadata_source": "manual"`. A reliable
field copied directly from Reel generation is labeled
`"metadata_source": "generation_pipeline"` by the report. Report runs read but
never rewrite the annotation file.

Allowed initial `content_goal` values are:

- `discovery`
- `utility`
- `authority`
- `retention`

Use a stable, machine-friendly series name such as
`one_minute_ai_workflows`; do not change the spelling between posts. Series
recommendations require at least five comparable posts, so consistent tags
matter.

### Record production effort

Enter `production_minutes` only from an actual timer, production log, or known
workflow duration. It is the total attributable production time used by
per-production-hour metrics. `manual_effort_minutes` can record the measured
human hands-on subset. `direct_cost_jpy` is an exact post-attributed cash cost.

Do not backfill any of these from video duration, file timestamps, visual
polish, or a guessed template average. Leave an unknown value absent or
`null`. A real zero direct cost is valid; zero production minutes is not a
valid efficiency denominator.

### Tag a controlled experiment

Both control and variant posts need the same `experiment_id`, distinct
`experiment_variant` values, one identical `changed_variable`, and the
prewritten `hypothesis`. A comparison is eligible for causal-style language
only when:

1. the experiment ID matches;
2. exactly one declared variable changed;
3. observations come from the same maturity window; and
4. other known differences are displayed.

Multiple changed variables make the comparison observational. The report does
not claim statistical significance unless a justified statistical test is
added separately.

## Generate the Moneyball reports

After syncing insights and updating annotations, run:

```sh
uv run python scripts/run_moneyball_analytics.py --channel aibrief_jp
```

The command writes:

- `out/moneyball_data_audit.md`
- `out/reel_report.moneyball.html`
- `out/reel_report.moneyball.md`
- `out/reel_report.moneyball.json`
- `out/reel_report.moneyball.csv`
- `out/reel_report.moneyball.facebook.csv`

The HTML file is the visual Moneyball UI. It is generated programmatically
from the same canonical report object as JSON, Markdown, and CSV. Its KPI
cards, follower-flow chart, maturity coverage, intent-versus-reach plot,
funnel diagnostics, Instagram evidence tables, and Facebook-native evidence
table use self-contained HTML/CSS/inline SVG and a small inline table-sorter;
there are no external chart dependencies or separately maintained values.
The standard command reads `state/reels.db` and, when it exists,
`state/facebook.db`. The JSON keeps the Facebook lane under
`platform_analytics.facebook`; the Facebook CSV is flat and platform-specific.
The original CSV remains Instagram-compatible.

The existing `out/reel_report.*` and dedicated
`out/aibrief_jp_reel_report.*` artifacts continue to use their existing
commands and behavior.

## Configure analytical rules

[`config/moneyball_analytics.json`](../config/moneyball_analytics.json) is the
single inspectable source for:

- fixed-window targets and maximum time after each target;
- duration bucket boundaries;
- percentile and minimum-sample requirements;
- Hidden Gem, Vanity Winner, Workhorse, Expensive Star, and Underperformer
  thresholds;
- cohort-relative funnel diagnostics; and
- SCALE, HOLD, REVISE, PAUSE, and INSUFFICIENT DATA series decisions.

The defaults use medians and quartiles, show `n`, and require production-time
coverage before assigning production-efficiency labels. Change thresholds in
configuration, not in report prose. A threshold change alters future report
classification; it does not modify publishing, prompts, channel voice, or
ranking weights.

## Facebook-native analytics and Graph API v25

Facebook publishing now produces an independent Video ID, so the Facebook
lane is not an Instagram crosspost roll-up. Its current Graph API v25 fallback
reads only fields verified with the configured Page access token:

- Video `views`;
- Video `likes.limit(0).summary(true)`;
- Video `comments.limit(0).summary(true)`;
- Video `post_id`, used to resolve the associated Page Post; and
- Page Post `shares.count`, but only when Graph actually returns the `shares`
  object.

If `shares` is omitted, it remains unavailable. Omission is not evidence of
zero shares. The dashboard can calculate transparent likes, comments, shares,
and total known engagement per 1,000 views from available components. These
are explicitly **view-denominator** rates. They are not labeled as reach rates
and are never ranked beside Instagram reach-denominator rates.

Meta also documents richer metrics on the Facebook
`/{video-id}/video_insights` edge, including Reel play/replay totals, unique
media viewers, average watch time, total view time, Reel-attributed follows,
reaction breakdowns, retention graphs, and social actions. Examples of the
documented source names include:

- `blue_reels_play_count`
- `fb_reels_replay_count`
- `fb_reels_total_plays`
- `post_total_media_view_unique`
- `post_video_avg_time_watched`
- `post_video_followers`
- `post_video_likes_by_reaction_type`
- `post_video_retention_graph`
- `post_video_social_actions`
- `post_video_view_time`

See Meta's [Video Insights edge
reference](https://developers.facebook.com/docs/graph-api/reference/video/video_insights/)
and [Graph API v25 Insights
reference](https://developers.facebook.com/docs/graph-api/reference/v25.0/insights/).

That edge requires `read_insights` together with the applicable Page
permissions (including Page engagement access such as
`pages_read_engagement` or `pages_manage_engagement`, depending on the edge).
A July 2026 recheck of Meta's current v25 Video Insights reference and
permissions catalog confirms that the **permission itself is not deprecated**.
Meta has deprecated or renamed some individual Page-insight metrics; that is a
separate migration from the `read_insights` permission.
A live v25 capability probe with the current Page token does not have
`read_insights`, so the pipeline uses the verified direct-object fallback
above. Documented metric names are not fabricated into stored observations
when the token cannot retrieve them. Every sync probes one stable rich metric
once per channel; when authorization succeeds, the standard request activates
all documented rich fields and isolates any media-specific metric failures.
Version-dependent or denied fields remain unavailable.

With the current Page token, the Facebook source does **not** provide verified Reel-level saves,
post-attributed profile visits, returning viewers, a direct first-three-second
skip rate, post-attributed follows, or an exact follower/non-follower reach
split. The direct fallback also does not currently provide verified reach or
watch-time values. Consequently:

- Facebook `follows / reach`, `likes / reach`, `shares / reach`, and
  `saves / reach` stay unavailable;
- Facebook follow-conversion, curiosity, retention, watch-depth, and
  production-efficiency claims that depend on those missing fields stay
  unavailable;
- no Instagram follower flow is assigned to a Facebook Reel; and
- no Hidden Gem, Vanity Winner, or Workhorse label is promoted merely from
  Facebook views.

The Facebook table still exposes the same canonical columns so coverage gaps
are visible. As Page permissions or verified metrics improve, those columns
populate without changing the platform separation or inventing historical
fixed-window values.

## Current Instagram data limitations

The canonical source names must remain visible because similarly named metrics
do not always mean the same thing:

- `views` and `total_views` are not silently treated as a verified `plays`
  metric.
- `reach` is the preferred rate denominator. A views-based fallback, where
  explicitly allowed, is labeled and never mixed into a reach-based ranking.
- `shares` is the Instagram Graph metric named `shares`. The current source
  does not establish that it is equivalent to private sends, so Moneyball does
  not create or double-count a separate `sends` value.
- `reposts` is a separate Reel metric in Graph API v25. It is not relabeled as
  a share and is not part of Meta's documented `total_interactions`
  definition.
- `total_interactions` is retained as Meta's net aggregate. The dashboard
  exposes the raw count and the transparent
  `total_interactions / reach` rate; it never hides it inside a weighted score.
- `reels_skip_rate` is already a percentage: the share of initial views that
  skipped within the first three seconds. It is displayed directly and is not
  multiplied by 100 or converted into an inferred skip count.
- Per-post non-follower reach, follower reach, profile visits, returning
  viewers, and DM keyword hits are not present in the current Instagram
  insight feed. They remain unavailable unless a verified post-attributed
  source is added.
- Reel watch time and first-three-second skip rate are supported. Legacy replay
  columns remain only for backward compatibility: `plays`,
  `clips_replays_count`, and the older aggregated-play metrics are deprecated
  on current API versions and are not requested.
- Graph API v25 exposes the account's point-in-time `followers_count` and the
  account-level `follows_and_unfollows` flow. Moneyball stores these separately
  and can report gross follows, unfollows, net growth, and rates against
  account-level reach for identical daily intervals. The sync also stores
  account-day REEL-filtered reach, views, likes, comments, saves, shares, and
  total interactions, plus the REEL follower/non-follower reach breakdown.
- Graph API v25 does not expose media-level `follows` for the `REELS` media
  product type. Post-attributed follows must not be substituted with account
  follower-count movement. Account growth between two dates can be influenced
  by several posts, delayed discovery, profile activity, and external events.
  Publication counts shown beside a daily flow are context, never attribution.
- Account-flow intervals ending less than 48 hours before report generation
  are labeled preliminary. Current follower stock has no API-provided history,
  so its historical series begins when this pipeline starts snapshotting it.
- REEL-filtered account-day reach includes all Reels viewed during that day,
  including older Reels. It is not the summed reach of Reels published that
  day, and follows divided by this value is an observational account-day ratio,
  not post attribution. Meta's estimated reach breakdown buckets are displayed
  as returned and are never forced to sum to the reported total.
- Watch-time units and metric provenance are normalized only when their source
  semantics are verified. An average derived from total watch time requires a
  verified plays denominator.
- Older rows may contain fewer metrics than recent rows because Meta metric
  availability and the resilient request set have changed. The data audit
  reports coverage and raw source names rather than filling those gaps.

## Dashboard evidence tables

`out/reel_report.moneyball.html` contains two Instagram tables and, when
Facebook-native data exists, a third platform-specific table:

- **Instagram per-Reel evidence** links directly to every Reel and shows its
  latest observed age, content metadata, Instagram reach and views,
  cross-surface views, view frequency, interactions/reach, separate
  likes/comments/shares/reposts/saves rates, intent actions, total and average
  watch time, Reel duration, watch depth, three-second skip rate, production
  efficiency, and classifications. Missing post-attributed follows remain
  visibly unavailable.
- **Publication-day account context** groups the Reels published during each
  exact UTC account-insight interval and shows gross follows, unfollows, net
  growth, account reach, and follows per 1,000 account reach for that interval.
  This is timing context, not attribution: older Reels and non-Reel activity
  can contribute to both the reach and follower flow.
- **Facebook-native per-Reel evidence** links to the Facebook Reel, shows the
  paired Instagram link when a shared content hash exists, and reports the
  Facebook publication age, views, available engagement counts, and explicitly
  labeled view-denominator rates. Reach, saves, follows, watch-time, skip-rate,
  profile-visit, and returning-viewer columns remain visibly unavailable until
  Graph returns verified values.

Each table uses the latest lifetime observation per native Reel and prints its
actual platform-specific age. Fixed 2-hour, 24-hour, 72-hour, and 7-day
leaderboards remain separate by both platform and maturity window, so
different ages and platform denominators are never mixed.

## Fixed-window Top 10 rankings

The dashboard also renders five linked Instagram Top 10 lists from the
configured `performance_rankings.maturity_window` (currently `24h`):

- `total_interactions / reach` — higher is stronger;
- watch depth — higher is stronger and remains uncapped;
- direct `reels_skip_rate` — lower is stronger;
- saves per 1,000 reached accounts — higher is stronger; and
- views per reached account — higher is stronger.

The aggregate Top 10 requires all five metrics. It is the unweighted mean of
the five directional percentiles within the same complete-case maturity
cohort—not an opaque engagement score. Every result exposes its component
value, leaderboard rank, cohort percentile, numerator/denominator provenance,
and direct Reel link. “Strong point” labels require at least the configured
directional percentile threshold (currently P75).

The per-metric lists retain their full valid coverage, while aggregate
percentiles are recomputed over the same complete-case cohort for all five
components. View-denominator fallbacks never enter a reach-based list.
Facebook receives the same machine-readable ranking structure, but its lists
remain explicitly `INSUFFICIENT_DATA` until all requested source fields exist
inside one Facebook maturity cohort.

## Measured winner hook and script library

Every Moneyball run also generates:

- `out/reel_report.moneyball.winner_library.md`
- `out/reel_report.moneyball.winner_library.json`

The library takes the union of all five current Instagram metric Top 10s and
the aggregate Top 10, then joins each media ID back to the canonical post and
generation artifacts. Each post retains its direct Reel link, exact published
caption hook, complete Japanese rendered subtitle script, original-language
source transcript, source video, ranking memberships, actual observation age,
raw numerators and denominators, and provenance warnings. A post appearing in
several lists is written once with all of its placements.

The published caption's first non-empty line takes precedence over mutable
`one_liners.json` options. Japanese `subtitles.ja.ass` is the primary script
source, and `notes.json.transcript` is retained separately as the
original-language reference. A stale clip path may be recovered only when
there is exactly one artifact with the same source-video clip index; such a
match is marked medium confidence rather than silently treated as exact.

“Measured winner” means fixed-window leaderboard membership in that report
snapshot. It does not mean the hook caused performance, that the result is
repeatable, or that the post converted followers. The library groups the five
metrics into two correlated evidence families:

- intent/action: interactions/reach and saves/1,000 reach; and
- attention/replay: watch depth, lower three-second skip, and views/reached.

Aggregate membership is a summary, not a sixth independent vote. The Markdown
includes a candidate-comparison protocol that requires an intended hypothesis
lane, three linked same-maturity analogues from distinct sources where
possible, duration and source-saturation checks, and a transparent decision
label. It does not change generation prompts, publishing behavior, or ranking
weights.

To rebuild only this library from an existing Moneyball JSON:

```sh
uv run --frozen python scripts/build_verified_winner_library.py
```

To apply the library to new `reel-app` `candidates.json` batches, use
`scripts/evaluate_reel_candidates.py`. The evaluator writes a linked Markdown
review and machine-readable JSON without modifying the source candidates or
publishing queue. Candidate mode now uses a two-pass semantic LLM review:
analogue selection happens before ranks and values are visible, then a verifier
interprets exact 24-hour evidence only for the locked analogue IDs. This mode
transmits unpublished candidate transcripts and internal winner evidence to the
configured Gemini API account and therefore requires approval for that data
transfer. See
[`REEL_CANDIDATE_EVALUATION.md`](REEL_CANDIDATE_EVALUATION.md).

Every column heading in the evidence tables is a sort button. Select a heading
once for ascending order and again for descending order; the active direction
is shown in the heading and exposed through `aria-sort`. Sorting uses
unformatted machine values embedded by the report renderer, keeps equal values
in a stable order, and always leaves unavailable values at the bottom. It only
changes the browser presentation—the canonical JSON/CSV and the deterministic
default order remain unchanged.

These gaps directly limit follower-conversion, curiosity, retention, and
production-efficiency conclusions. The highest-value fields to start recording
immediately are measured `production_minutes`, stable `series`,
`content_goal`, and complete experiment metadata. Fixed-age snapshot coverage
must be collected prospectively.
