# AI Brief JP three-day Moneyball content pulse

This is the durable runbook for the Codex Scheduled task:

- **Name:** `AI Brief JP — Three-day Moneyball Pulse`
- **Schedule:** every 72 hours in the user's Asia/Tokyo locale
- **RRULE:** `RRULE:FREQ=HOURLY;INTERVAL=72`
- **Project:** `/Users/aiagent/GitHub/carousel-app`
- **Execution:** standalone task in the local project, not an isolated worktree

The hourly interval is anchored to the task's creation time. Create or enable
it shortly after a successful snapshot checkpoint so future runs stay aligned
to fresh data. Codex Scheduled tasks do not attach a timezone field to this
RRULE; the desktop app uses the user's local timezone.

The cadence is a tactical learning loop. It is not permission to publish,
reschedule, edit, delete, render, queue, graduate, pause, or repost content.

## Why every three days

`aibrief_jp` normally publishes four Reels per day, so one pulse should add
roughly 12 newly mature 24-hour observations. That is enough to identify
directional editorial signals without reviewing every daily fluctuation.

Three days is too short for formal strategy changes. The report may suggest
tests, but it must not declare a winning series, Workhorse, growth winner, or
production-efficient format while post-attributed follows and production time
remain unavailable.

## Scheduled task procedure

### 1. Regenerate the canonical analytics

From the project root, run exactly:

```sh
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_moneyball_analytics.py --channel aibrief_jp
```

Before running it, check `state/reel_scheduler.lock`. If the lock exists, wait
in short intervals for at most 60 seconds. If it is still present, write a
`DEFERRED — SNAPSHOT CHECKPOINT ACTIVE` task result and stop without replacing
either report path. Do not remove the lock.

The existing checkpoint automation is responsible for collecting snapshots.
This pulse reads those ledgers and regenerates the canonical Moneyball
dashboard, JSON, Markdown, CSV, and audit. It must not call `run-due`,
`queue-outputs`, a publisher, a renderer, `reflow-queue`, or another mutating
content command.

The same command also generates `out/reel_report.moneyball.records.md` and
`.records.json`, embeds account records in the canonical dashboard/JSON, and
preserves `state/moneyball_records/aibrief_jp.history.json`. Keep that history
file across runs: it retains earlier record holders, former Top-10 members,
and their hooks/scripts even after they leave the current winner library.
Do not delete or reset it to make a run pass. A missing history establishes a
baseline; it does not establish when existing historical records were broken.

If the Moneyball command fails, report the error and stop. Do not analyze a
partially regenerated bundle.

### 2. Check readiness

When `/Users/aiagent/GitHub/reel-app/outputs` contains candidate files, generate
the read-only review of the three newest reconciled batches:

```sh
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/evaluate_reel_candidates.py \
  --analysis-mode diagnostic \
  --latest-from /Users/aiagent/GitHub/reel-app/outputs \
  --latest-count 3 \
  --markdown-out out/reel_candidate_evaluation.latest.md \
  --json-out out/reel_candidate_evaluation.latest.json
```

This recurring pulse remains on the local diagnostic mode until transmission
of unpublished candidate transcripts and internal winner analytics to the
configured Gemini API account is explicitly approved for recurring runs. The
current one-time three-folder approval does not authorize recurring sends. It
selects pipeline outputs by file modification time, not source-video
publication date. Preserve valid empty `clips` arrays in the report and never
recover rejected intermediate candidates automatically. A
candidate-evaluation failure must be disclosed, but it must not invalidate an
otherwise complete analytics refresh.

Read:

- `out/reel_report.moneyball.json`
- `out/reel_report.moneyball.winner_library.md`
- `out/reel_report.moneyball.winner_library.json`
- `out/reel_report.moneyball.records.md`
- `out/reel_report.moneyball.records.json` (also embedded as `account_records`
  in the canonical JSON)
- `state/moneyball_records/aibrief_jp.history.json` for past champions and
  preserved winner examples
