# Reel Scheduling

`reel_scheduler.py` turns a clips folder into a durable Instagram reel schedule.
Each clip directory is matched against the selected channel's
`publishing.instagram_reels.media_filename`, and its `notes.json` supplies the
description used in the caption. When the clips folder has a sibling
`metadata.json`, its original URL is included as source attribution. Channel
defaults supply the language, CTA, hashtags, timezone, and posting cadence.

## Create A Schedule

For `aibrief_jp`, the configured input is `reel.mp4`, the timezone is Tokyo, and
the default cadence is one reel per day at 09:00.

```sh
uv run python reel_scheduler.py plan \
  /Users/aiagent/GitHub/reel-app/outputs/PQU9o_5rHC4/clips \
  --channel aibrief_jp \
  --start-at 2026-06-23T09:00:00+09:00
```

The command writes `out/reel_schedules/<channel>_<timestamp>/schedule.json`, plus
one `manifest.json` and `caption.txt` per clip. Override the cadence with
`--interval-hours`, or use `--limit` while reviewing a small batch.

## Preview And Publish

Preview every job without publishing:

```sh
uv run python reel_scheduler.py run-due path/to/schedule.json --all --dry-run
```

Publish only jobs whose scheduled time has arrived, uploading the local video to
Cloudflare R2 before Instagram reads it:

```sh
uv run python reel_scheduler.py run-due path/to/schedule.json --upload-r2
```

The existing Instagram and R2 environment variables used by
`instagram_publish.py` are required for live publishing. A periodic runner can
call `run-due` safely; completed jobs are skipped, previewed jobs remain eligible
for live publishing, and failures require `--retry-failed`.
