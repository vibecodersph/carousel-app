#!/usr/bin/env python3
"""Build a branded 1080x1350 MP4 carousel slide from a video source.

The source can be a local video, a URL supported by yt-dlp, or pasted
X/Twitter embed HTML. The result is an MP4 that uses the same vibecodersph
carousel furniture as the static PNG slides.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from channel import load_channel

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
FONTS = ASSETS / "archivo.css"
OUT = ROOT / "out"
VIDEO_ASSETS = ASSETS / "video_sources"

SLIDE_W, SLIDE_H = 1080, 1350
MEDIA_X, MEDIA_Y, MEDIA_W, MEDIA_H = 110, 300, 860, 760
POST_VIDEO_MEDIA_X, POST_VIDEO_MEDIA_Y = 75, 600
POST_VIDEO_MEDIA_W, POST_VIDEO_MEDIA_H = 930, 523


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def extract_status_url(value: str) -> str | None:
    text = html.unescape(value)
    match = re.search(
        r"https://(?:www\.)?(?:x|twitter)\.com/[^\"'\s<>]+/status/\d+[^\"'\s<>]*",
        text,
    )
    if not match:
        return None
    return match.group(0)


def extract_status_id(value: str) -> str | None:
    status_url = extract_status_url(value) or value
    match = re.search(r"(?:/status/|^)(\d+)", status_url)
    if not match:
        return None
    return match.group(1)


def strip_tags(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def parse_tweet_embed(value: str | None) -> dict[str, str] | None:
    if not value or "twitter-tweet" not in value:
        return None

    decoded = html.unescape(value)
    post: dict[str, str] = {}
    status_url = extract_status_url(decoded)
    if status_url:
        post["url"] = status_url

    text_match = re.search(r"<p\b[^>]*>(.*?)</p>", decoded, flags=re.IGNORECASE | re.DOTALL)
    if text_match:
        post["text"] = strip_tags(text_match.group(1))

    byline_match = re.search(
        r"(?:&mdash;|—)\s*([^<\(]+?)\s*\((@[^)]+)\)\s*<a\b[^>]*>([^<]+)</a>",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if byline_match:
        post["author"] = html.unescape(byline_match.group(1)).strip()
        post["handle"] = html.unescape(byline_match.group(2)).strip()
        post["date"] = html.unescape(byline_match.group(3)).strip()
    return post or None


def normalize_source(source: str | None, tweet_embed_file: Path | None) -> str | Path:
    if tweet_embed_file:
        embed = tweet_embed_file.read_text()
        status_url = extract_status_url(embed)
        if not status_url:
            raise SystemExit(f"no X/Twitter status URL found in {tweet_embed_file}")
        return status_url

    if not source:
        raise SystemExit("provide --source or --tweet-embed-file")

    source = source.strip()
    status_url = extract_status_url(source)
    if status_url:
        return status_url

    path = Path(source).expanduser()
    if path.exists():
        return path.resolve()

    if is_url(source):
        return source

    raise SystemExit(f"source is not a file, URL, or X embed: {source}")


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode != 0:
        if capture:
            if result.stdout:
                print(result.stdout, file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(cmd)}")
    return result


def download_video(url: str, output_dir: Path, cookies_from_browser: str | None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = output_dir / "%(extractor)s_%(id)s.%(ext)s"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-mtime",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
        "-f",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "-o",
        str(output_template),
        "--print",
        "after_move:filepath",
    ]
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    cmd.append(url)

    print(f"[video 1/5] downloading source -> {output_dir}")
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        if "No module named yt_dlp" in result.stderr:
            print(
                "yt-dlp is not installed. Use `uv run python build_video_slide.py ...` "
                "or install the dependencies in pyproject.toml.",
                file=sys.stderr,
            )
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(f"could not download video URL: {url}")

    for line in reversed(result.stdout.splitlines()):
        candidate = Path(line.strip())
        if candidate.exists():
            return candidate

    candidates = sorted(output_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            return candidate

    raise SystemExit(f"download finished, but no video file was found in {output_dir}")


def fetch_video_metadata(url: str, cookies_from_browser: str | None) -> dict[str, object]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--dump-json",
    ]
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    cmd.append(url)

    print("[video] fetching post metadata")
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise SystemExit(f"could not fetch metadata for: {url}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit("yt-dlp returned metadata that was not valid JSON") from exc


def compact_number(value: object) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number >= 1_000_000:
        text = f"{number / 1_000_000:.1f}M"
    elif number >= 1_000:
        text = f"{number / 1_000:.1f}K"
    else:
        text = str(int(number))
    return text.replace(".0M", "M").replace(".0K", "K")


def format_post_date(timestamp: object) -> str:
    if not timestamp:
        return ""
    try:
        dt = datetime.fromtimestamp(float(timestamp), timezone.utc)
    except (TypeError, ValueError, OSError):
        return ""
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def clean_post_text(text: object) -> str:
    if not isinstance(text, str):
        return ""
    text = html.unescape(text).strip()
    text = re.sub(r"\s*(?:https://t\.co|pic\.twitter\.com)/\S+\s*$", "", text)
    text = re.sub(r"[ \t]{2,}", "\n\n", text)
    return text.strip()


def post_from_metadata(
    metadata: dict[str, object] | None,
    *,
    author: str | None,
    handle: str | None,
    text: str | None,
    date: str | None,
) -> dict[str, str]:
    metadata = metadata or {}
    resolved_author = author or str(metadata.get("uploader") or "Source post")
    resolved_handle = handle or str(metadata.get("uploader_id") or "")
    if resolved_handle and not resolved_handle.startswith("@"):
        resolved_handle = f"@{resolved_handle}"

    resolved_text = clean_post_text(text) or clean_post_text(metadata.get("description"))
    if not resolved_text:
        resolved_text = str(metadata.get("title") or "Source post")

    resolved_date = date or format_post_date(metadata.get("timestamp"))
    views = compact_number(metadata.get("view_count"))
    likes = compact_number(metadata.get("like_count"))
    reposts = compact_number(metadata.get("repost_count"))
    replies = compact_number(metadata.get("comment_count"))

    meta_items = [item for item in [resolved_date, f"{views} views" if views else ""] if item]
    metric_items = [
        f"{likes} likes" if likes else "",
        f"{reposts} reposts" if reposts else "",
        f"{replies} replies" if replies else "",
    ]

    return {
        "author": resolved_author,
        "handle": resolved_handle,
        "text": resolved_text,
        "date_line": " · ".join(meta_items),
        "metrics": " · ".join(item for item in metric_items if item),
        "initial": (resolved_author.strip()[:1] or "S").upper(),
    }


def dots(active: int, count: int) -> str:
    active = max(1, min(active, count))
    items = []
    for i in range(1, count + 1):
        items.append('<div class="dash"></div>' if i == active else '<div class="dot"></div>')
    return "\n    ".join(items)


def handle_markup() -> str:
    channel = load_channel()
    handle = html.escape(channel.handle)
    colors = channel.brand.get("colors", {})
    icon_fill = colors.get("fg", "#16140F")
    icon_lens = colors.get("bg", "#F4F2EC")
    return f"""
  <div class="handle">
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none">
      <rect x="2" y="2" width="20" height="20" rx="5.5" fill="{icon_fill}"/>
      <circle cx="12" cy="12" r="4.4" stroke="{icon_lens}" stroke-width="1.8"/>
      <circle cx="17.2" cy="6.8" r="1.3" fill="{icon_lens}"/>
    </svg>
    <span>{handle}</span>
  </div>
