# Carousel Automation

Render branded Instagram-style carousel assets from X posts, web articles, X
Articles, weekly story queues, and source videos. The project is channel-based:
branding, language, audience, handle, and voice all come from one channel config.

Generated media and manifests are written under `out/`, which is ignored by git.

## Kinetic Image Cover Page

Open `index.html` in a browser to view the standalone kinetic cover page. It has
no build step and uses only `index.html`, `styles.css`, and `script.js`.

The page uses the generated title art from
`out/image_brief_sample_2070555272230384038_vibecodersph/title_assets/` as the
hero image, then animates it with zooming image planes, moving crop slices,
orbiting image windows, and flipping page panels. The cover keeps a 4:5
Instagram-friendly frame and uses the sample manifest copy for the headline and
CTA.

## What It Builds

| Workflow | Script | Default output | Use it for |
| --- | --- | --- | --- |
| X post/thread | `build_x_carousel.py` | `out/x_carousel/` | One X status URL, with optional same-author thread posts and video slides |
| Web article | `build_article_carousel.py` | `out/article_carousel/` | One long-form article URL or local HTML file |
| X Article | `build_x_article_carousel.py` | `out/x_article_carousel/` | Long-form X notes/articles behind a status URL |
| Weekly roundup | `build_weekly_carousel.py` | `out/weekly_carousel/` | A ranked set of top AI stories from the scout queue or an input JSON |
| Research idea story | `research_idea_generator/cli.ts` + `build_idea_carousel.py` | `out/research_idea_generator/` | Research-backed AI-builder story briefs rendered into carousels |
| Cover art | `generate_cover.py` | `out/cover_<slug>.png` | Standalone channel-branded cover images |
| Scroll-stopper cover variants | `scroll_stopper_cover.py` | `out/scroll_stopper_cover/<slug>/` | 3-6 editable HTML/CSS first-slide cover options with attention scores |
| Video slide | `build_video_slide.py` | `out/video_slide_02.mp4` | A local video, remote video, or X video inside the branded carousel frame |
| Reel | `build_reel.py` | `out/reels/` | A video post turned into a full-bleed, branded 9:16 Instagram reel (render only) |

Publishing helpers:

- `instagram_publish.py` publishes any generated `manifest.json` through the
  Instagram Graph API.
- `story_scout.py` scans, scores, approves, builds, and optionally publishes
  X/article candidates.

## Setup

```sh
uv sync
uv run python -m playwright install chromium
```

Install `ffmpeg` locally; carousel covers render as MP4, and video posts also use it.

Most scripts load `.env` from the repo root. Add only the credentials you need:

```sh
# Gemini text, article curation, captions, and title copy
GOOGLE_API_KEY=...
# or GEMINI_API_KEY=...

# OpenAI cover/title image generation
OPENAI_API_KEY=...

# xAI X search for tweet/thread/X Article lookup
XAI_API_KEY=...

# Optional publishing
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=...
R2_PUBLIC_BASE_URL=https://...
INSTAGRAM_USER_ID=...
INSTAGRAM_ACCESS_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

xAI lookup can also use a Hermes OAuth token:

```sh
hermes auth add xai-oauth
```

Useful model overrides:

```sh
GEMINI_TEXT_MODEL=gemini-3.5-flash
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_TITLE_IMAGE_SIZE=2048x1152
GEMINI_IMAGE_MODEL=gemini-3.1-flash-image
XAI_TWEET_MODEL=grok-4.3
XAI_ARTICLE_MODEL=grok-4.3
```

## Channels

A channel bundles the parts that make output feel native to one account:

- slide branding and typography
- account name and handle
- language and audience
- voice guide used by cover/caption/copy prompts
- optional publishing defaults, such as an Instagram user id

Channel files live under `channels/`:

```text
channels/
  channels.json
  vibecodersph/channel.json
  aibrief_jp/channel.json
  aibrief_jp/voice.md
