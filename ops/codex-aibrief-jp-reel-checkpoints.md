# AI Brief JP Reel checkpoint automation

This is a regular observation journal for `aibrief_jp`. It is not an A/B test
and does not attempt to prove which hook caused distribution.

For every newly published Japanese Reel, the runner records one immutable
Markdown analysis at each checkpoint:

- `01h.md`: first core-valid snapshot from +1.0 through +2.0 hours.
- `03h.md`: first core-valid snapshot from +3.0 through +4.5 hours.
- `24h.md`: first core-valid snapshot from +24.0 through +28.0 hours.

Every file prints the actual observed age. A later lifetime total is never
relabeled as an earlier checkpoint. If a window closes without a valid
snapshot, the runner records `MISSED_CHECKPOINT`.

## Output folder

```text
out/aibrief_jp_reel_learning/
  YYYY-MM-DD/                   # actual publication date in JST
    HHMM_<content-hash-12>/
      01h.md
      03h.md
      24h.md
```

The files keep Instagram, Meta all-surface, Facebook, and explicit
Instagram-plus-Facebook metrics separate. At +3h and +24h, the report includes
deltas from the previous available frozen checkpoint.

## Manual commands

Preview current work without Graph calls or file writes:

```bash
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py --dry-run
```

Run the collector normally:

```bash
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py
```

Rebuild only from snapshots already in SQLite:

```bash
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py --no-sync
```

The normal runner calls `reel_scheduler.py sync-insights --media-id ...` only
for the exact Reel identities with a checkpoint due. A run with no due
checkpoint performs no Graph request.

## Codex Scheduled task

Create one standalone Scheduled task in **local-project mode** for
`/Users/aiagent/GitHub/carousel-app`. Local mode is required because the task
shares `state/reels.db`, the scheduler lock, and the `out/` journal with the
publisher.

Use this advanced recurrence rule in Asia/Tokyo time:

```text
RRULE:FREQ=DAILY;BYHOUR=0,9,10,12,13,14,16,18,19,21,22;BYMINUTE=30;BYSECOND=0
```

The eleven runs cover all unique checkpoint times for the regular 09:00,
13:00, 18:00, and 21:00 posting slots. The actual `published_at`, never the
nominal slot, decides whether a checkpoint is due.

### Task prompt

```text
Act as the AI Brief JP Reel checkpoint recorder in
/Users/aiagent/GitHub/carousel-app.

Run exactly:
UV_CACHE_DIR=state/uv-cache uv run --frozen python scripts/run_aibrief_jp_reel_checkpoints.py

This task is analytics-only. Never queue, reschedule, edit, render, delete,
publish, or repost any content. The script may append exact-media insight
snapshots to state/reels.db and write immutable checkpoint Markdown files only
under out/aibrief_jp_reel_learning/.

If the command records files, report their paths and whether they are +1h,
+3h, +24h, or MISSED_CHECKPOINT. If nothing is due, say so briefly. If Graph
access or the scheduler lock prevents the run, report the exact error and do
not fabricate a checkpoint from a later snapshot.
```

The Scheduled task needs the same narrow Meta Graph access already used by the
existing insight sync. Do not grant publishing permission for this analytics
task.

## Interpretation rules

- `EARLY_OBSERVATION` (+1h): baseline only; wait for +3h.
- `EARLY_TRAJECTORY` (+3h): describe movement from +1h; wait for +24h.
- `PROVISIONAL_24H` (+24h): suggest one future retest, then continue to the
  existing 72–96-hour decision window.
- Raw counts stay beside engagement rates. A high rate on small reach is not a
  broad-audience win.
- Average watch divided by estimated duration is a diagnostic ratio, not a
  completion rate or retention curve.
- Never add overlapping Instagram, Facebook, crossposted, or Meta all-surface
  view metrics.
