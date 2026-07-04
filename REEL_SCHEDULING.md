# Reel Scheduling

`reel_scheduler.py` turns a clips folder into a durable Instagram reel schedule.
This document covers the **clips folder layout**, the **commands that exist
today**, and the channel-aware ledger with slot-based cadence, stats, and a
dashboard.

---

## Clips Folder Layout

There are **two clip sources** with different layouts, and discovery must handle
both:

- **This repo (`build_reel.py`)** writes one reel per folder to
  `out/reels/<channel>_<slug>/reel.mp4` — a plain `reel.mp4`, with the channel
  encoded in the **folder name**. This is what the current
  `media_filename: reel.mp4` default matches.
- **The separate `reel-app` project** writes the multi-channel layout below,
  where the channel is encoded in the **filename** and one folder serves every
  channel at once. This is the source you point `plan` at for the 3–4/day work.

The redesign standardizes on the `reel-app` multi-channel layout. Its structure:
one source video becomes one output folder holding a `clips/` directory plus
source-level sidecars:

```text
reel-app/outputs/<VIDEO_ID>/            e.g. We7BZVKbCVw, PQU9o_5rHC4
├── metadata.json                       source video metadata (title, webpage_url, uploader)
├── transcript.en.json
├── candidates.json
└── clips/
    ├── 001-<slug>/
    │   ├── notes.json                  shared per-clip metadata (one_liner, transcript, score, index)
    │   ├── one_liners.json             per-language localized hook, e.g. {"ja": "【…】…"}
    │   ├── reel.en.vibecodersph.mp4    English variant  → vibecodersph channel
    │   ├── reel.ja.aibrief_jp.mp4      Japanese variant → aibrief_jp channel
    │   ├── subtitles.en.ass
    │   └── subtitles.ja.ass
    ├── 002-<slug>/
    └── …
```

Two things drive the redesign:

1. **One clip folder targets multiple channels.** The media filename encodes the
   routing: `reel.<lang>.<channel_id>.mp4`. A folder that contains both
   `reel.en.vibecodersph.mp4` and `reel.ja.aibrief_jp.mp4` should produce **two**
   scheduled reels — one per channel — from the same source clip.

2. **`metadata.json` is a sibling of `clips/`**, so source attribution
   (`webpage_url`) applies to every clip in the folder. This already matches
   `load_source_metadata()`, which reads `clips_dir.parent / "metadata.json"`.

### Filename convention

```text
reel.<lang>.<channel_id>.mp4
        │         └─ must match a channels/<channel_id>/channel.json
        └─ language tag: en, ja, …  (used to pick caption language + one_liners key)
```

The channel id in the filename is the source of truth for *where a clip is
posted*. The planner discovers a folder's channel targets by globbing
`reel.*.<channel_id>.mp4`, not by a single fixed `media_filename`.

---

## Commands That Exist Today

There are two scheduling paths:

- **Legacy schedule path:** `plan` scans a clips folder for **one** configured
  filename and writes a per-run `schedule.json` plus a manifest + caption per
  clip. `run-due path/to/schedule.json` still publishes those jobs and now also
  mirrors status into the SQLite ledger.
- **Ledger path for reel-app clips:** `scan` and `plan-ledger` understand the
  multi-channel `reel.<lang>.<channel>.mp4` layout. They upsert one row per
  clip/channel, fill per-channel posting slots, write normal Instagram
  manifests, and let `run-due` publish directly from `state/reels.db`.

Date inputs can be simple: `2026-06-24` is accepted anywhere a start date is
needed. The fallback timezone is JST (`Asia/Tokyo`); channel-specific slot
timezones still apply when configured.

