#!/usr/bin/env python3
"""Turn a video post into a branded, Instagram-ready 9:16 reel for one channel.

The reel uses the "tech-news card" layout: the channel logo + name + handle on top,
a channel-language headline, the source video *contained* on a channel-themed surface
(so nothing is ever cropped), and a view-count chip. Everything — logo, language,
colours — comes from the active channel; nothing is hardcoded.

Like the other channel-sensitive builders, the active channel is chosen with
``--channel`` (or CAROUSEL_CHANNEL); every ``load_channel()`` call then resolves to
it. See ``channel.py``. The headline is written in the channel's language with xAI
(Japanese for aibrief_jp, Taglish/English for vibecodersph); pass ``--headline`` to
override, or it falls back to the post text when no xAI credential is present.

This is a *render-only* feature: it downloads and composes, it never publishes.

    uv run python build_reel.py --source https://x.com/HighSignal_AI/status/2068287838328959444 --channel aibrief_jp
    uv run python build_reel.py --source clip.mp4 --channel vibecodersph --headline "Ito ang totoong gamit ng air purifier"
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

from build_video_slide import (
    OUT,
    clean_post_text,
    compact_number,
    download_video,
    extract_status_id,
    fetch_video_metadata,
    normalize_source,
    run,
)
from channel import Channel, load_channel
from fetch_tweet_data import load_env_file, resolve_xai_token, xai_responses_text

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
FONTS = ASSETS / "archivo.css"
VIDEO_ASSETS = ASSETS / "video_sources"
DEFAULT_OUT = OUT / "reels"

# Instagram reel canvas: 9:16.
REEL_W, REEL_H = 1080, 1920

# The media is contained inside this box and TOP-aligned at (x, y): the top of the
# video sits a fixed distance below the headline regardless of the source aspect, so
# landscape and portrait share the same header-to-video gap. Width is kept wide and
# the height bounds how far it can grow down toward the centred view chip.
MEDIA_REGION = (20, 544, 1040, 1276)

# The headline's bottom edge is pinned here; the video top is a fixed gap below it.
HEADLINE_BOTTOM = 500

# Dark "tech card" surface, used when a channel omits a brand.reel theme block.
DEFAULT_THEME = {
    "background": "#0B0B0C",
    "text": "#FFFFFF",
    "muted": "#8B98A5",
    "verified": "#1D9BF0",
    "accent": "#C0552E",
}


def handle_slug(value: str) -> str:
    """Filesystem-safe handle fragment, e.g. '@HighSignal_AI' -> 'highsignal_ai'."""
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "", (value or "").lstrip("@"))
    return cleaned.lower() or "source"


def reel_theme(channel: Channel) -> dict[str, str]:
    """Resolve the reel surface theme from the channel brand, with dark defaults."""
    theme = (channel.brand or {}).get("reel") or {}
    return {**DEFAULT_THEME, **{k: v for k, v in theme.items() if isinstance(v, str)}}


def probe_dimensions(video_path: Path) -> tuple[int, int]:
    """Return (width, height) of the first video stream via ffprobe."""
    ffprobe = _require("ffprobe")
    result = run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0",
            str(video_path),
        ],
        capture=True,
    )
    width, height = (int(part) for part in result.stdout.strip().splitlines()[0].lower().split("x")[:2])
    return width, height


def probe_duration(video_path: Path) -> float:
    """Return the duration of the video in seconds (0.0 if unknown)."""
    ffprobe = _require("ffprobe")
    result = run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
        capture=True,
    )
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise SystemExit(f"{binary} is required to build reels")
    return path


def media_rect(video_w: int, video_h: int) -> tuple[int, int, int, int]:
    """Contain the source inside MEDIA_REGION; return an even-sized, centred rect."""
    rx, ry, rw, rh = MEDIA_REGION
    scale = min(rw / video_w, rh / video_h)
    mw = max(2, int(round(video_w * scale)) & ~1)  # force even for yuv420p
    mh = max(2, int(round(video_h * scale)) & ~1)
    mx = rx + (rw - mw) // 2  # centre horizontally
    my = ry  # top-align: fixed distance below the headline
    return mx, my, mw, mh


def clean_headline(raw: str) -> str:
    """Take the first line of a model reply and strip quotes/labels/trailing marks."""
    line = ""
    for candidate in raw.splitlines():
        candidate = candidate.strip()
        if candidate:
            line = candidate
            break
    line = re.sub(r'^(?:headline|タイトル|見出し)\s*[:：]\s*', "", line, flags=re.IGNORECASE)
    line = line.strip().strip('"“”「」\'')
    return re.sub(r"\s+", " ", line).strip(" .,;:—-")


def generate_headline(channel: Channel, post_text: str, override: str | None) -> str:
    """Write a feed headline in the channel's language; fall back to the post text."""
    if override is not None and override.strip():
        return clean_headline(override)
    text = re.sub(r"\s+", " ", html.unescape(post_text or "")).strip()
    token = resolve_xai_token(required=False)
    if not token or not text:
        # No model available: clamp the post text so it still reads as a hook.
        if len(text) > 90:
            text = text[:90].rsplit(" ", 1)[0].rstrip(",.;:—- ") + "…"
        return text or channel.brand_name
    voice = (channel.voice_prompt or "")[:700]
    prompt = (
        f"You write punchy headlines for a top tech-news video account.\n"
        f"Write ONE headline in {channel.language_name} for the brand "
        f'"{channel.brand_name}" (audience: {channel.audience}). It introduces the '
        f"video below in the feed. Make it concrete and curiosity-driving, natural and "
        f"idiomatic in {channel.language_name}, 6-12 words. No hashtags, no quotes, no "
        f"emoji, no trailing punctuation. Output ONLY the headline.\n\n"
        f"Voice/tone guide:\n{voice}\n\nSource post:\n{text}"
    )
    print(f"[reel] writing {channel.language_name} headline with xAI")
    try:
        raw = xai_responses_text(prompt, token, timeout=60)
    except SystemExit as exc:
        print(f"[reel] headline generation failed ({exc}); using post text", file=sys.stderr)
        return clean_headline(text)
    return clean_headline(raw) or clean_headline(text)