"""


def media_box(layout: str) -> tuple[int, int, int, int]:
    if layout == "post-video":
        return POST_VIDEO_MEDIA_X, POST_VIDEO_MEDIA_Y, POST_VIDEO_MEDIA_W, POST_VIDEO_MEDIA_H
    return MEDIA_X, MEDIA_Y, MEDIA_W, MEDIA_H


def post_text_size(text: str) -> int:
    if len(text) > 260:
        return 23
    if len(text) > 190:
        return 25
    if len(text) > 120:
        return 27
    return 30


def frame_html(
    caption: str,
    kicker: str,
    source_label: str,
    active: int,
    count: int,
    *,
    layout: str,
    post: dict[str, str] | None,
) -> str:
    font_css = FONTS.read_text()
    safe_caption = html.escape(caption)
    safe_kicker = html.escape(kicker)
    safe_source = html.escape(source_label or "SOURCE VIDEO")
    media_x, media_y, media_w, media_h = media_box(layout)
    media_shell_x = media_x - 18
    media_shell_y = media_y - 18
    media_shell_w = media_w + 36
    media_shell_h = media_h + 36
    is_post_video = layout == "post-video"
    kicker_top = 154 if is_post_video else 176
    kicker_markup = "" if is_post_video else f'<div class="kicker"><em>{safe_kicker}</em></div>'
    post_card_top = 146
    post_card_x = media_x
    post_card_w = media_w
    post_card_h = 386
    source_label_top = 1100
    caption_top = 1140
    dots_bottom = 62 if is_post_video else 108
    post_markup = ""
    source_markup = f'<div class="source-label">{safe_source}</div>'
    caption_markup = f'<div class="video-caption">{safe_caption}</div>'
    if is_post_video:
        if not post:
            raise SystemExit("post-video layout requires post metadata")
        safe_author = html.escape(post["author"])
        safe_handle = html.escape(post["handle"])
        safe_text = html.escape(post["text"])
        safe_date_line = html.escape(post["date_line"])
        safe_metrics = html.escape(post["metrics"])
        safe_initial = html.escape(post["initial"])
        size = post_text_size(post["text"])
        post_markup = f"""
  <div class="post-card">
    <div class="post-head">
      <div class="post-avatar">{safe_initial}</div>
      <div class="post-name-block">
        <div class="post-author">{safe_author}<span>✓</span></div>
        <div class="post-handle">{safe_handle}</div>
      </div>
      <div class="post-source">X</div>
    </div>
    <div class="post-text" style="font-size: {size}px">{safe_text}</div>
    <div class="post-meta">{safe_date_line}</div>
    <div class="post-metrics">{safe_metrics}</div>
  </div>