```sh
# Create a schedule (today: one channel, one filename, evenly spaced by interval)
uv run python reel_scheduler.py plan \
  /Users/aiagent/GitHub/reel-app/outputs/We7BZVKbCVw/clips \
  2026-06-24 \
  --channel aibrief_jp \
  --media-filename reel.ja.aibrief_jp.mp4

# Preview every job without publishing
uv run python reel_scheduler.py run-due path/to/schedule.json --all --dry-run

# Publish only jobs whose time has arrived, uploading to R2 first
uv run python reel_scheduler.py run-due path/to/schedule.json --upload-r2

# Seed the ledger from old schedule.json files
uv run python reel_scheduler.py import-schedules

# Plan multi-channel reel-app clips into per-channel daytime slots
uv run python reel_scheduler.py plan-ledger \
  /Users/aiagent/GitHub/reel-app/outputs/PQU9o_5rHC4/clips \
  2026-06-24

# Preview or publish due ledger rows
uv run python reel_scheduler.py run-due --dry-run --all
uv run python reel_scheduler.py run-due --upload-r2

# Operator views and stats
uv run python reel_scheduler.py status
uv run python reel_scheduler.py sync-insights --limit 10
uv run python reel_scheduler.py report --out out/reel_report.html
```

### Legacy Schedule Limits

- **Schedule-centric, not clip-centric.** Legacy truth lives inside each per-run
  `schedule.json`. Answering "which clips have ever been scheduled or published"
  requires scanning every schedule and joining on `media_path`. Use
  `import-schedules` once to seed the ledger from old schedules; the duplicate
  `…_attributed` schedule collapses via the content-hash key.
- **Single fixed `media_filename`.** Legacy `discover_clips()` does
  `rglob(media_filename)`, so it can only target one channel per run and ignores
  the multi-channel `reel.<lang>.<channel>.mp4` convention. `aibrief_jp`'s
  `media_filename: reel.mp4` is *not* broken — it correctly matches the
  single-channel reels `build_reel.py` writes to `out/reels/`. But no file named
  `reel.ja.aibrief_jp.mp4` exists in *this* repo; those live in the `reel-app`
  outputs, so scheduling from there should use `plan-ledger`.
- **Cadence is a single interval from one anchor** (`interval_hours`). In legacy
  mode, to post
  3–4×/day you would set `interval_hours: 6`, which posts around the clock
  (09:00, 15:00, 21:00, 03:00 local) rather than in chosen daytime slots. The
  ledger path uses per-channel `slots`.
- **`run-due path/to/schedule.json` read-modify-writes the whole
  `schedule.json`** on every status change. Ledger-native `run-due` claims rows
  with a conditional SQLite update before publishing.

---

## Channel-Aware Ledger + Slots

The single structural move: **make the clip-on-a-channel the unit of truth, and
demote `schedule.json` to disposable render output.** A small SQLite ledger
becomes the source of truth; `plan-ledger`, ledger-native `run-due`, stats, and
the dashboard all read and write it.

```text
clips/ (library, multi-channel)        state/reels.db (source of truth)        views
  001-…/reel.en.vibecodersph.mp4  ─┐    row per (clip × channel):              • plan   fill open slots
  001-…/reel.ja.aibrief_jp.mp4    ─┼──► status, scheduled_at, published_at,    • status CLI summary
  002-…/reel.en.vibecodersph.mp4  ─┘    media_id, permalink, insights[]        • report HTML dashboard
                                                                               • stats  insights sync
```

Store the db somewhere **persistent and backed up** (e.g. `state/reels.db`, not
under `out/` if that is disposable), with WAL mode enabled.

### Data model (SQLite)

```sql
-- one row per (clip variant × channel): the two language variants of a clip
-- are different files AND different channels, so they are two independent rows
CREATE TABLE reels (
  content_hash  TEXT NOT NULL,        -- sha256 of THIS channel's mp4 = stable identity
  channel_id    TEXT NOT NULL,        -- parsed from filename reel.<lang>.<channel>.mp4
  lang          TEXT,                 -- parsed from filename (en, ja, …)
  clip_dir      TEXT NOT NULL,
  media_path    TEXT NOT NULL,        -- the channel-specific variant file
  source_video  TEXT,                 -- <VIDEO_ID> folder, for grouping
  title         TEXT,                 -- en: notes.one_liner ; ja: one_liners.json["ja"]
  caption       TEXT,
  status        TEXT NOT NULL,        -- new | scheduled | publishing | published | failed | skipped
  scheduled_at  TEXT,                 -- tz-aware ISO 8601
  published_at  TEXT,
  media_id      TEXT,                 -- IG media id, from publish report result.published.id
  permalink     TEXT,
  last_error    TEXT,
  manifest_path TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (content_hash, channel_id)   -- this UNIQUE is the dedup guard, for free
);

-- time series so the dashboard can show "plays over time", not just a snapshot
CREATE TABLE insights (
  id            INTEGER PRIMARY KEY,
  content_hash  TEXT NOT NULL,
  channel_id    TEXT NOT NULL,
  media_id      TEXT NOT NULL,
  captured_at   TEXT NOT NULL,
  views INTEGER, reach INTEGER, likes INTEGER, comments INTEGER,
  saved INTEGER, shares INTEGER, total_interactions INTEGER,
  raw           TEXT,                 -- full API JSON (e.g. ig_reels_avg_watch_time, reposts)
  FOREIGN KEY (content_hash, channel_id) REFERENCES reels(content_hash, channel_id)
);
```