def headline_size(text: str) -> int:
    """Scale the headline down as it grows so up to ~3 lines stay inside its area."""
    n = len(text)
    if n > 90:
        return 46
    if n > 64:
        return 52
    if n > 40:
        return 58
    return 66


def overlay_html(channel: Channel, headline: str, views: str, rect: tuple[int, int, int, int]) -> str:
    """The top design layer: themed surface with a transparent rounded media window."""
    font_css = FONTS.read_text()
    brand = channel.brand or {}
    typography = brand.get("typography") or {}
    heading_font = typography.get("heading_font") or "Archivo"
    theme = reel_theme(channel)

    mx, my, mw, mh = rect
    name = html.escape(channel.account_name or channel.brand_name)
    handle = html.escape(channel.handle)
    safe_headline = html.escape(headline)
    size = headline_size(headline)
    logo_uri = channel.logo_path.as_uri() if channel.logo_path and channel.logo_path.exists() else ""
    logo_markup = f'<img src="{logo_uri}" alt="">' if logo_uri else ""

    views_markup = ""
    if views:
        views_markup = f"""
  <div class="views">
    <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
      <path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z" stroke="{theme['text']}" stroke-width="1.8"/>
      <circle cx="12" cy="12" r="3.2" stroke="{theme['text']}" stroke-width="1.8"/>
    </svg>
    <span>{html.escape(views)}</span>
  </div>"""

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
{font_css}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ background: transparent; }}
body {{ font-family: {heading_font}, 'Archivo', sans-serif; }}

.overlay {{
  position: relative;
  width: {REEL_W}px;
  height: {REEL_H}px;
  background: transparent;
  color: {theme['text']};
  overflow: hidden;
}}

/* A transparent rounded window; its huge box-shadow paints the themed surface over
   everything else, so the video shows only inside the rounded rect. */
.window {{
  position: absolute;
  left: {mx}px; top: {my}px; width: {mw}px; height: {mh}px;
  border-radius: 28px;
  background: transparent;
  box-shadow: 0 0 0 1.5px rgba(255,255,255,0.10), 0 0 0 4000px {theme['background']};
  z-index: 1;
}}