- `data/aibrief_jp_manual_follow_conversion.json`, the appendable user-supplied
  follow-conversion observations, and the generated `.manual_follows.md` / JSON
  companions. These are a separate leaderboard, not API or fixed-window data.
- `out/reel_candidate_evaluation.latest.md`, when generated
- `out/reel_candidate_evaluation.latest.json`, when generated
- `out/reel_report.moneyball.content_analysis.md` as a structure and writing
  reference only
- the most recent report under
  `out/moneyball_content_analysis/aibrief_jp/`, when one exists

The five requested metrics are:

1. direct three-second skip rate, where lower is stronger
2. looping: `views / reach`, where higher is stronger
3. save rate: `saves / reach`, displayed as a percentage
4. share ratio: `shares / views`, displayed as a percentage
5. raw reach, where higher is stronger

Only these five metrics determine standouts, individual Top 10s, combined
rankings, active fixed-window account records, and winner-library membership.
The combined ranking is the equal-weight mean of the five directional
percentiles. Watch depth, total interaction rate, absolute views, and manually
reported follows are supporting context, not combined-ranking inputs. Do not
substitute reach for the share-ratio denominator.

Use only fixed 24-hour observations for the fresh cohort and rolling rankings.
Never mix latest lifetime values or different maturity windows into those
comparisons.

Define the fresh publication cohort as the 72-hour interval ending 28 hours
before the report timestamp:

```text
(report time − 100 hours, report time − 28 hours]
```

This provides a complete three-day publication span while allowing the
configured 24–28-hour snapshot tolerance to elapse.

For the first pulse, use that interval directly. For later pulses, also state
whether any publication gap or overlap exists relative to the prior report.

Readiness gates:

- At least **8** fresh Reels must have valid 24-hour observations before
  producing fresh editorial conclusions.
- If fewer than 8 qualify, write a coverage-only report with status
  `INSUFFICIENT FRESH DATA`.
- Rolling metric Top 10s require at least **30** eligible 24-hour Reels.
- The aggregate Top 10 requires at least 30 complete Reels and at least 70%
  coverage across all five metrics.
- A rate below 100 reached accounts must be marked `LOW BASE`.
- A save or share claim based on fewer than five raw actions must be
  marked `LOW COUNT`.

Every rate claim must show the raw numerator and denominator beside the rate.

### 3. Analyze the fresh batch and rolling evidence

All arithmetic must come from the JSON, not from intuition or a language-model
estimate. Use a short read-only script or `jq` when necessary to calculate
counts, medians, percentiles, overlaps, and rank movement.

The report must contain:

1. **Data readiness**
   - report timestamp, fresh interval, eligible post count, expected count,
     fixed-window coverage, and important missing fields.
2. **What changed**
   - new rolling Top-10 entrants, exits, and material rank movement compared
     with the previous pulse.
3. **Fresh three-day cohort**
   - every qualifying Reel with a direct link, actual snapshot age, reach,
     skip rate, views, looping, raw saves, saves/reach, raw shares, and
     shares/views. Raw watch time and duration may be included as supporting
     context but must not influence selection or ranking.
4. **Fresh standouts**
   - at most three balanced posts and at most three specialists per metric.
     Do not call ten of an approximately twelve-post batch a Top 10.
   - For every standout, include its all-time standing for each metric used to
     justify the selection: rank / eligible historical count, directional
     percentile, linked record holder, gap to that record, and raw support.
     Use `account_records.fresh.posts` and the matching fixed-24h board; these
     counts differ from the rolling-28-day cohort. If record eligibility fails,
     show the measured rank separately with its LOW BASE/LOW COUNT flag and do
     not describe it as an eligible account record.