`content_hash` is the hash of the channel-specific variant, so
`reel.en.vibecodersph.mp4` and `reel.ja.aibrief_jp.mp4` are distinct rows even
before `channel_id` is considered. The `(content_hash, channel_id)` primary key
makes re-running `plan` idempotent and prevents double-publishing.

### Channel routing on discovery

`scan` and `plan-ledger` scan a clips folder and **fan out across every channel
present**:

1. For each clip subfolder, find `reel.<lang>.<channel_id>.mp4` files.
2. For each match, resolve `channel_id` to a `channels/<id>/channel.json`. Skip
   filenames whose channel id has no config (and log the skip).
3. Upsert one `reels` row per (variant × channel) with status `new` if unseen.
4. Pick the title/caption language from the filename's `lang` tag:
   - `en` → `notes.json.one_liner`
   - `ja` → `one_liners.json["ja"]` (fallback to `notes.json.one_liner_translated`,
     then `notes.json.one_liner`). Ledger-planned captions pass that routed
     title into the manifest and caption builder.

This keeps legacy `--media-filename` support for single-channel builds while the
ledger path uses the convention matcher for reel-app outputs.

### Cadence: per-channel slots

Cadence is **per channel** — 3–4×/day is a per-channel target, and the two
channels have different audiences and timezones. Configure slots in each
`channel.json` under `publishing.instagram_reels`:

```json
// channels/aibrief_jp/channel.json  (JP audience, Tokyo)
"instagram_reels": {
  "slots": ["09:00", "13:00", "19:00"],
  "timezone": "Asia/Tokyo",
  "skip_days": [],
  "jitter_minutes": 7
}
```

```json
// channels/vibecodersph/channel.json  (Manila; instagram_user_id can stay in env)
"publishing": {
  "instagram_reels": {
    "slots": ["12:00", "18:00", "21:00"],
    "timezone": "Asia/Manila",
    "jitter_minutes": 7
  }
}
```

The planner walks forward day by day per channel, dropping the next `new` clip
into the next open slot, capped at `len(slots)` per day. When multiple source
video folders have new unscheduled clips, `plan-ledger` round-robins those
sources (`A1, B1, A2, B2`) while preserving clip order inside each source. Rows
that are already scheduled, previewed, or published are not reshuffled.
`interval_hours` stays supported as a fallback so nothing breaks.
`jitter_minutes` avoids a robotic exact-`:00` posting footprint. Per-channel
tokens already exist in `.env` (`META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH`),
so each channel publishes with its own credentials.

### Command surface (evolution of the current CLI)