.brand {{
  position: absolute;
  top: 60px; left: 60px; right: 60px;
  display: flex; flex-direction: column; align-items: center; gap: 14px;
  text-align: center;
  z-index: 2;
}}
.brand .avatar {{
  width: 116px; height: 116px; border-radius: 50%;
  overflow: hidden; background: #F4F2EC; flex: 0 0 auto;
}}
.brand .avatar img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
.brand .name {{
  display: flex; align-items: center; justify-content: center; gap: 12px;
  font-size: 42px; font-weight: 800; color: {theme['text']}; line-height: 1.1;
}}
.brand .check {{
  width: 34px; height: 34px; border-radius: 50%;
  background: {theme['verified']}; color: #fff; flex: 0 0 auto;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 22px; font-weight: 900;
}}
.brand .handle {{
  font-size: 30px; font-weight: 600; color: {theme['muted']};
}}

.headline {{
  position: absolute;
  bottom: {REEL_H - HEADLINE_BOTTOM}px; left: 60px; right: 60px;
  max-height: 200px; overflow: hidden;
  z-index: 2; text-align: center;
  font-size: {size}px; font-weight: 700; line-height: 1.18;
  letter-spacing: -0.01em; color: {theme['text']};
}}

.views {{
  position: absolute;
  left: 0; right: 0; bottom: 66px;
  display: flex; align-items: center; justify-content: center; gap: 14px;
  z-index: 2;
  color: {theme['text']};
}}
.views span {{ font-size: 34px; font-weight: 800; letter-spacing: 0.01em; }}
</style>
</head>
<body>
<div class="overlay">
  <div class="window"></div>
  <div class="brand">
    <div class="avatar">{logo_markup}</div>
    <div class="id">
      <div class="name">{name}<span class="check">✓</span></div>
      <div class="handle">{handle}</div>
    </div>
  </div>
  <div class="headline">{safe_headline}</div>{views_markup}
