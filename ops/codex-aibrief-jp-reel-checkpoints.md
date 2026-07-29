# AI Brief JP Reel checkpoint automation

This is a regular observation journal for `aibrief_jp`. It is not an A/B test
and does not attempt to prove which hook caused distribution.

For every newly published Japanese Reel, the default v2 runner records one
immutable Markdown analysis at each checkpoint:

- `01h.v2.md`: first core-valid snapshot from +1.0 through +2.0 hours.
- `03h.v2.md`: first core-valid snapshot from +3.0 through +4.5 hours.
- `24h.v2.md`: first core-valid snapshot from +24.0 through +28.0 hours.
- `72h.v2.md`: first core-valid snapshot from +72.0 through +76.0 hours.
- `7d.v2.md`: first core-valid snapshot from +168.0 through +192.0 hours.

Each platform prints its actual observed age from its own `published_at`. A
later lifetime total is never relabeled as an earlier checkpoint.

The runner treats the same full `(channel_id, content_hash)` as one logical
Reel and supports two distribution modes:

- `legacy_crosspost`: one Instagram media object supplies the historical
  crosspost metrics.
- `independent_dual_upload`: Instagram and Facebook have separate media
  objects, publication clocks, snapshots, links, and deltas.

Platform states are `NOT_STARTED`, `DUE`, `RECORDED`, `MISSED_CHECKPOINT`,
`NOT_PUBLISHED`, or `MEDIA_ID_MISSING`. A v2 file is written only after every
expected platform is terminal; `NOT_STARTED` and `DUE` remain retryable.

## Output folder

```text
out/aibrief_jp_reel_learning/
  YYYY-MM-DD/                   # first actual platform publication date in JST
    HHMM_<content-hash-12>/
      01h.v2.md
      03h.v2.md
      24h.v2.md
      72h.v2.md
      7d.v2.md
```

The directory identity is anchored to the first actual platform publication;
snapshot selection and age calculations still use each platform's own clock.
Existing legacy `01h.md`, `03h.md`, `24h.md`, `72h.md`, and `7d.md` files are never
overwritten.

The report keeps Instagram and Facebook metrics in separate panels. For
independent uploads it may show a clearly labeled sum of non-unique platform
play events only when both counts exist. It never sums reach. For legacy
crossposts, `crossposted_views` is already the aggregate and is never added to
Instagram views. At +3h, +24h, +72h, and +7d, each platform includes deltas
from its own previous available frozen checkpoint. The default 240-hour
lookback is long enough to find a Reel throughout the +7d window.

Each file also records whether the Reel launched as regular or Trial, its
current Trial cohort, and its phase at capture. When a matching
`trial_experiments` row exists, it adds the experiment id, case, state, asset
family, parent media, and baseline/variant hooks. A Reel launched as Trial
stays outside the regular performance baseline after graduation.

## Manual commands

Preview current work without Graph calls or file writes:

```bash
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py --report-version 2 --dry-run
```

Run the collector normally:

```bash
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py --report-version 2
```

Render still-missing files only from snapshots already in SQLite:

```bash
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py --report-version 2 --no-sync
```

Run the preserved Instagram-only formatter when a legacy v1 output is
specifically required:

```bash
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py --report-version 1
```

V2 requires both `state/reels.db` and `state/facebook.db`, including when it is
rendering historical crossposts. The normal runner calls
`reel_scheduler.py sync-insights --media-id ...` only for exact platform media
IDs with a checkpoint due, against the corresponding ledger. Independent
Instagram requests omit the old crosspost metrics. A run with no due
checkpoint performs no Graph request.

## Codex Scheduled task

Create one standalone Scheduled task in **local-project mode** for
`/Users/aiagent/GitHub/carousel-app`. Local mode is required because the task
shares `state/reels.db`, `state/facebook.db`, the scheduler lock, and the
`out/` journal with the publisher.

Use this advanced recurrence rule in Asia/Tokyo time:

```text
RRULE:FREQ=DAILY;BYHOUR=0,9,10,12,13,14,15,16,18,19,20,21,22;BYMINUTE=30;BYSECOND=0
```

The thirteen runs cover all unique checkpoint times for the regular 09:00,
13:00, 18:00, and 21:00 posting slots plus the daily 19:00 published-parent
Trial lane. The 20:30 run captures that lane's +1h window, while 22:30,
19:30 on the next day, and 19:30 three days later cover +3h, +24h, and +72h.
The same platform-specific publication clock makes the corresponding daily
run seven days later eligible for +7d.
The 15:30 run also covers the +3h window for manually published or jittered
late-morning Trials such as `PILOT-000`. The actual `published_at`, never the
nominal slot, decides whether a checkpoint is due.

### Task prompt

```text
Act as the AI Brief JP Reel checkpoint recorder in
/Users/aiagent/GitHub/carousel-app.

Run exactly:
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py --report-version 2

This task is analytics-only. Never queue, reschedule, edit, render, delete,
publish, or repost any content. The script may append exact-media insight
snapshots to state/reels.db and state/facebook.db and write immutable v2
Markdown checkpoint files only under out/aibrief_jp_reel_learning/.

If the command fails because Instagram or Facebook Graph access is blocked by
DNS/network sandboxing, retry the same exact command once with escalated
network permission. Do not change arguments, do not use `--no-sync` to mask
the failure, and do not fabricate checkpoints from later snapshots.

If the command records files, report each path, its +1h, +3h, +24h, +72h, or +7d
checkpoint, its distribution mode, and each platform checkpoint status:
RECORDED, MISSED_CHECKPOINT, NOT_PUBLISHED, or MEDIA_ID_MISSING. If the runner
is waiting on a DUE or NOT_STARTED platform, report that briefly. If nothing is
due, say so briefly. If Graph access still fails after the retry or the
scheduler lock prevents the run, report the exact error and do not fabricate a
checkpoint from a later snapshot.
```

The Scheduled task needs Instagram Insights access plus Facebook Page Reel
engagement access. Facebook Graph API v25 reads the Reel's direct Video
`views`, `likes`, and `comments` fields with `pages_read_engagement`, plus
associated Page-post `shares.count` when returned. The documented
`/{video-id}/video_insights` Reel edge additionally requires `read_insights`
and Page engagement permissions. The collector probes that edge once per
channel and uses richer plays, unique viewers, watch time, attributed follows,
reactions, retention, and social actions when authorized; otherwise it prints
one actionable warning and continues with the direct fallback. Publishing
access alone is not sufficient.

## Interpretation rules

- `EARLY_OBSERVATION` (+1h): baseline only; wait for +3h.
- `EARLY_TRAJECTORY` (+3h): describe movement from +1h; wait for +24h.
- `PROVISIONAL_24H` (+24h): suggest one future retest, then continue to the
  +72-hour decision checkpoint.
- `DECISION_READY_72H` (+72h): make a manual Trial graduate/stop decision and
  preserve the launch cohort for later analytics.
- `MATURE_7D` (+7d): review the mature trajectory without replacing the
  fixed +72h Trial decision.
- Raw counts stay beside engagement rates. A high rate on small reach is not a
  broad-audience win.
- Average watch divided by estimated duration is a diagnostic ratio, not a
  completion rate or retention curve.
- For independent uploads, a combined play count is non-unique and never a
  reach estimate. Never sum reach or add Instagram all-surface/crosspost fields
  to the separate Facebook object.
- For legacy crossposts, never add the already-aggregated
  `crossposted_views` to Instagram views.