| Command | Behavior | Answers |
| --- | --- | --- |
| `scan <clips_dir>` | Hash each variant, upsert into the ledger (`new` if unseen), fan out across all channels found, skip unknown channel ids. | discovery + dedup |
| `plan-ledger <clips_dir>` | Run `scan`, round-robin new unscheduled source folders, assign `scheduled_at` by filling open per-channel slots, and write manifests/captions under `out/reel_schedules/ledger/`. Existing scheduled/published rows are preserved. | 3–4/day planning |
| `run-due [--channel X]` | With no schedule path, publish due ledger rows, store `media_id` + permalink, and claim rows before publishing. With a schedule path, run legacy jobs and mirror status into the ledger. | publishing |
| `status [--channel X]` | Print counts (new/scheduled/published/failed) + the next 7 days. | "which clips are scheduled/published" |
| `sync-insights [--channel X]` | For each `published` row with a `media_id`, pull Graph insights and append a timestamped snapshot. | stats |
| `report --out report.html` | Render the ledger to one self-contained HTML file plus `*.insights.json` and `*.insights.md` exports. | dashboard + LLM review |
| `insights-md [json_path]` | Convert an existing insights JSON export into a readable Markdown table. | LLM review |
| `import-schedules` | One-time: walk existing `out/reel_schedules/*/schedule.json`, hash each media file, and seed the ledger so previewed/published history is not lost. Collapses the duplicate `…_attributed` schedule via the dedup key. | migration |

### Stats (your keys can do this)

The published media id is captured in each **real** publish report
(`result.published.id` in `instagram_publish.json`; dry-run reports have an empty
`result`, so only live publishes are stats-eligible). `sync-insights` reads it
and calls Graph
`GET /{media-id}/insights?metric=views,total_views,reach,likes,total_likes,comments,total_comments,saved,shares,total_interactions`,
storing a snapshot per run so trends are visible.

**API caveats (verified against current Meta docs):**

- The access token needs **`instagram_business_manage_insights`** (paired with
  `instagram_business_basic`) for the Instagram Login / business-login path this
  project uses — *not* `instagram_manage_insights`, which belongs to the older
  Facebook-Login product.
- Metric names changed in Meta's 2025 consolidation: use **`views`** (not
  `plays`) for playback count, and the saves field is **`saved`** (not `saves`).
  **`impressions` is deprecated** and not requestable from API v22.0+ — leave it
  out. `reach`, `likes`, `comments`, `shares`, `total_interactions` remain valid;
  reels-specific extras like `ig_reels_avg_watch_time` and `reposts` are
  available if wanted.
- The report stores **`total_views`**, **`total_likes`**, and
  **`total_comments`** into the visible `views`/`likes`/`comments` columns when
  Meta returns them. This better matches the Instagram app on older/crossposted
  reels while still falling back to the base metrics when the totals are absent.
- Verify with a single `/insights` call before relying on live numbers.

### Dashboard

`report` emits **one self-contained HTML file** from the ledger: a
calendar of upcoming posts (per channel), a clip table with status badges, and
published reels with their latest insights. It also writes a neighboring
`*.insights.json` file with the latest metrics, source metadata, and local reel
transcript data, plus a readable `*.insights.md` table with columns for stats,
reel hook, and transcript so the performance data can be dropped into an LLM for
recommendations. Transcript extraction prefers the actual rendered reel subtitle
file (`subtitles.<lang>.ass`) in
`/Users/aiagent/GitHub/reel-app/outputs/<youtube_id>/clips/...`. If a ledger row
has stale paths, the exporter reads the YouTube id from the manifest/source URL,
finds that output folder, and matches the exact channel/language reel by media
hash or localized hook.

When `queue-ui` is running, open `http://127.0.0.1:8765/report` or click the
report's **Update Instagram Insights** button. The button calls the same
server-side `sync-insights` path, keeps the Instagram token out of the browser,
and regenerates both the HTML report and JSON export. Zero hosted infra; runs
from the same local process.

### Concurrency

Ledger-native `run-due` claims a row by conditionally changing
`scheduled`/`publish_previewed` (and `failed` when `--retry-failed` is set) to
`publishing` before invoking `instagram_publish.py`. If another runner claimed
the row first, the second runner skips it. Legacy `run-due path/to/schedule.json`
still has JSON-file clobber risk and should not be overlapped.

### Rollout order

1. Done: `reel_ledger.py` schema + `import-schedules` preserves history.
2. Done: channel-routing discovery, `plan-ledger`, ledger-native `run-due`, and
   `status`.
3. Done: JP captions use `one_liners.json`; `vibecodersph` has a reels cadence
   block.
4. Done: `sync-insights` appends snapshots for published media ids.
5. Done: `report --out out/reel_report.html`. FastAPI only if browser actions
   are needed later.