</div>
</body>
</html>
"""


def render_overlay(
    channel: Channel,
    headline: str,
    views: str,
    rect: tuple[int, int, int, int],
    overlay_out: Path,
) -> Path:
    """Render the transparent themed overlay PNG with Playwright (Chrome)."""
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "playwright is not installed. Use `uv run python build_reel.py ...` "
            "or install the dependencies in pyproject.toml."
        ) from exc

    overlay_out.parent.mkdir(parents=True, exist_ok=True)
    html_path = overlay_out.parent / "_reel_overlay.html"
    html_path.write_text(overlay_html(channel, headline, views, rect))

    print(f"[reel 3/6] rendering brand overlay -> {overlay_out}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(viewport={"width": REEL_W, "height": REEL_H}, device_scale_factor=1)
            page.goto(html_path.as_uri())
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(300)
            page.locator(".overlay").screenshot(path=str(overlay_out), omit_background=True)
            browser.close()
    except Exception as exc:
        raise SystemExit(
            "could not render the overlay. If this is a fresh setup, run "
            "`uv run python -m playwright install chromium` once."
        ) from exc
    return overlay_out


def compose_reel(
    source_video: Path,
    overlay: Path,
    out_path: Path,
    *,
    rect: tuple[int, int, int, int],
    background: str,
    fps: int,
) -> Path:
    """Contain the source in the media window on the themed surface, then add brand."""
    ffmpeg = _require("ffmpeg")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mx, my, mw, mh = rect
    filter_complex = (
        f"color=c={background}:s={REEL_W}x{REEL_H}[bg];"
        f"[0:v]scale={mw}:{mh},setsar=1[vid];"
        f"[bg][vid]overlay={mx}:{my}:shortest=1[stage];"
        f"[stage][1:v]overlay=0:0,fps={fps},format=yuv420p[v]"
    )
    print(f"[reel 4/6] composing reel ({mw}x{mh} media on {background}) -> {out_path}")
    run(
        [
            ffmpeg, "-y",
            "-i", str(source_video),
            "-loop", "1", "-framerate", str(fps), "-i", str(overlay),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "19", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", "-shortest",
            str(out_path),
        ]
    )
    return out_path


def extract_qa_frames(video_path: Path, out_dir: Path) -> list[Path]:
    """Pull sample frames at ~10/50/90% of the duration for the visual QA pass."""
    ffmpeg = _require("ffmpeg")
    duration = probe_duration(video_path)
    frames: list[Path] = []
    print("[reel 6/6] extracting QA frames (10/50/90%)")
    for pct in (10, 50, 90):
        timestamp = duration * pct / 100 if duration else 0.0
        frame_path = out_dir / f"qa_frame_{pct:02d}.png"
        run([ffmpeg, "-y", "-ss", f"{timestamp:.3f}", "-i", str(video_path),
             "-frames:v", "1", "-update", "1", str(frame_path)])
        frames.append(frame_path)
    return frames


def build_reel(
    *,
    source: str | None,
    channel: Channel,
    out_dir: Path,
    headline: str | None,
    fps: int,
    cookies_from_browser: str | None,
) -> dict[str, object]:
    resolved = normalize_source(source, None)

    metadata: dict[str, object] = {}
    status_id = ""
    source_handle = ""
    if isinstance(resolved, str):
        status_id = extract_status_id(resolved) or ""
        print("[reel 1/6] fetching post metadata")
        try:
            metadata = fetch_video_metadata(resolved, cookies_from_browser)
        except SystemExit:
            metadata = {}
        source_handle = str(metadata.get("uploader_id") or "")
        source_video = download_video(resolved, VIDEO_ASSETS, cookies_from_browser)
    else:
        source_video = resolved
        print(f"[reel 1/6] using local source -> {source_video}")

    post_text = clean_post_text(metadata.get("description")) or str(metadata.get("title") or "")
    headline_text = generate_headline(channel, post_text, headline)
    views = compact_number(metadata.get("view_count"))

    width, height = probe_dimensions(source_video)
    orientation = "landscape" if width > height else ("square" if width == height else "portrait")
    rect = media_rect(width, height)
    print(f"[reel 2/6] source is {width}x{height} ({orientation}); headline: {headline_text!r}")

    slug_parts = [channel.id, handle_slug(source_handle)]
    if status_id:
        slug_parts.append(status_id)
    run_dir = out_dir / "_".join(part for part in slug_parts if part)
    run_dir.mkdir(parents=True, exist_ok=True)

    overlay = render_overlay(channel, headline_text, views, rect, run_dir / "overlay.png")
    theme = reel_theme(channel)
    reel = compose_reel(
        source_video, overlay, run_dir / "reel.mp4",
        rect=rect, background=theme["background"], fps=fps,
    )

    poster = run_dir / "poster.png"
    print(f"[reel 5/6] writing poster -> {poster}")
    run([_require("ffmpeg"), "-y", "-i", str(reel), "-frames:v", "1", "-update", "1", str(poster)])

    qa_frames = extract_qa_frames(reel, run_dir)

    manifest = {
        "channel": channel.id,
        "handle": channel.handle,
        "account_name": channel.account_name,
        "logo": channel.logo_doc_rel,
        "language": channel.language_name,
        "source": str(resolved),
        "source_video": str(source_video),
        "source_dimensions": f"{width}x{height}",
        "orientation": orientation,
        "headline": headline_text,
        "views": views,
        "media_rect": {"x": rect[0], "y": rect[1], "width": rect[2], "height": rect[3]},
        "size": f"{REEL_W}x{REEL_H}",
        "fps": fps,
        "theme": theme,
        "overlay": str(overlay),
        "reel": str(reel),
        "poster": str(poster),
        "qa_frames": [str(frame) for frame in qa_frames],
    }
    manifest_path = run_dir / "reel.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"[reel] wrote manifest -> {manifest_path}")
    print(f"[reel] done -> {reel}")
    return manifest


def main() -> int:
    load_env_file(ROOT / ".env")  # make XAI_API_KEY available for headline generation
    ap = argparse.ArgumentParser(
        description="Render a branded 9:16 Instagram reel from a video post "
        "(--channel selects branding/language/voice/logo). Render only, never publishes."
    )
    ap.add_argument("--source", required=True, help="Local video path, URL, or X status URL")
    ap.add_argument(
        "--channel",
        default=os.environ.get("CAROUSEL_CHANNEL"),
        help="Channel id (see channels/<id>/channel.json); defaults to the registry "
        "default_channel; also settable with CAROUSEL_CHANNEL.",
    )
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="Output base dir")
    ap.add_argument(
        "--headline", "--caption", dest="headline",
        help="Override the headline (default: written in the channel language from the post text)",
    )
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument(
        "--cookies-from-browser",
        help="Pass through to yt-dlp, for example chrome or safari when X gates media",
    )
    args = ap.parse_args()

    # Select the active channel for every load_channel() call in this process.
    if args.channel:
        os.environ["CAROUSEL_CHANNEL"] = args.channel
    channel = load_channel(args.channel)

    build_reel(
        source=args.source,
        channel=channel,
        out_dir=args.out_dir,
        headline=args.headline,
        fps=args.fps,
        cookies_from_browser=args.cookies_from_browser,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
