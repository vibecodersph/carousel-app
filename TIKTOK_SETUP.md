# TikTok Automation Setup

This mirrors the Instagram reel pipeline (`REEL_SCHEDULING.md`) for TikTok. The
**same clips, the same channel slots, the same ledger model** drive both — only
the publisher and the auth differ. You have not set up anything on TikTok yet,
so this doc is the from-zero checklist. Everything except the live publish step
already works in `--dry-run` today.

## How it maps to the reel pipeline

| Piece | Instagram | TikTok |
| --- | --- | --- |
| Publisher | `instagram_publish.py` | `tiktok_publish.py` |
| Auth | long-lived Meta system token | per-user OAuth (`tiktok_auth.py`) |
| Channel config block | `publishing.instagram_reels` | `publishing.tiktok` |
| Ledger db | `state/reels.db` | `state/tiktok.db` |
| Publish report | `instagram_publish.json` | `tiktok_publish.json` |
| Scheduler | `reel_scheduler.py … ` | `reel_scheduler.py … --platform tiktok` |

The same `reel.<lang>.<channel>.mp4` clips feed both. A clip can be tracked
independently on each platform because each platform has its own ledger db, so
publishing to TikTok never touches the Instagram ledger.

---

## Step 1 — Register a TikTok app

1. Sign in at <https://developers.tiktok.com/> and create an app.
2. Add the **Content Posting API** product to the app, and turn on **Direct
   Post** configuration.
3. Add the scopes: `video.publish` (direct post), `video.upload` (inbox
   drafts), and `user.info.basic`. Add `video.list` too if you want analytics
   (`sync-insights`).
4. Add a **redirect URI** (e.g. `https://your.site/tiktok/callback`). It only
   has to receive the `?code=...` redirect — even a page you read the URL off of
   by hand is fine to start.
5. Copy the **Client key** and **Client secret**.

### ⚠️ The unaudited-app limit (read this)

> **All content posted by an unaudited client is forced to private viewing
> (`SELF_ONLY`).** TikTok rejects public direct posts with
> `unaudited_client_can_only_post_to_private_accounts` until your app passes
> TikTok's audit.

That is why the default mode here is **`inbox`**, not `direct`:

- **`inbox`** (default) — the reel is uploaded as a *draft* into the account's
  TikTok notifications. You open the app, the video is waiting, you paste the
  caption (written to `caption.txt` next to the manifest), choose **public**,
  and tap post. This yields a public post on a brand-new unaudited app.
- **`direct`** — posts straight to the account with the caption pre-filled, but
  on an unaudited app it can only be `SELF_ONLY` (private). Switch your channel
  to `"mode": "direct"` + `"privacy_level": "PUBLIC_TO_EVERYONE"` only **after**
  your app is audited.

---

## Step 2 — Put credentials in `.env`

Add (per channel, suffix = the channel id uppercased; or unsuffixed as a
global). See the block already appended to `.env`:

```sh
TIKTOK_CLIENT_KEY=awxxxxxxxxxxxx
TIKTOK_CLIENT_SECRET=xxxxxxxxxxxxxxxx
TIKTOK_REDIRECT_URI=https://your.site/tiktok/callback

# filled in by tiktok_auth.py after you authorize:
TIKTOK_ACCESS_TOKEN_VIBECODERSPH=
TIKTOK_ACCESS_TOKEN_AIBRIEF_JP=
```

Per-channel keys (`TIKTOK_CLIENT_KEY_VIBECODERSPH`, etc.) are supported if each
channel is a different TikTok app/account; otherwise the unsuffixed keys apply
to every channel.

---

## Step 3 — Authorize each channel (OAuth)

```sh
# 1. Print the consent URL, open it, authorize the target TikTok account
uv run python tiktok_auth.py url --channel vibecodersph

# 2. Copy the `code` from the redirect, exchange it for tokens
uv run python tiktok_auth.py exchange <code> --channel vibecodersph
#    -> saves state/tiktok_tokens.json and prints the .env line to paste

# 3. Smoke-test the token (this is your "is it wired up" check)
uv run python tiktok_auth.py creator-info --channel vibecodersph
#    -> prints the account's allowed privacy levels.
#       ["SELF_ONLY"] only  => app still unaudited => use inbox mode.

# Access tokens last ~24h. Refresh when they expire:
uv run python tiktok_auth.py refresh --channel vibecodersph
```

