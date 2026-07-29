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

If the Moneyball command fails, report the error and stop. Do not analyze a
partially regenerated bundle.

### 2. Check readiness

Read:

- `out/reel_report.moneyball.json`
- `out/reel_report.moneyball.content_analysis.md` as a structure and writing
  reference only
- the most recent report under
  `out/moneyball_content_analysis/aibrief_jp/`, when one exists

The five requested metrics are:

1. `total_interactions / reach`
2. watch depth
3. direct three-second skip rate, where lower is stronger
4. saves per 1,000 reach
5. views per reached account

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
- A save or interaction claim based on fewer than five raw actions must be
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
     raw interactions, interaction rate, average watch, duration, watch depth,
     skip rate, raw saves, saves/1,000 reach, views, and views/reached.
4. **Fresh standouts**
   - at most three balanced posts and at most three specialists per metric.
     Do not call ten of an approximately twelve-post batch a Top 10.
5. **Rolling 28-day rankings**
   - the five linked Top 10s and the transparent balanced aggregate Top 10.
     Keep lower-is-better direction for skip.
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
11. **Unsupported conclusions**
    - explicitly list what cannot be concluded from missing follows, profile
      visits, returning viewers, production time, series tags, or experiments.

### 4. Evidence and language rules

- Use “associated with,” “consistent with,” or “candidate pattern.” Never say
  the hook, duration, topic, or algorithm caused the result.
- Call the aggregate a **balanced leading-indicator ranking**, not a growth or
  engagement score.
- State that Meta `total_interactions` includes saves. Interaction rate and
  save rate are therefore not independent confirmation.
- State that three-second skip and watch depth are related attention signals.
- State that watch-depth comparisons remain duration-sensitive.
- State that views/reached may indicate replay or repeated delivery, not
  satisfaction.
- Never say a Reel converted followers: media-level follows are unavailable.
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
  limitation.

If validation fails, preserve the prior stable report and return the exact
failure.

## Strategic cadence

Use the three-day pulse to adjust hypotheses and the next learning batch. Use
7-day maturity evidence for slower allocation decisions. Because the
three-day schedule rotates through weekdays, do not interpret one pulse as a
weekly season-level verdict.