5. **Rolling 28-day rankings**
   - the five linked Top 10s and the transparent balanced aggregate Top 10.
     Keep lower-is-better direction for skip.
   - **All-time AIbrief account records**: include the generated recordbook's
     current 24h leaders for exactly the five requested metrics,
     measured/eligible coverage, and record changes since the prior run. Show
     both old/new holder links and values, absolute improvement, and relative
     improvement where defined. Use the generated event status exactly: a
     baseline, tie, late-discovered historical observation, or data revision is
     not a newly achieved record. Explicitly say when no new record is found.
     Include separate concise 72h and 7d leader summaries, and a separate
     descriptive lifetime reach table with snapshot ages and accompanying
     views as context. Do not create an absolute-views leaderboard or record.
     Never combine
     these windows or platforms in one ranking.
     The generated `events` list compares analytics refreshes, which may also
     happen between pulses. For the pulse's record callouts, filter the saved
     history's `record_events` by `detected_at` in
     `(previous completed pulse source as_of, current source as_of]` and
     deduplicate by `event_id`. Do not lose events from intervening refreshes.
     If no previous completed pulse exists, report baseline status and use
     only events after the saved history's baseline time. Missing prior-pulse
     timing must be disclosed rather than guessed.
     Filter pulse record events to the active `ranking_policy.signature`.
     When the metric policy changes, report `METHODOLOGY_BASELINE_ESTABLISHED`,
     preserve earlier metric records and winner examples in history, and do
     not describe a changed formula/cohort as a performance record. Rank
     movement and entrant/exit comparisons require the same metric policy.
6. **24h-to-72h follow-through**
   - use only posts with both real observations. Name the actual ages. Do not
     interpolate or reconstruct a missing window.
7. **Newly available 7-day evidence**
   - describe late distribution or decay, but do not rank it beside 24-hour
     results.
8. **Editorial interpretation**
   - inspect hooks and generation `notes.json` transcripts for the linked
     posts. Identify content architecture, source/speaker repetition, duration
     confounds, and plausible hypotheses.
9. **Fragile results**
   - name LOW COUNT, LOW BASE, source concentration, duration bias, and
     correlated metrics.
10. **Next twelve-post learning portfolio**
    - suggest tests, not automatic queue changes. Each suggestion must name its
      24-hour evidence, sample size, baseline, and confidence.
    - judge proposed hooks/scripts with the winner library protocol: declare the
      intended evidence lane, show three linked same-maturity analogues from at
      least two distinct source videos when possible, identify meaningful
      differences, check source/topic saturation, and use only the allowed
      decision labels.
    - Include relevant historical exemplars from the current library or the
      preserved winner archive as well as recent evidence. A former winner
      remains one observation; label its historical status and retain its
      original measured window, hook/script provenance, and caveats.
11. **Unsupported conclusions**
    - explicitly list what cannot be concluded from missing follows, profile
      visits, returning viewers, production time, series tags, or experiments.
12. **Manual follow-conversion leaderboard**
    - Show the supplied viewers, follows, computed `follows / viewers`, watch
      time when supplied, attribution scope, boost status, and observation
      date/identity status. Link each matched alias to its actual Reel and show
      the published title. Keep actual regular/trial distribution separate from
      the supplied attribution label; a trial Reel can have post-level insights.
      Do not equate manually labelled viewers with API reach or views.
    - Exact supplied counts support descriptive conversion rates. Approximate
      counts and bounds such as `~20,000`, `<9`, and `<0.045%` remain visibly
      approximate/bounded and unranked as exact observations. Missing values
      stay unavailable. Preserve supplied reported rounding alongside the
      recomputed exact-count percentage.
    - Append new manual observations; do not overwrite past snapshots. Resolve
      supplied title hints against the published ledger and save the actual
      media ID, permalink and match evidence. Disambiguate repeat uploads and
      trials using published titles, existing mappings and supporting snapshots;
      leave ambiguous identities unresolved. Never infer an observation date,
      boost status, or post maturity from an identity match.
      Manual follow counts must not flow into the automated combined rank,
      fixed-window records, or follower-attribution claims for other posts.