"""
        source_markup = f'<div class="media-label">{safe_source}</div>' if source_label else ""
        caption_markup = ""
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
{font_css}

:root {{
  --bg: #F4F2EC;
  --bg-top: #E9E6DF;
  --fg: #16140F;
  --ink-soft: rgba(20, 18, 14, 0.78);
  --primary: #C0552E;
  --muted: rgba(20, 18, 14, 0.55);
  --rule: rgba(20, 18, 14, 0.28);
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ margin: 0; background: transparent; font-family: 'Archivo', sans-serif; }}

.slide {{
  width: {SLIDE_W}px;
  height: {SLIDE_H}px;
  position: relative;
  overflow: hidden;
  background: linear-gradient(180deg, var(--bg-top) 0%, #F1EEE6 48%, var(--bg) 100%);
  color: var(--fg);
}}

.handle {{
  position: absolute;
  top: 68px;
  left: 78px;
  display: flex;
  align-items: center;
  gap: 14px;
}}
.handle svg {{ display: block; }}
.handle span {{
  font-size: 27px;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--fg);
}}

.zone {{ position: absolute; pointer-events: none; }}
.zone.top-right {{ top: 40px; right: 40px; width: 160px; height: 80px; }}
.zone.bottom-handle {{ bottom: 40px; left: 340px; width: 400px; height: 60px; }}

.kicker {{
  position: absolute;
  top: {kicker_top}px;
  left: 120px;
  right: 120px;
  display: flex;
  align-items: center;
  gap: 28px;
}}
.kicker::before, .kicker::after {{
  content: '';
  flex: 1;
  height: 2px;
  background: var(--rule);
}}
.kicker em {{
  font-style: normal;
  font-size: 25px;
  font-weight: 700;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--ink-soft);
  white-space: nowrap;
}}

.media-shell {{
  position: absolute;
  top: {media_shell_y}px;
  left: {media_shell_x}px;
  width: {media_shell_w}px;
  height: {media_shell_h}px;
  border-radius: 30px;
  background: #16140F;
  border: 3px solid rgba(20, 18, 14, 0.82);
  box-shadow: 0 30px 70px rgba(20, 18, 14, 0.22);
}}

.media-slot {{
  position: absolute;
  top: {media_y}px;
  left: {media_x}px;
  width: {media_w}px;
  height: {media_h}px;
  background: #11100D;
  overflow: hidden;
}}

.source-label {{
  position: absolute;
  top: {source_label_top}px;
  left: 120px;
  right: 120px;
  text-align: center;
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--primary);
}}

.video-caption {{
  position: absolute;
  top: {caption_top}px;
  left: 126px;
  right: 126px;
  max-height: 88px;
  overflow: hidden;
  text-align: center;
  font-size: 30px;
  font-weight: 650;
  line-height: 1.26;
  color: var(--ink-soft);
}}
.video-caption strong {{ font-weight: 850; color: var(--fg); }}

.dots {{
  position: absolute;
  left: 0;
  right: 0;
  bottom: {dots_bottom}px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
}}
.dots .dash {{
  width: 34px;
  height: 9px;
  border-radius: 5px;
  background: var(--primary);
}}
.dots .dot {{
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: rgba(20, 18, 14, 0.28);
}}

.post-card {{
  position: absolute;
  top: {post_card_top}px;
  left: {post_card_x}px;
  width: {post_card_w}px;
  height: {post_card_h}px;
  padding: 26px 36px 22px;
  border-radius: 28px;
  background: #101820;
  color: #E7E9EA;
  border: 2px solid rgba(231, 233, 234, 0.16);
  box-shadow: 0 24px 58px rgba(20, 18, 14, 0.18);
}}
.post-head {{
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 18px;
}}
.post-avatar {{
  width: 54px;
  height: 54px;
  border-radius: 50%;
  background: var(--primary);
  color: #FFF8F2;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 27px;
  font-weight: 850;
  flex: 0 0 auto;
}}
.post-name-block {{ flex: 1; min-width: 0; }}
.post-author {{
  display: flex;
  align-items: center;
  gap: 8px;
  color: #FFFFFF;
  font-size: 26px;
  line-height: 1.08;
  font-weight: 850;
}}
.post-author span {{
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #1D9BF0;
  color: white;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 900;
}}
.post-handle {{
  margin-top: 4px;
  color: #8B98A5;
  font-size: 22px;
  font-weight: 650;
}}
.post-source {{
  color: #FFFFFF;
  font-size: 32px;
  font-weight: 850;
}}
.post-text {{
  color: #F7F9F9;
  line-height: 1.2;
  font-weight: 560;
  white-space: pre-line;
  max-height: 168px;
  overflow: hidden;
}}
.post-meta {{
  margin-top: 14px;
  color: #8B98A5;
  font-size: 19px;
  font-weight: 650;
}}
.post-metrics {{
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid #2F3336;
  color: #8B98A5;
  font-size: 18px;
  font-weight: 700;
}}
.media-label {{
  position: absolute;
  top: {media_y + 18}px;
  left: {media_x + 18}px;
  z-index: 2;
  border-radius: 999px;
  background: rgba(16, 24, 32, 0.78);
  color: #F7F9F9;
  padding: 9px 14px;
  font-size: 17px;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}}
</style>
</head>
<body>
<div class="slide">
{handle_markup()}
  <div class="zone top-right"></div>
  {kicker_markup}
  {post_markup}
  <div class="media-shell"></div>
  <div class="media-slot"></div>
  {source_markup}
  {caption_markup}
  <div class="dots">
    {dots(active, count)}
  </div>
  <div class="zone bottom-handle"></div>
</div>
</body>
</html>
"""