```

The active channel is resolved in this order:

1. `--channel <id>`
2. `CAROUSEL_CHANNEL`
3. `default_channel` in `channels/channels.json`
4. built-in `vibecodersph` fallback

Inspect channels:

```sh
uv run python channel.py --list
uv run python channel.py aibrief_jp
```

Run any builder against a channel:

```sh
uv run python build_article_carousel.py "https://example.com/story" --channel aibrief_jp
CAROUSEL_CHANNEL=aibrief_jp uv run python build_x_carousel.py "https://x.com/user/status/123"
```

To add a channel, copy an existing `channels/<id>/` folder, edit
`channel.json`, add or point to a voice guide, and set `default_channel` or pass
`--channel`. Keep the voice guide's `## Copy-Paste Prompt Block For Automation`
section if you want the pipeline to inject a focused prompt block.

For non-Latin output, make sure the channel typography points at an available
font and the render path can load it offline. Japanese output, for example,
needs a Noto Sans JP-style font in addition to `assets/archivo.css`.

## Build Workflows

### Scroll-Stopper Cover Variants

```sh
uv run python scroll_stopper_cover.py \
  "How to make Instagram carousels get more saves" \
  --audience "solo creators and marketers" \
  --promise "Teach 5 first-slide fixes that increase saves and swipes" \
  --motion kinetic
```

The generator writes `scroll_stopper_covers.json` plus one preview HTML file per
variant. Each variant includes sanitized HTML, scoped `.ssc-` CSS, optional image
asset prompts, an attention score, and export hints for `1080x1350` carousel
covers. Use `--allow-generated-images` to include OpenAI image prompts in the
asset plan, and `--generate-images` to actually create those assets with the
configured image model.

### X Post Or Thread

```sh
uv run python build_x_carousel.py "https://x.com/OpenAI/status/2061887650391625870"
```

The builder writes an ordered `manifest.json` plus slides to `out/x_carousel/`.
The first slide is the channel-branded cover; later slides are the source post,
thread posts, and any rendered media.

Thread discovery defaults to `auto`: xAI `x_search` when `XAI_API_KEY` or Hermes
OAuth is available, otherwise Playwright. Pin the backend when needed:

```sh
uv run python build_x_carousel.py "https://x.com/user/status/123" --thread-source xai
uv run python build_x_carousel.py "https://x.com/user/status/123" --thread-source playwright
uv run python build_x_carousel.py "https://x.com/user/status/123" --no-thread
```

If X hides replies or media from anonymous browsers, pass logged-in browser
cookies through Playwright/yt-dlp:

```sh
uv run python build_x_carousel.py "https://x.com/user/status/123" --cookies-from-browser chrome
```

Use `X_THREAD_SOURCE` or `X_COOKIES_FROM_BROWSER` when automation should apply
the same defaults without extra CLI flags.

Other useful flags:

```sh
uv run python build_x_carousel.py "https://x.com/user/status/123" \
  --max-thread-posts 6 \
  --cover-headline "Pati yung gumawa, [tinatamad] na mag-type." \
  --cover-swipe-line "Silipin ang cheat sheet"

uv run python build_x_carousel.py "https://x.com/user/status/123" --first-page-only
uv run python build_x_carousel.py "https://x.com/user/status/123" --skip-first-explainer
```

### Web Article

```sh
uv run python build_article_carousel.py \
  "https://venturebeat.com/technology/xiaomis-new-open-source-agentic-ai-coding-harness-mimo-code-beats-claude-code-at-ultra-long-200-step-tasks"
```

The builder writes to `out/article_carousel/`:

- `slide_01.mp4`: animated channel-branded title/hook slide
- `slide_01_poster.png`: poster frame for the animated cover
- `slide_02.png` and onward: selected high-signal article sections
- `manifest.json`: slide order, source metadata, curation backend, selected scores
- `source_article.json`: extracted blocks and candidate scores for review

Animated covers keep the generated cover art visible full-frame from frame one.
The text floats above it with near-still premium motion: subtle parallax,
drifting grain, creeping light, and a restrained boxed-to-clean kinetic
typography morph.

Gemini curation is used when `GOOGLE_API_KEY` or `GEMINI_API_KEY` is available;
otherwise local scoring is used. Tune the run with:

```sh
uv run python build_article_carousel.py "https://example.com/story" \
  --max-pages 5 \
  --min-score 6 \
  --curation-backend gemini

uv run python build_article_carousel.py "file:///absolute/path/story.html" --curation-backend local
uv run python build_article_carousel.py "https://example.com/story" --no-title-enrichment
```

### X Article

```sh
uv run python build_x_article_carousel.py "https://x.com/satyanadella/status/2066182223213293753"
```

