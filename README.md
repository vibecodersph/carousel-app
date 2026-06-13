# LLMAW Carousel Automation

This repo renders LLMAW-branded carousel assets from HTML:

- `out/slide_NN.png` for static carousel pages
- `out/carousel.pptx` for Canva import
- `out/video_slide_02.mp4` for a branded video page inside a carousel

## One-time setup

```sh
# Core deps (Playwright, python-pptx, yt-dlp, openai)
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

The X carousel workflow uses Gemini to detect the topic and identify involved companies and CEOs. It uses GPT Image 2.0 for the branded first-slide cover; Gemini is not used for image generation in this workflow. You can override the defaults when model names change:

```sh
GEMINI_TEXT_MODEL=gemini-3.5-flash
OPENAI_IMAGE_MODEL=gpt-image-2
OPENAI_TITLE_IMAGE_SIZE=2048x1152
```

Generated title images are cached inside the generated output folder. Make sure you have the rights to use generated or downloaded imagery in your final carousel.

## AI Cover Art

Generate LLMAW-branded cover art from a topic using GPT Image 2.0 or Grok Imagine:

```sh
# GPT Image 2.0 (default — uses OPENAI_API_KEY)
uv run python generate_cover.py "Fable 5 changes everything"

# Gemini Nano Banana models (uses GOOGLE_API_KEY or GEMINI_API_KEY)
uv run python generate_cover.py "Fable 5 changes everything" --provider gemini --model nano-banana-pro
uv run python generate_cover.py "Fable 5 changes everything" --provider gemini --model nano-banana-2

# Grok Imagine (uses xAI OAuth or XAI_API_KEY)
uv run python generate_cover.py "Why reasoning models win" --provider xai

# Choose a visual style
uv run python generate_cover.py "The prompt" --style typographic --out assets/cover.png

# Preview the prompt without generating
uv run python generate_cover.py "topic" --prompt-only
```

Styles: `abstract` (default), `typographic`, `minimal`, `illustrative`, `photo`.
The script reads `brand.json` for the LLMAW color palette (cream paper #F4F2EC, dark ink #16140F, rust accent #C0552E) and builds a prompt that matches.

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

The automation front door is `story_scout.py`: it scans configured X accounts, scores high-signal posts, queues candidates for approval, and can hand approved posts into the existing one-URL carousel build.

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
```

The build writes to `out/automation/builds/<candidate_id>/` and records the manifest path in `out/automation/candidates.json`.

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
  --instagram-media-base-url "https://cdn.example.com/llmaw/x_abc123def0"
```

Prefer Buffer over the direct Meta API? `--publish-buffer` uploads the rendered slides to R2 and creates a Buffer draft on the connected Instagram channel:

```sh
uv run python story_scout.py approve x_abc123def0 --publish-buffer
```

The draft waits in the Buffer dashboard for a final review before anything reaches Instagram. Use `--buffer-mode queue` to schedule into the Buffer queue instead, or `--buffer-mode now` to publish immediately. `--buffer-dry-run` writes the payload without calling Buffer, and a `buffer_publish.json` report lands next to the manifest either way. Requires `BUFFER_API_KEY` and `BUFFER_CHANNEL_ID` in `.env`.

Buffer does not support mixed-media Instagram carousels ([their docs](https://support.buffer.com/article/657-scheduling-instagram-posts-and-reels)): their API silently keeps only the video plus the last image, which then publishes as a single reel. Builds that mix video and image slides therefore abort by default when published through Buffer. Choose explicitly with `--buffer-video-strategy`: `poster` swaps each video for its poster still (image-only carousel), `reel` publishes the first video alone as a reel. For true mixed-media carousels use the Meta Graph API path (`instagram_publish.py` / `--publish-instagram`), which supports them.

Telegram approvals are optional. Configure a bot token and chat ID, then scan with notifications:

```sh
export TELEGRAM_BOT_TOKEN=123456:...
export TELEGRAM_CHAT_ID=123456789
uv run python story_scout.py scan --config story_sources.json --notify
uv run python story_scout.py telegram-poll --watch --publish-buffer
```

Telegram approval callbacks use the same build path as the CLI, so a poller started with `--publish-buffer` turns each Telegram approval into a built carousel plus a Buffer draft automatically. The broader automation plan lives in `AUTOMATION_ROADMAP.md`.

## Static PNG/PPTX build

```sh
uv run python build.py
```

Use retina PNGs when needed:

```sh
uv run python build.py --scale 2
```

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
LLMAW carousel pages:

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

## Instagram Publishing

`instagram_publish.py` publishes any generated carousel manifest through the Instagram Graph API. Instagram requires a professional Instagram account, an access token with content publishing permissions, and media files that Instagram can fetch from public HTTPS URLs. Local files and `localhost` URLs cannot be published directly.

Configure credentials:

```sh
export R2_ACCOUNT_ID=...
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...
export R2_BUCKET=llmaw-carousel-media
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

Use `--caption` or `--caption-file` to override the default caption. Use repeated `--media-url` flags for per-slide URLs when the files do not share one base URL:

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
  --caption "The source clip stays inside the LLMAW carousel frame."
```

Use an X/Twitter embed snippet as the full post context plus the video:

```sh
uv run python build_video_slide.py \
  --tweet-embed-file assets/tweet_embed.html \
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

## Full Build Plus Video

`build.py` keeps the static PNG/PPTX path and can also emit the MP4 slide in one command:

```sh
uv run python build.py \
  --video-tweet-embed-file assets/tweet_embed.html \
  --video-layout post-video \
  --video-source-label "@claudeai on X" \
  --video-kicker "The post"
```

Video outputs:

- `out/video_frame_02.png`: the LLMAW frame used behind the clip
- `out/video_slide_02.mp4`: the carousel-ready MP4
- `out/video_slide_02_poster.png`: first-frame poster for previews
- `out/video_slide_02.json`: source and render manifest

The default video fit is `contain`, preserving the full source clip inside the branded media well. Use `--fit cover` or `--video-fit cover` when you want the clip to fill the well by cropping.