def render_frame(
    frame_out: Path,
    caption: str,
    kicker: str,
    source_label: str,
    active: int,
    count: int,
    layout: str,
    post: dict[str, str] | None,
) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "playwright is not installed. Use `uv run python build_video_slide.py ...` "
            "or install the dependencies in pyproject.toml."
        ) from exc

    OUT.mkdir(exist_ok=True)
    html_path = OUT / "_video_frame.html"
    html_path.write_text(
        frame_html(
            caption,
            kicker,
            source_label,
            active,
            count,
            layout=layout,
            post=post,
        )
    )
    frame_out.parent.mkdir(parents=True, exist_ok=True)

    print(f"[video 2/5] rendering branded frame -> {frame_out}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome")
            page = browser.new_page(
                viewport={"width": SLIDE_W, "height": SLIDE_H},
                device_scale_factor=1,
            )
            page.goto(html_path.as_uri())
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(300)
            page.locator(".slide").screenshot(path=str(frame_out))
            browser.close()
    except Exception as exc:
        raise SystemExit(
            "could not render the frame. If this is a fresh setup, run "
            "`uv run python -m playwright install chromium` once."
        ) from exc
    return frame_out


def compose_video(
    source_video: Path,
    frame: Path,
    out_path: Path,
    *,
    fit: str,
    fps: int,
    mute: bool,
    layout: str,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required to compose branded video slides")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    current_media_x, current_media_y, current_media_w, current_media_h = media_box(layout)
    if fit == "cover":
        media_filter = (
            f"scale={current_media_w}:{current_media_h}:force_original_aspect_ratio=increase,"
            f"crop={current_media_w}:{current_media_h}"
        )
    else:
        media_filter = (
            f"scale={current_media_w}:{current_media_h}:force_original_aspect_ratio=decrease,"
            f"pad={current_media_w}:{current_media_h}:(ow-iw)/2:(oh-ih)/2:color=#11100D"
        )

    filter_complex = (
        f"[1:v]{media_filter},setsar=1[media];"
        f"[0:v][media]overlay={current_media_x}:{current_media_y}:shortest=1,format=yuv420p[v]"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(frame),
        "-i",
        str(source_video),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
    ]
    if mute:
        cmd.append("-an")
    else:
        cmd.extend(["-map", "1:a?", "-c:a", "aac", "-b:a", "160k"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-shortest",
            str(out_path),
        ]
    )

    print(f"[video 3/5] composing MP4 -> {out_path}")
    run(cmd)
    return out_path


def make_poster(video_path: Path, poster_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required to create poster PNGs")
    poster_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-i", str(video_path), "-frames:v", "1", "-update", "1", str(poster_path)]
    print(f"[video 4/5] writing poster -> {poster_path}")
    run(cmd)
    return poster_path


def build_video_slide(
    *,
    source: str | None,
    tweet_embed_file: Path | None,
    out_path: Path,
    frame_out: Path,
    poster_out: Path,
    caption: str,
    kicker: str,
    source_label: str,
    active: int,
    count: int,
    fit: str,
    fps: int,
    mute: bool,
    cookies_from_browser: str | None,
    layout: str,
    post_author: str | None,
    post_handle: str | None,
    post_text: str | None,
    post_date: str | None,
) -> dict[str, object]:
    raw_source = tweet_embed_file.read_text() if tweet_embed_file else source
    embed_post = parse_tweet_embed(raw_source if isinstance(raw_source, str) else None)
    resolved = normalize_source(source, tweet_embed_file)
    metadata = None
    if layout == "post-video" and isinstance(resolved, str):
        metadata = fetch_video_metadata(resolved, cookies_from_browser)
    post = None
    if layout == "post-video":
        post = post_from_metadata(
            metadata,
            author=post_author or (embed_post or {}).get("author"),
            handle=post_handle or (embed_post or {}).get("handle"),
            text=post_text or (embed_post or {}).get("text"),
            date=post_date or (embed_post or {}).get("date"),
        )

    if isinstance(resolved, Path):
        source_video = resolved
    else:
        source_video = download_video(resolved, VIDEO_ASSETS, cookies_from_browser)

    frame = render_frame(frame_out, caption, kicker, source_label, active, count, layout, post)
    video = compose_video(source_video, frame, out_path, fit=fit, fps=fps, mute=mute, layout=layout)
    poster = make_poster(video, poster_out)

    manifest = {
        "source": str(resolved),
        "source_video": str(source_video),
        "frame": str(frame),
        "video": str(video),
        "poster": str(poster),
        "layout": layout,
        "post": post,
        "size": f"{SLIDE_W}x{SLIDE_H}",
        "media_box": {
            "x": media_box(layout)[0],
            "y": media_box(layout)[1],
            "width": media_box(layout)[2],
            "height": media_box(layout)[3],
            "fit": fit,
        },
    }
    manifest_path = out_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[video 5/5] wrote manifest -> {manifest_path}")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="Local video path, URL, X status URL, or pasted tweet embed HTML")
    ap.add_argument("--tweet-embed-file", type=Path, help="HTML file containing a twitter-tweet blockquote")
    ap.add_argument("--layout", choices=["video", "post-video"], default="video")
    ap.add_argument("--out", type=Path, default=OUT / "video_slide_02.mp4")
    ap.add_argument("--frame-out", type=Path, default=OUT / "video_frame_02.png")
    ap.add_argument("--poster-out", type=Path, default=OUT / "video_slide_02_poster.png")
    ap.add_argument("--caption", default="A source video, framed inside the vibecodersph carousel system.")
    ap.add_argument("--kicker", default="Video receipt")
    ap.add_argument("--source-label", default="")
    ap.add_argument("--active", type=int, default=2, help="Active dot index, 1-based")
    ap.add_argument("--count", type=int, default=6, help="Total dots")
    ap.add_argument("--fit", choices=["contain", "cover"], default="contain")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--mute", action="store_true")
    ap.add_argument(
        "--cookies-from-browser",
        help="Pass through to yt-dlp, for example chrome or safari when X gates media",
    )
    ap.add_argument("--post-author", help="Override detected post author in post-video layout")
    ap.add_argument("--post-handle", help="Override detected post handle in post-video layout")
    ap.add_argument("--post-text", help="Override detected post text in post-video layout")
    ap.add_argument("--post-date", help="Override detected post date in post-video layout")
    args = ap.parse_args()

    build_video_slide(
        source=args.source,
        tweet_embed_file=args.tweet_embed_file,
        out_path=args.out,
        frame_out=args.frame_out,
        poster_out=args.poster_out,
        caption=args.caption,
        kicker=args.kicker,
        source_label=args.source_label,
        active=args.active,
        count=args.count,
        fit=args.fit,
        fps=args.fps,
        mute=args.mute,
        cookies_from_browser=args.cookies_from_browser,
        layout=args.layout,
        post_author=args.post_author,
        post_handle=args.post_handle,
        post_text=args.post_text,
        post_date=args.post_date,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