X Articles and long-form X notes are fetched with xAI `x_search`, rebuilt into
the same article structure as the web-article pipeline, then rendered with the
article carousel system. Use `XAI_API_KEY` or Hermes OAuth.

X Articles default to `--min-score 3`, lower than web articles, because they are
often essay-style rather than benchmark-dense news. The familiar article flags
also work:

```sh
uv run python build_x_article_carousel.py "https://x.com/user/status/123" \
  --max-pages 6 \
  --curation-backend auto \
  --no-title-enrichment
```

### Weekly Roundup

```sh
uv run python build_weekly_carousel.py --channel vibecodersph
uv run python build_weekly_carousel.py --channel aibrief_jp --max-stories 6
```

The weekly builder creates a cover, one slide per story, and an outro. It reads
an explicit `--input stories.json` first; otherwise it ranks recent candidates
from `out/automation/candidates.json`.

```sh
uv run python build_weekly_carousel.py --input stories.json --max-stories 8
uv run python build_weekly_carousel.py --days 7 --per-source 2
uv run python build_weekly_carousel.py --verify
uv run python build_weekly_carousel.py --reuse-cover
```

Instagram supports up to 20 carousel slides. The weekly builder reserves one
for the cover and one for the outro, so story slides are clamped to 3-18.

`--verify` runs the source/copy verifier and writes `run_manifest.json` without
rendering slides.

### Research Idea Generator

The research idea generator's canonical handoff is
`out/research_idea_generator/carousel_briefs.json`. Treat this as the standard
for research-backed ideas: each brief contains the selected hook, lean
`slides[]`, `instagramDescription`, confidence/risk metadata, and `evidenceUrls`.

```sh
npm run ideas:research -- run --provider local --cards 5

uv run python build_idea_carousel.py \
  --input out/research_idea_generator/carousel_briefs.json \
  --index 0 \
  --out-dir out/research_idea_generator/story_render \
  --cover-style kinetic-fly \
  --cover-template auto \
  --no-generate-images
```

`--index` chooses the story from `carousels[]`; the generator writes higher
scoring ideas first, so index `0` is the default story to test. The renderer
accepts this brief shape directly and renders only the selected brief's
`slides[]`: the cover uses the hook, and each following slide uses only its JSON
headline plus any provided `lines[]`.

Every archived research run also writes its own immutable `carousel_briefs.json`
under `out/research_idea_generator/runs/**/`. Source-specific jobs can keep
their own cadence and output folders, then a queue scanner can discover
unpublished briefs across all archived runs without merging source outputs:

```sh
npm run ideas:scan-briefs
```

The scanner writes `out/research_idea_generator/carousel_brief_queue.json`, keyed
by the carousel brief id plus evidence URLs. It preserves publish lifecycle
state (`new`, `rendered`, `scheduled`, `published`, `skipped`, `failed`) in the
queue file instead of mutating archived run folders. Newly discovered briefs are
assigned `aibrief_jp` publish slots at `09:00`, `12:00`, `18:00`, and `21:00`
Asia/Tokyo, so every queued item carries a concrete `scheduledAt` value.

The publisher wrapper scans archived runs, then publishes the next due queue
item with a rendered manifest:

```sh
scripts/run_research_carousel_publisher_due.sh
```

#### Carousel Music Clips

Instagram's Graph API does not expose the in-app carousel music picker, so this
repo treats music as a local render/publish step. Put short audio clips in
`assets/music/clips/`, copy `assets/music/library.example.json` to
`assets/music/library.json`, and point each track at a 15-60 second file.

If Suno generates a full song, trim it before adding it to the library:

```sh
mkdir -p assets/music/clips
ffmpeg -y -ss 0 -t 60 -i ~/Downloads/suno-track.mp3 \
  -af "afade=t=in:st=0:d=0.7,afade=t=out:st=59.3:d=0.7" \
  assets/music/clips/signal-glow-60s.mp3
```

`build_idea_carousel.py` muxes the selected clip into the cover MP4 when a
library exists, and `instagram_publish.py` has the same fallback before R2
upload for already-rendered carousel manifests. Both paths skip one-item video
publishes/Reels, and both support:

```sh
--music-library assets/music/library.json --music-clip signal-glow --music-duration 60
```

