# Carousel Automation

This repo renders branded carousel assets from X posts, articles, and source
media. The default brand is `vibecodersph`, but branding, language, and voice are
swappable as one bundle per **channel** (see [Channels](#channels-branding--language--voice)):

- `out/x_carousel/` for one-URL X/Twitter carousels
- `out/article_carousel/` for one-URL article carousels
- `out/video_slide_02.mp4` for a branded video page inside a carousel

## Channels (branding + language + voice)

A **channel** bundles everything that makes output specific to one brand and one
language at the same time: the slide branding (colors, type, layout), the account
handle shown on slides, the language the AI writes copy in, the audience it writes
for, and the brand-voice guide it follows. Switch all of it with one setting.

Channels live under `channels/`:

```
channels/
  channels.json            # { "default_channel": "vibecodersph" }  <- the default setting
  vibecodersph/channel.json # cream/ink branding, Taglish, Filipino audience
  aibrief_jp/channel.json   # shares the cream/ink branding, Japanese language + audience
  aibrief_jp/voice.md       # AI Brief JP voice guide (Japanese)
```

The active channel is resolved in priority order:

1. `--channel <id>` flag on any builder (`build_x_carousel.py`, `build_article_carousel.py`, `build_x_article_carousel.py`, `build_weekly_carousel.py`, `generate_cover.py`),
2. the `CAROUSEL_CHANNEL` environment variable,
3. `default_channel` in `channels/channels.json`,
4. the built-in `vibecodersph` fallback (which also reads the legacy root `brand.json` / `brand/VIBECODERS_IG_VOICE.md`, so existing checkouts keep working).

```sh
# Change the default for every build: edit channels/channels.json -> "default_channel"
# Or override for one run:
uv run python build_article_carousel.py "https://example.com/story" --channel aibrief_jp
CAROUSEL_CHANNEL=aibrief_jp uv run python build_x_carousel.py "https://x.com/user/status/123"

# Inspect resolved channels:
uv run python channel.py --list
uv run python channel.py aibrief_jp
```

To add a channel: copy a `channels/<id>/` folder, edit its `channel.json` (brand
colors/type + `language.name` + `language.audience`) and its voice guide markdown
(keep the `## Copy-Paste Prompt Block For Automation` section — that block is what
the pipeline injects into the cover prompt), then point `default_channel` (or
`CAROUSEL_CHANNEL`) at the new id. The AI writes cover copy and captions in the
channel's `language.name`, so a Japanese channel produces Japanese copy with no
code changes. Note: rendering non-Latin text offline needs a matching embedded
font (e.g. a Noto Sans JP woff2) alongside `assets/archivo.css`.

## One-time setup

```sh
# Core deps (Playwright, yt-dlp, openai)
uv sync
uv run python -m playwright install chromium

# Optional: OpenAI GPT Image cover art
export OPENAI_API_KEY=sk-...

# Optional: xAI Grok Imagine and xAI tweet lookup
export XAI_API_KEY=xai-...  # cover art API key
hermes auth add xai-oauth # tweet lookup via Hermes OAuth token
```

Create a local `.env` with a Google AI Studio / Gemini API key for title imagery:

```sh
GOOGLE_API_KEY=your_google_ai_studio_key
```

The X carousel workflow uses Gemini to detect the topic, write the first-slide cover copy, and identify involved companies and CEOs. The Instagram cover voice lives in `brand/VIBECODERS_IG_VOICE.md`; edit that doc to tune the Taglish/witty VibeCoders PH cover style without changing Python. GPT Image 2.0 is used for the branded first-slide artwork; Gemini is not used for image generation in this workflow. You can override the defaults when model names change:

```sh
GEMINI_TEXT_MODEL=gemini-3.5-flash
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_TITLE_IMAGE_SIZE=2048x1152
```

Gemini also writes a VibeCoders PH Instagram caption into `manifest.json` as `instagram_caption`. `instagram_publish.py` uses that generated caption by default, unless you pass `--caption` or `--caption-file`.

To pin exact cover copy for a build, pass cover overrides. The visible brand line remains `vibecodersph`:

```sh
uv run python build_x_carousel.py "https://x.com/user/status/123" \
  --cover-headline "Pati yung gumawa, [tinatamad] na mag-type." \
  --cover-swipe-line "Silipin ang cheat sheet"
```

Generated title images are cached inside the generated output folder. Make sure you have the rights to use generated or downloaded imagery in your final carousel.

## AI Cover Art

Generate vibecodersph-branded cover art from a topic using GPT Image 2.0 or Grok Imagine:

```sh
# GPT Image 2.0 (default — uses OPENAI_API_KEY)
uv run python generate_cover.py "Fable 5 changes everything"

# Gemini Nano Banana models (uses GOOGLE_API_KEY or GEMINI_API_KEY)
uv run python generate_cover.py "Fable 5 changes everything" --provider gemini --model nano-banana-pro
uv run python generate_cover.py "Fable 5 changes everything" --provider gemini --model nano-banana-2

# Grok Imagine (uses xAI OAuth or XAI_API_KEY)
uv run python generate_cover.py "Why reasoning models win" --provider xai

# Choose a visual style
uv run python generate_cover.py "The prompt" --style typographic --out out/cover.png

# Preview the prompt without generating
uv run python generate_cover.py "topic" --prompt-only
```

Styles: `abstract` (default), `typographic`, `minimal`, `illustrative`, `photo`.
The script reads `brand.json` for the vibecodersph color palette (cream paper #F4F2EC, dark ink #16140F, rust accent #C0552E) and builds a prompt that matches.

## Tweet Data via xAI

Fetch structured tweet content + metadata via xAI Responses with X search instead of brittle Playwright screenshots:

```sh
# Auth: XAI_API_KEY env var (or .env), falling back to a Hermes xAI OAuth token
uv run python fetch_tweet_data.py https://x.com/bcherny/status/2064431111154053187
uv run python fetch_tweet_data.py 2064431111154053187 --out tweet.json

# Fetch the complete same-author thread containing the tweet, in order
uv run python fetch_tweet_data.py 2064431111154053187 --thread --max-posts 12
```

Returns JSON with: id, text, author, handle, date, likes, retweets, replies, views, has_video, formatted counts, and URL. With `--thread` it returns an ordered JSON array, first post to last, restricted to the thread author's own posts.

### Thread source decision: xAI API first, Playwright as fallback

The carousel pipeline previously discovered threads only by scrolling the live X page in Playwright. That breaks for anonymous browsers (X hides thread replies behind the login wall), requires `--cookies-from-browser`, and ships no engagement metrics for thread posts. The xAI `x_search` path has none of those problems and returns structured data, so it is now the preferred thread source whenever credentials exist (`XAI_API_KEY` or Hermes OAuth). Playwright remains in two roles:

- **Fallback discovery** when no xAI credentials are configured.
- **Rendering** — embedded-post screenshots and HTML→PNG slide capture are visual jobs the API cannot do; Playwright keeps them.

The official X API was rejected: read access requires paid developer enrollment and offers no advantage over `x_search` for this workflow.

## Human-in-the-loop Story Scout

The automation front door is `story_scout.py`: it scans configured X accounts and article feeds, scores high-signal candidates, queues them for approval, and hands approved items into the matching one-URL carousel build.

Create a local source list:

```sh
cp story_sources.example.json story_sources.json
```

Run a scan:

```sh
uv run python story_scout.py scan --config story_sources.json
uv run python story_scout.py list
```

Approve and build a queued candidate:

```sh
uv run python story_scout.py approve x_abc123def0
uv run python story_scout.py approve article_abc123def0
```

The build writes to `out/automation/builds/<candidate_id>/` and records the manifest path in `out/automation/candidates.json`. X candidates dispatch to `build_x_carousel.py`; article candidates dispatch to `build_article_carousel.py`. For article candidates, tune the build with `--article-max-pages`, `--article-min-score-build`, `--article-curation-backend`, or `--article-no-title-enrichment`.

Preview the build-to-Instagram path after approval:

```sh
uv run python story_scout.py approve x_abc123def0 \
  --publish-instagram \
  --instagram-upload-r2 \
  --instagram-dry-run
```

That uploads only the rendered carousel slides listed in `manifest.json`, then writes `instagram_publish.json` next to the manifest with the exact media URL mapping and Instagram API steps. For a real publish:

```sh
uv run python story_scout.py approve x_abc123def0 \
  --publish-instagram \
  --instagram-upload-r2 \
  --instagram-media-base-url "https://cdn.example.com/vibecodersph/x_abc123def0"
```

Prefer Buffer over the direct Meta API? `--publish-buffer` uploads the rendered slides to R2 and creates a Buffer draft on the connected Instagram channel:

```sh
uv run python story_scout.py approve x_abc123def0 --publish-buffer
```

The draft waits in the Buffer dashboard for a final review before anything reaches Instagram. Use `--buffer-mode queue` to schedule into the Buffer queue instead, or `--buffer-mode now` to publish immediately. `--buffer-dry-run` writes the payload without calling Buffer, and a `buffer_publish.json` report lands next to the manifest either way. Requires `BUFFER_API_KEY` and `BUFFER_CHANNEL_ID` in `.env`.

Buffer does not support mixed-media Instagram carousels ([their docs](https://support.buffer.com/article/657-scheduling-instagram-posts-and-reels)): their API silently keeps only the video plus the last image, which then publishes as a single reel. Builds that mix video and image slides therefore abort by default when published through Buffer. Choose explicitly with `--buffer-video-strategy`: `poster` swaps each video for its poster still (image-only carousel), `reel` publishes the first video alone as a reel. For true mixed-media carousels use the Meta Graph API path (`instagram_publish.py` / `--publish-instagram`), which supports them.

Telegram approvals are optional. Configure a bot token and chat ID, then scan
with notifications. A scan now sends both high-scoring X posts from `accounts`
and high-scoring stories from `article_sources` to the same approval chat:

```sh
export TELEGRAM_BOT_TOKEN=123456:...
export TELEGRAM_CHAT_ID=123456789
uv run python story_scout.py scan --config story_sources.json --notify
uv run python story_scout.py telegram-poll --watch --publish-buffer
```

Telegram approval callbacks use the same build path as the CLI. X candidates
(`x_*`) dispatch to `build_x_carousel.py`; article candidates (`article_*`)
dispatch to `build_article_carousel.py` with the article tuning flags from the
poller command, such as `--article-max-pages`, `--article-min-score-build`, and
`--article-curation-backend`. A poller started with `--publish-buffer` turns
each approved X post or article into a built carousel plus a Buffer draft
automatically. The broader automation plan lives in `AUTOMATION_ROADMAP.md`.

## One-URL X Carousel

Drop in one X/Twitter status URL:

```sh
uv run python build_x_carousel.py "https://x.com/OpenAI/status/2061887650391625870"
```

The script writes an ordered carousel folder to `out/x_carousel`:

- `slide_01.png`: branded title/hook slide
- `slide_02.png`: branded post slide for a normal post
- `slide_02.mp4`: branded post+video slide when the post has video
- `manifest.json`: ordered slide list and source URLs

By default it tries to detect same-author thread posts and creates one post/media slide for each detected part. Thread discovery uses the xAI `x_search` API when `XAI_API_KEY` or a Hermes OAuth token is configured, and falls back to scraping the live X page with Playwright otherwise; the manifest records which backend produced the posts in `thread_source`. Use `--thread-source xai|playwright|auto` (or `X_THREAD_SOURCE`) to pin a backend, `--no-thread` to force a single-post carousel, `--max-thread-posts` to cap a long thread, or `--title` to override the generated title slide.

X sometimes hides thread replies from anonymous browsers. If a URL is part of a thread but only one post is visible, let the workflow use your logged-in browser cookies:

```sh
uv run python build_x_carousel.py "https://x.com/OpenAI/status/2061887650391625870" \
  --cookies-from-browser chrome
```

For automation triggers that should still accept only the URL, set this once in the runtime environment:

```sh
export X_COOKIES_FROM_BROWSER=chrome
```

## One-URL Article Carousel

Drop in a long-form article URL to turn only the highest-signal sections into
vibecodersph carousel pages:

```sh
uv run python build_article_carousel.py \
  "https://venturebeat.com/technology/xiaomis-new-open-source-agentic-ai-coding-harness-mimo-code-beats-claude-code-at-ultra-long-200-step-tasks"
```

The script writes to `out/article_carousel`:

- `slide_01.png`: branded title/hook slide, using the same cover system as the X workflow
- `slide_02.png` and onward: selected article signal pages
- `manifest.json`: ordered slide list, source article metadata, curation backend, and selected section scores
- `source_article.json`: extracted blocks and candidate section scores for editorial review

By default, curation uses Gemini when `GOOGLE_API_KEY` or `GEMINI_API_KEY` is
available, and falls back to local scoring otherwise. Both paths filter for
concrete facts such as benchmarks, releases, open-source details, technical
claims, quantified comparisons, and strategic implications. Tune selection with
`--max-pages`, `--min-score`, or force a backend with
`--curation-backend gemini|local|auto`.

Article feeds also run through the full automation queue. Add `article_sources`
to `story_sources.json`, scan as usual, then approve the generated `article_*`
candidate:

```sh
uv run python story_scout.py scan --config story_sources.json
uv run python story_scout.py approve article_abc123def0 --article-curation-backend local
```

## One-URL X Article Carousel

X Articles and long-form X notes are full essays published behind a single
status URL. Drop in that URL to turn the essay into a vibecodersph carousel,
picking only the highest-signal sections:

```sh
uv run python build_x_article_carousel.py "https://x.com/satyanadella/status/2066182223213293753"
uv run python build_x_article_carousel.py "https://x.com/plutos_eth/status/2066297019912610286"
```

The script writes to `out/x_article_carousel`:

- `slide_01.png`: the shared vibecodersph title cover (same brand-voice cover
  system as the X-post and web-article workflows)
- `slide_02.png` and onward: the highest-signal article sections, paraphrased
  by Gemini when `GOOGLE_API_KEY` is available, each badged `X ARTICLE`
- `manifest.json`: ordered slide list plus an `x_article` block recording the
  author handle, post date, and engagement counts (likes / reposts / views)

It fetches the verbatim long-form text via the xAI `x_search` API (the same auth
as `fetch_tweet_data.py`: `XAI_API_KEY` in `.env`, or a Hermes xAI OAuth token),
detects section headings, rebuilds the essay into the web-article `Article`
structure, and reuses that pipeline end to end -- candidate sectioning, Gemini /
local curation, the title cover, slide rendering, and the manifest.

Because X Articles are curated thought pieces rather than benchmark-dense news,
section selection defaults to a lower threshold than the web-article builder
(`--min-score 3` vs `6`). Tune with `--max-pages`, `--min-score`,
`--curation-backend gemini|local|auto`, `--title`, or `--no-title-enrichment`.
For short tweets (not long-form essays), use `build_x_carousel.py` instead.

## Weekly News Roundup Carousel

`build_weekly_carousel.py` builds a *curated "top AI news of the week"* carousel:
one cover, one slide per story, then an outro. Instead of deep-diving one URL, it
ranks the week's highest-signal stories (scored by `story_scout.py`) and turns
each into a slide.

```sh
uv run python build_weekly_carousel.py --channel vibecodersph
uv run python build_weekly_carousel.py --channel aibrief_jp --max-stories 6
```

It is fully **channel-sensitive** ([Channels](#channels-branding--language--voice)):
both channels share the cream/ink visual branding, so the same week of news speaks
witty Taglish through `vibecodersph` and Japanese through `aibrief_jp` (with
localized labels like `今週のAIニュース`) on the same look. The cover is the regular
image cover — AI editorial art with up to three portraits of the CEOs whose
companies are in that week's news. Cover copy and every story's kicker/headline/
summary are written in the channel's language by Gemini when `GOOGLE_API_KEY` is
set, with a deterministic local fallback otherwise.

Story source priority: an explicit `--input stories.json` (a list, or
`{"stories": [...]}`), otherwise the `story_scout` candidate queue
(`out/automation/candidates.json`) filtered to the last `--days` (default 7). If
the dated window is too thin, it falls back to the all-time top of the queue.

**Page cap:** Instagram carousels support up to 20 slides. A roundup spends one
on the cover and one on the outro, so stories are capped at **18** (default **7**);
`--max-stories` is clamped to 3–18. Use `--per-source` (default 2) to limit how
many stories one account can contribute, for variety. Outputs (cover, story
slides, outro, `manifest.json` with the per-story source URLs and the channel's
`instagram_caption`) go to `out/weekly_carousel`.

## Instagram Publishing

`instagram_publish.py` publishes any generated carousel manifest through the Instagram Graph API. Instagram requires a professional Instagram account, an access token with content publishing permissions, and media files that Instagram can fetch from public HTTPS URLs. Local files and `localhost` URLs cannot be published directly.

Configure credentials:

```sh
export R2_ACCOUNT_ID=...
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...
export R2_BUCKET=vibecodersph-carousel-media
export INSTAGRAM_USER_ID=178414...
export INSTAGRAM_ACCESS_TOKEN=...
export INSTAGRAM_GRAPH_DOMAIN=instagram
export INSTAGRAM_MEDIA_BASE_URL="https://pub-010164abaff84929ae890815a7290ca0.r2.dev"
```

The simplest way to get Instagram credentials is from the Meta app dashboard, not Graph API Explorer:

1. Open the app in Meta for Developers.
2. Go to **Instagram > API setup with Instagram business login**.
3. Click **Generate token** next to the Instagram professional account.
4. Copy the access token into `INSTAGRAM_ACCESS_TOKEN`.
5. Fetch the Instagram user ID:

```sh
curl "https://graph.instagram.com/v25.0/me?fields=user_id,username&access_token=$INSTAGRAM_ACCESS_TOKEN"
```

Use the returned `user_id` as `INSTAGRAM_USER_ID`. App Dashboard tokens are long-lived for about 60 days.

Upload the rendered carousel slides to R2 and preview the publish plan without calling Instagram:

```sh
uv run python instagram_publish.py out/x_carousel/manifest.json --upload-r2 --dry-run
```

Publish for real after the same R2 upload step:

```sh
uv run python instagram_publish.py out/x_carousel/manifest.json --upload-r2
```

By default the publisher uses `instagram_caption` from the manifest when present, then falls back to topic plus source URL. Use `--caption` or `--caption-file` to override it. Use repeated `--media-url` flags for per-slide URLs when the files do not share one base URL:

```sh
uv run python instagram_publish.py out/x_carousel/manifest.json --dry-run \
  --media-url 1=https://cdn.example.com/slide_01.png \
  --media-url slide_02.png=https://cdn.example.com/slide_02.png
```

The publisher writes `instagram_publish.json` beside the manifest. In dry-run mode it contains the validated media list and planned API calls; after a real publish it also records the returned Instagram media IDs and permalink lookup result.

## Branded Video Slide

Use a local video:

```sh
uv run python build_video_slide.py \
  --source assets/video_sources/example.mp4 \
  --source-label "SOURCE VIDEO" \
  --caption "The source clip stays inside the vibecodersph carousel frame."
```

Use an X/Twitter embed snippet as the full post context plus the video:

```sh
uv run python build_video_slide.py \
  --tweet-embed-file path/to/tweet_embed.html \
  --layout post-video \
  --source-label "@claudeai on X" \
  --kicker "The post"
```

The embed file can contain the raw code copied from X's embed-post feature:

```html
<blockquote class="twitter-tweet">
  <p lang="en" dir="ltr">Post text... <a href="https://t.co/example">pic.twitter.com/example</a></p>
  &mdash; Claude (@claudeai)
  <a href="https://x.com/claudeai/status/2064394146916229443">June 9, 2026</a>
</blockquote>
<script async src="https://platform.x.com/widgets.js" charset="utf-8"></script>
```

Use an X/Twitter post URL:

```sh
uv run python build_video_slide.py \
  --source "https://x.com/claudeai/status/2064394146916229443" \
  --source-label "@claudeai on X" \
  --caption "Claude's launch video, framed as a carousel receipt."
```

If X gates the media, pass browser cookies through to `yt-dlp`:

```sh
uv run python build_video_slide.py \
  --source "https://x.com/claudeai/status/2064394146916229443" \
  --cookies-from-browser chrome \
  --source-label "@claudeai on X" \
  --caption "Claude's launch video, framed as a carousel receipt."
```

Video outputs:

- `out/video_frame_02.png`: the vibecodersph frame used behind the clip
- `out/video_slide_02.mp4`: the carousel-ready MP4
- `out/video_slide_02_poster.png`: first-frame poster for previews
- `out/video_slide_02.json`: source and render manifest

The default video fit is `contain`, preserving the full source clip inside the branded media well. Use `--fit cover` or `--video-fit cover` when you want the clip to fill the well by cropping.