### 4. Evidence and language rules

- Use “associated with,” “consistent with,” or “candidate pattern.” Never say
  the hook, duration, topic, or algorithm caused the result.
- Call the aggregate a **balanced leading-indicator ranking**, not a growth or
  engagement score.
- Skip and looping are related attention/replay signals; the combined ranking
  does not establish independent causal confirmation. Save and share rates
  use different denominators and must retain their raw support.
- Watch time/depth may explain duration context but are not ranking criteria.
- State that views/reached may indicate replay or repeated delivery, not
  satisfaction.
- API media-level follows remain unavailable. Manual post-level observations
  may support only their explicitly supplied attribution scope and snapshot;
  do not generalize them to fixed windows or unverified organic conversion.
- Never call a format efficient: production time is unavailable.
- Never call a series proven with fewer than five tagged comparable posts.
- A pattern seen in one pulse is an anecdote. Require either two consecutive
  pulses or at least five comparable tagged posts before calling it a
  repeatability candidate.
- The same Reel appearing in several reports or remaining in a rolling Top 10
  is still one observation, not repeated evidence. Count newly eligible unique
  Reels and distinct comparable examples for each content family. Change an
  allocation recommendation only when several distinct Reels support it.
- Missing values remain unavailable, never zero.
- “All-time” means best among this account's tracked eligible observations,
  not every post ever published or a platform-wide/global record. Missing old
  checkpoints cannot be reconstructed. Lifetime totals are descriptive and
  favor older posts; absolute reach/views also reflect changing account size.
- The recordbook retains measured standings but requires at least 100 reach
  for skip/looping/save-rate record eligibility, plus at least five saves for
  save rate. Share-ratio records require at least 100 views and five shares.
  Thin samples are provisional references.
  These are screening rules, not proof of statistical significance. A small
  historical cohort remains weak evidence even when a post ranks first.
- Historical rank and percentile are relative to the current comparison
  cohort. Movement alone is not improvement in a post's measured performance.
- Winner history begins at the saved baseline. Do not claim it contains every
  post that held a winning rank before tracking began.
- Every Reel named in a ranking or recommendation must link to its permalink.
- Do not browse for creator folklore or universal Instagram benchmarks.

## Output contract

Create the archive directory when needed:

```text
out/moneyball_content_analysis/aibrief_jp/
```

Write:

```text
out/moneyball_content_analysis/aibrief_jp/YYYY-MM-DD.md
```

Use the report date in Asia/Tokyo. Do not overwrite a different prior run. If a
same-day file already exists, append `-HHMM` to the new filename.

After validating the report, update:

```text
out/reel_report.moneyball.content_analysis.md
```

The stable path is the latest pulse; the dated path preserves comparison
history.

Before replacing the stable path, verify:

- the source timestamp matches the regenerated Moneyball JSON;
- every claimed Reel URL occurs in the source JSON;
- all five rolling lists contain at most ten rows;
- the aggregate contains only complete five-metric observations;
- no `NaN`, `Infinity`, unlabeled fallback denominator, or unsupported causal
  statement appears; and
- the report states the fresh sample size and the no-follower-attribution
  limitation;
- the account-record source `as_of` matches the canonical JSON `as_of`, each
  historical rank/count/percentile comes from the same platform and window,
  and every selected fresh standout has historical context;
- record callouts agree with saved event status, cover the entire interval
  since the prior completed pulse (including intervening analytics refreshes),
  and retain both holder
  links, raw support, and the tracked-history limitation; and
- fixed24h, fixed72h, fixed7d, and lifetime tables remain visibly separate.

If validation fails, preserve the prior stable report and return the exact
failure.

## Strategic cadence

Use the three-day pulse to adjust hypotheses and the next learning batch. Use
7-day maturity evidence for slower allocation decisions. Because the
three-day schedule rotates through weekdays, do not interpret one pulse as a
weekly season-level verdict.