Use `--no-carousel-music` to disable the local clip step for a render or publish.

When `--cover-style kinetic-fly` is enabled, `--cover-template auto` scores the
hook and chooses a motion cover template from `stop-signal`, `pattern-break`,
`metric-snap`, `split-switch`, and `loom-reveal`. You can pass any template id
to force a specific cover during QA; item slide rendering is unchanged.

### Cover Art

```sh
# OpenAI image generation, default provider
uv run python generate_cover.py "Fable 5 changes everything"

# Gemini image generation
uv run python generate_cover.py "Fable 5 changes everything" --provider gemini --model nano-banana-pro

# xAI Grok Imagine
uv run python generate_cover.py "Why reasoning models win" --provider xai

# Preview the prompt without generating
uv run python generate_cover.py "topic" --prompt-only
```

Styles: `abstract` (default), `typographic`, `minimal`, `illustrative`, `photo`.
The prompt uses the active channel's brand palette and falls back to `brand.json`
only when needed.

### Video Slide

```sh
uv run python build_video_slide.py \
  --source assets/video_sources/example.mp4 \
  --source-label "SOURCE VIDEO" \
  --caption "The source clip stays inside the carousel frame."
```

An X post URL can also be the source:

```sh
uv run python build_video_slide.py \
  --source "https://x.com/claudeai/status/2064394146916229443" \
  --cookies-from-browser chrome \
  --source-label "@claudeai on X" \
  --caption "Claude's launch video, framed as a carousel receipt."
```

Outputs:

- `out/video_frame_02.png`
- `out/video_slide_02.mp4`
- `out/video_slide_02_poster.png`
- `out/video_slide_02.json`

Use `--fit contain` to preserve the full source clip, or `--fit cover` to crop
into the media well.

### Reel

Turns a video post into a branded, Instagram-ready 9:16 reel for one channel, using
a "tech-news card" layout: the channel logo + name + handle on top, a
channel-language headline, the source video *contained* on the channel's themed
surface (so nothing is ever cropped), and a view-count chip. This is render-only: it
downloads and composes, it never publishes.

```sh
uv run python build_reel.py \
  --source "https://x.com/HighSignal_AI/status/2068287838328959444" \
  --channel aibrief_jp
```

Everything is channel-driven (nothing hardcoded):

- **Logo** — `channels/<id>/logo.png`, the `logo` field in `channel.json` (see
  `make_channel_logos.py` to regenerate, or drop in your own PNG).
- **Headline** — written in the channel's language with xAI (Japanese for
  `aibrief_jp`, Taglish/English for `vibecodersph`). Pass `--headline "..."` to
  override; falls back to the post text when no xAI credential is present.
- **Theme** — the `brand.reel` block in `channel.json` (background/text/accent),
  with a dark default.

The source is scaled to fit the media window and centred, so landscape, portrait,
and square all keep the whole subject visible. Pass `--cookies-from-browser chrome`
when X gates the media.

Each run writes to `out/reels/<channel>_<handle>_<status_id>/`:

- `reel.mp4` — the finished 9:16 reel
- `overlay.png` — the transparent brand/design layer
- `poster.png` — first-frame poster
- `qa_frame_10.png`, `qa_frame_50.png`, `qa_frame_90.png` — sample frames for review
- `reel.json` — manifest

For queueing and publishing reel-app outputs, use `reel_scheduler.py`:

```sh
uv run python reel_scheduler.py queue-outputs /Users/aiagent/GitHub/reel-app/outputs
uv run python reel_scheduler.py queue-outputs /Users/aiagent/GitHub/reel-app/outputs --mode reshuffle
uv run python reel_scheduler.py queue-ui --limit 50
uv run python reel_scheduler.py report --out out/reel_report.html
```

`queue-outputs` scans each `<VIDEO_ID>/clips/` folder under a reel-app outputs
root, schedules new rows into the ledger, and can reshuffle the unpublished
queue after appending. `queue-ui` serves a local review page with unschedule
actions for unpublished items, and `scripts/run_reel_scheduler_due.sh` now
refreshes the HTML report after each due-run. The report writes LLM-ready
`reel_report.insights.json` and readable `reel_report.insights.md` sidecars.
The Markdown table uses the actual reel subtitle transcript from
`/Users/aiagent/GitHub/reel-app/outputs/<youtube_id>/clips/.../subtitles.<lang>.ass`
when available, matched from the reel's YouTube source id, plus the latest
Instagram metrics. When `queue-ui` is running, the report's **Update Instagram
Insights** button fetches fresh Graph API insights and regenerates all report
artifacts.