Tokens are stored in `state/tiktok_tokens.json` (gitignored) **and** echoed as
an `.env` line. The scheduler reads the token from `.env`; the JSON store keeps
the refresh token for `refresh`.

---

## Step 4 — Plan and post (same commands, `--platform tiktok`)

```sh
# Discover clips into the TikTok ledger (state/tiktok.db)
uv run python reel_scheduler.py scan \
  /Users/aiagent/GitHub/reel-app/outputs/PQU9o_5rHC4/clips --platform tiktok

# Fill each channel's tiktok slots with the new clips
uv run python reel_scheduler.py plan-ledger \
  /Users/aiagent/GitHub/reel-app/outputs/PQU9o_5rHC4/clips 2026-06-24 --platform tiktok

# Preview every due job without calling TikTok (writes tiktok_publish.json)
uv run python reel_scheduler.py run-due --platform tiktok --dry-run --all

# Post for real (inbox drafts by default; per-channel mode/source/privacy in channel.json)
uv run python reel_scheduler.py run-due --platform tiktok

# Operator views (read the tiktok.db, not reels.db)
uv run python reel_scheduler.py status --platform tiktok
uv run python reel_scheduler.py report --platform tiktok --out out/tiktok_report.html
uv run python reel_scheduler.py sync-insights --platform tiktok   # needs video.list scope
```

You can also post a single manifest directly, bypassing the ledger:

```sh
uv run python tiktok_publish.py path/to/manifest.json --dry-run
uv run python tiktok_publish.py path/to/manifest.json --mode inbox
```

### Cadence (`publishing.tiktok` in each `channel.json`)

Already added to both channels — tune the slots/timezone as you like:

```json
"tiktok": {
  "mode": "inbox",              // inbox (default) | direct (after audit)
  "source": "file",            // file (no domain verify) | pull (R2, needs verify)
  "privacy_level": "SELF_ONLY",// direct-mode only; unaudited apps must be SELF_ONLY
  "timezone": "Asia/Manila",
  "slots": ["12:30", "19:00", "21:30"],
  "jitter_minutes": 7,
  "caption_context": "...",
  "caption_cta": "...",
  "hashtags": ["#AI", "#fyp"]
}
```

CLI flags `--tiktok-mode`, `--tiktok-source`, `--tiktok-privacy` override the
channel block for a single `run-due`.

---

## File upload vs pull-from-URL (`--source`)

- **`file`** (default): `tiktok_publish.py` uploads the local mp4 to TikTok
  directly (chunked `FILE_UPLOAD`). **No domain verification needed** — best to
  start with.
- **`pull`**: the reel is uploaded to Cloudflare R2 first (the exact path
  Instagram uses), then handed to TikTok as a `PULL_FROM_URL`. This requires
  verifying your R2 public domain in the TikTok developer portal (URL prefix
  ownership). Use it once you've verified the domain and want TikTok to fetch
  rather than receive bytes.

---

## After your app is audited

1. Re-run `tiktok_auth.py creator-info` — `privacy_level_options` will now
   include `PUBLIC_TO_EVERYONE`.
2. In each `channel.json` `tiktok` block set `"mode": "direct"` and
   `"privacy_level": "PUBLIC_TO_EVERYONE"`.
3. `run-due --platform tiktok` now posts publicly with the caption pre-filled —
   a full auto-publish mirror of the Instagram flow.

---

## API reference

- Content Posting API overview: <https://developers.tiktok.com/doc/content-posting-api-get-started>
- Direct Post: <https://developers.tiktok.com/doc/content-posting-api-reference-direct-post>
- Upload (inbox) + media transfer: <https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide>
- Status fetch: `POST https://open.tiktokapis.com/v2/post/publish/status/fetch/`
- OAuth token: `POST https://open.tiktokapis.com/v2/oauth/token/`
