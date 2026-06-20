# Carousel Automation

Render branded Instagram-style carousel assets from X posts, web articles, X
Articles, weekly story queues, and source videos. The project is channel-based:
branding, language, audience, handle, and voice all come from one channel config.

Generated media and manifests are written under `out/`, which is ignored by git.

## What It Builds

| Workflow | Script | Default output | Use it for |
| --- | --- | --- | --- |
| X post/thread | `build_x_carousel.py` | `out/x_carousel/` | One X status URL, with optional same-author thread posts and video slides |
| Web article | `build_article_carousel.py` | `out/article_carousel/` | One long-form article URL or local HTML file |
| X Article | `build_x_article_carousel.py` | `out/x_article_carousel/` | Long-form X notes/articles behind a status URL |
| Weekly roundup | `build_weekly_carousel.py` | `out/weekly_carousel/` | A ranked set of top AI stories from the scout queue or an input JSON |
| Daily AI news | `build_daily_carousel.py` | `out/daily_carousel/` | 30+ RSS feeds → 5-story carousel (cover + story slides + CTA) |
| Cover art | `generate_cover.py` | `out/cover_<slug>.png` | Standalone channel-branded cover images |
| Video slide | `build_video_slide.py` | `out/video_slide_02.mp4` | A local video, remote video, or X video inside the branded carousel frame |

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

Install `ffmpeg` locally if you need video posts or `build_video_slide.py`.

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

- `slide_01.png`: channel-branded title/hook slide
- `slide_02.png` and onward: selected high-signal article sections
- `manifest.json`: slide order, source metadata, curation backend, selected scores
- `source_article.json`: extracted blocks and candidate scores for review

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

### Daily AI News

```sh
uv run python build_daily_carousel.py
uv run python build_daily_carousel.py --channel aibrief_jp --max-stories 5
```

Pulls from the same 30+ RSS/sitemap/API feeds as VCPH OS, scores and diversifies
stories (2 international + 2 PH + 1 workforce). Renders a multi-slide carousel:

- **Cover**: repo-local `daily_drop_cover.py` builds a text-free VibeCoders PH Daily Drop cover photo with `gpt-image-2` for 5-story runs; slide 1 owns the headline, story list, logo, progress, and swipe cue
- **Voice**: Gemini rewrites the cover, story copy, and caption in channel voice; Taglish channels fail instead of silently falling back when Gemini is unavailable
- **Story slides**: article image (scraped og:image or AI-generated) + Gemini headline + body
- **CTA**: follow + save

```sh
# Full build with Gemini voice + images
# Needs GOOGLE_API_KEY or GEMINI_API_KEY for voice, OPENAI_API_KEY for images
uv run python build_daily_carousel.py

# Text-only fallback with Gemini voice
uv run python build_daily_carousel.py --no-images

# Diagnostic raw-copy fallback only; not for VibeCoders PH publishing
uv run python build_daily_carousel.py --no-images --no-voice

# Dry run: fetch and score only
uv run python build_daily_carousel.py --dry-run

# Quick smoke test
uv run python build_daily_carousel.py --max-stories 2 --no-voice --no-x-trending

# Without X trending slot
uv run python build_daily_carousel.py --no-x-trending

# Custom source registry or dedupe DB
uv run python build_daily_carousel.py --registry custom_feeds.json --db-path custom.db
```

Output lands in `out/daily_carousel/`: `slide_01.png` through `slide_N.png`,
`manifest.json`, and `posted.db` (deduplication). The manifest is compatible
with `instagram_publish.py`.

The daily workflow is self-contained in this repo. The feed pipeline is
`vcph_feed_pipeline.py`, the cover engine is `daily_drop_cover.py`, the source
registry is `vcph_source_registry.json`, and the VibeCodersPH logo asset is
`assets/vibecodersph_logo.png`. Team members only need their own repo-local
`.env` credentials for Gemini/OpenAI/xAI.

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
  --media-url 1=https://cdn.example.com/slide_01.png \
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