Cadence is controlled by each channel's `publishing.instagram_reels.slots` in
`channels/<id>/channel.json`. The LaunchAgent in
`ops/com.carousel.reel-scheduler.plist` should stay on its 900-second check
interval; that timer controls pickup latency, not posts per day. For
`aibrief_jp`, use `scripts/apply_aibrief_4day_reflow.sh --dry-run --start-at
2026-07-04` to preview the four-Reel schedule migration, then rerun with
`--apply` only when intentionally updating the live ledger.

To rebuild only the readable table from an existing JSON export:

```sh
uv run python reel_scheduler.py insights-md out/reel_report.insights.json
```

## Story Scout Automation

`story_scout.py` is the human-in-the-loop front door. It scans configured X
accounts and article feeds, scores candidates, queues them for approval, and
hands approved items to the matching one-URL builder.

Create a local source list:

```sh
cp story_sources.example.json story_sources.json
```

Scan, inspect, approve:

```sh
uv run python story_scout.py scan --config story_sources.json
uv run python story_scout.py list
uv run python story_scout.py approve x_abc123def0
uv run python story_scout.py approve article_abc123def0 --article-curation-backend local
```

Approved builds go under `out/automation/builds/<candidate_id>/`, and the queue
records the resulting manifest path in `out/automation/candidates.json`.

Publish previews can be attached to approval:

```sh
uv run python story_scout.py approve x_abc123def0 \
  --publish-instagram \
  --instagram-upload-r2 \
  --instagram-dry-run
```

Telegram approval callbacks are optional:

```sh
export TELEGRAM_BOT_TOKEN=123456:...
export TELEGRAM_CHAT_ID=123456789

uv run python story_scout.py scan --config story_sources.json --notify
uv run python story_scout.py telegram-poll --watch --publish-instagram --instagram-upload-r2
```

The broader automation plan lives in `AUTOMATION_ROADMAP.md`.

## AI News Sourcing And Ranking

The TypeScript pre-validation pipeline finds demand-proven AI posts, dedupes
them, ranks them, and writes candidate records for human approval. It is
reversible up to the queue stage: topic approval and publishing still happen
through explicit `story_scout.py approve` / publish commands.

```sh
npm run source:ai -- --reddit-only --no-media --no-remember
npm run rank:ai -- --input out/automation/sourcing/source_items.json --top 30
npm run pipeline:ai -- --top 30
```

Useful direct form:

```sh
node sourcing/cli.ts run --top 30
node sourcing/cli.ts source --reddit-only --no-media --no-remember \
  --out out/automation/sourcing/source_items.dryrun.json
node sourcing/cli.ts source --x-queue out/automation/candidates.json \
  --no-reddit --no-media --no-remember --no-top-replies \
  --dedup-state /tmp/carousel-dedup-check.json
node sourcing/cli.ts source --max-duration-seconds 90 --max-height 720
```

What it writes:

- `out/automation/sourcing/source_items.json`: deduped `SourceItem` objects
- `out/automation/sourcing/dedup-state.json`: seen ids and recent title embeddings
- `out/automation/source_media/`: downloaded mp4 assets when media download is enabled
- `out/automation/ranking/ranked_items.json`: ranked queue candidates
- `out/automation/ranking/spectacle-cache.json`: cached spectacle scores
- `out/automation/candidates.json`: human-review queue merged by stable source item id

The source JSON includes a `report.acceptance` object with the minimum item
threshold, pass/fail state, and blocking `reasons` (count below minimum, missing
metrics, or video items without `localPath`). Degraded upstream sources are
reported separately in `report.warnings` and do **not** fail acceptance — a run
that still returns ≥50 deduped items with metrics passes even if Reddit is
unreachable. Per-listing Reddit diagnostics stay in `sourceEvents.reddit`.

Connectors that feed a default run:

- **Reddit** — public `.json` listings (`top/day`, `top/week`, `rising`) across
  the configured AI subreddits. Blocked by HTTP 403 on some datacenter networks.
- **X** — imported from the `out/automation/candidates.json` scout queue (unless
  `--no-x-queue`), plus `--x-url <status-url>` for one-off enrichment.
- **YouTube** — real `yt-dlp` flat-playlist search for AI-demo shorts (all video,
  duration-capped so they download cleanly). Disable with `--no-youtube`; tune
  with `--youtube-query "<search>"` (repeatable), `--youtube-limit <n>`, or the
  `YOUTUBE_SOURCE_QUERIES` env override (comma separated).
- **Product Hunt / Hugging Face Spaces** — stubs behind the same `SourceItem`
  interface, ready to implement.

Because YouTube is reachable where Reddit is blocked, the default `source` run
(X queue + YouTube) clears the ≥50-item bar on its own. Use `--no-top-replies`
for cheap dry runs that skip Reddit comment and X quote/reply lookups.

Ranking weights live in `ranking/weights.json` and are read on each rank run, so
they can change without code edits. Set `GEMINI_API_KEY` or `GOOGLE_API_KEY` for
LLM spectacle scoring; without it, the pipeline uses a deterministic local
fallback so dry runs and tests stay stable. Set `EMBEDDING_PROVIDER=openai` and
`OPENAI_API_KEY` only if you want remote title embeddings; local hash embeddings
are the default.

Media download shells out to the repo's existing `uv run yt-dlp` dependency and
merges Reddit split audio/video with ffmpeg. By default, video candidates whose
download fails are reported in `mediaFailures` and are not written to seen-state,
so a later run can retry them. Pass `--allow-missing-media` only for exploratory
runs where ranking a video without `media.localPath` is acceptable. Reddit may
return HTTP 403 from some networks even for public `.json` listings; those
listings are skipped and logged instead of aborting the run.

Reddit request overrides for accepted networks or authenticated public JSON:

```sh
REDDIT_USER_AGENT="carousel-app-ai-news-bot/0.1 by u/<username>"
REDDIT_COOKIE="reddit_session=..."
REDDIT_AUTHORIZATION="Bearer ..."
REDDIT_JSON_BASE_URL="https://www.reddit.com"
```

## Publishing

### Instagram Graph API

`instagram_publish.py` publishes a generated manifest. Instagram needs public
HTTPS URLs for every media item, so the helper can upload local slides to
Cloudflare R2 first.

```sh
uv run python instagram_publish.py out/x_carousel/manifest.json --upload-r2 --dry-run
uv run python instagram_publish.py out/x_carousel/manifest.json --upload-r2
```

Live publishing retries failed pre-publish media container processing twice by
default. These retries create fresh Instagram containers after errors such as
`Media upload has failed`, but they stop before retrying a failed
`media_publish` call to avoid duplicate posts. Tune or disable this with:

```sh
uv run python instagram_publish.py out/x_carousel/manifest.json \
  --upload-r2 \
  --publish-retries 0
```

Required for R2 upload:

```sh
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=...
R2_PUBLIC_BASE_URL=https://...
```

Required for live Instagram publishing:

```sh
INSTAGRAM_USER_ID=178414...
INSTAGRAM_ACCESS_TOKEN=...
INSTAGRAM_GRAPH_DOMAIN=instagram
```

`INSTAGRAM_USER_ID` can also come from the manifest channel's
`publishing.instagram_user_id`. `META_SYSTEM_USER_ACCESS_TOKEN` is preferred over
`INSTAGRAM_ACCESS_TOKEN` when both are set.

Captions default to `instagram_caption` from the manifest, then to topic plus
source URL. Override when needed:

```sh
uv run python instagram_publish.py out/x_carousel/manifest.json \
  --dry-run \
  --media-url 1=https://cdn.example.com/slide_01.mp4 \
  --media-url slide_02.png=https://cdn.example.com/slide_02.png \
  --caption-file caption.txt
```

The publisher writes `instagram_publish.json` beside the manifest.

## Manifests And Outputs

Every builder writes a `manifest.json` with the ordered `slides` list, source
metadata, `channel_id`, and generated `instagram_caption` when available.
The Instagram publisher reads that manifest, validates public media URLs, and writes a
report next to it:

- `instagram_publish.json`
- `run_manifest.json` for weekly verification

Generated files live in `out/` and are safe to delete between runs.
