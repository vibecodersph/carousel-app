#!/usr/bin/env python3
"""
Daily AI/tech news carousel builder for carousel-app.

Uses the same RSS feed pipeline as VCPH OS (via vcph_feed_pipeline.py) but
renders multi-slide carousels with images instead of a single magazine cover.

    uv run python build_daily_carousel.py
    uv run python build_daily_carousel.py --no-images  # text-only fallback

Slides:
  1. Cover/hook: VCPH OS Daily Drop magazine cover + carousel swipe cue
  2..N+1. Story slide: article image (scraped or generated) + headline + body
  Last. CTA: follow @handle

Cover image priority: VCPH OS Daily Drop full-cover generator for 5-story VCPH runs,
then GPT Image 2.0 compilation fallback.
Story image priority: scrape og:image from article URL → GPT Image 2.0 fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from build_x_carousel import (  # noqa: E402
    SLIDE_H,
    SLIDE_W,
    carousel_cta_copy,
    dot_markup,
    download_image,
    extract_gemini_text,
    gemini_api_key,
    gemini_generate_content,
    gemini_text_model,
    load_env_file,
    parse_json_object,
    phrase_text_markup,
    render_cta_slide,
    render_html_slide,
    shared_css,
)
from channel import Channel, load_channel  # noqa: E402
from daily_drop_cover import generate_full_cover as generate_daily_drop_full_cover  # noqa: E402
from vcph_feed_pipeline import (  # noqa: E402
    get_diverse_stories,
    record_posted_stories,
)

DEFAULT_OUT = ROOT / "out" / "daily_carousel"
DEFAULT_MAX_STORIES = 5
COVER_SIZE = "1024x1024"
STORY_IMAGE_SIZE = "1024x1024"
BASE_VOICE_GUIDE = ROOT / "brand" / "VIBECODERS_IG_VOICE.md"

# ─────────────────────────── Helpers ───────────────────────────


def _load_dotenv_value(*names):
    for name in names:
        val = os.environ.get(name)
        if val:
            return val.strip()
    env_path = ROOT / ".env"
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() in names:
                    return value.strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _load_openai_key():
    return (
        os.environ.get("OPENAI_API_KEY")
        or _load_dotenv_value("OPENAI_API_KEY")
    )


def _load_gemini_key():
    return (
        gemini_api_key()
        or _load_dotenv_value("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )


def strip_em_dashes(text):
    if not text:
        return ""
    return text.replace("\u2014", ", ").replace("\u2013", "-")


def _strip_for_vcph(text):
    """Normalize VCPH copy after model output."""
    text = strip_em_dashes(str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip('"').strip()


def _clamp_words(text, limit):
    """Clamp text without cutting the final word in half."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return clipped or text[:limit].rstrip()


def _plain_cover_headline(text):
    """Remove accent brackets before sending text to image models."""
    return re.sub(r"\[([^\]]+)\]", r"\1", _strip_for_vcph(text))


_COVER_TERM_STOPWORDS = {
    "about", "after", "again", "adds", "added", "also", "with", "from",
    "into", "more", "news", "over", "today", "this", "that", "their",
    "they", "your", "will", "tech", "updated", "latest", "officially",
}


def _story_terms(story):
    text = f"{story.get('source', '')} {story.get('title', '')} {story.get('desc', '')[:180]}"
    terms = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+-]{2,}", text.lower()):
        token = raw.strip(".+-")
        if len(token) >= 4 and token not in _COVER_TERM_STOPWORDS:
            terms.add(token)
    return terms


def _text_matches_story(text, story):
    """Loose guard to keep Daily Drop cover art aligned with story #1."""
    haystack = str(text or "").lower()
    if not haystack:
        return False
    return any(term in haystack for term in _story_terms(story))


def _daily_drop_voice_stories(stories, voice):
    """Adapt carousel story copy to VCPH OS generate_full_cover's schema."""
    vmap = {}
    if voice and isinstance(voice.get("stories"), list):
        for row in voice["stories"]:
            if isinstance(row, dict):
                vmap[row.get("n", 0)] = row

    out = []
    for i, story in enumerate(stories, 1):
        row = vmap.get(i, {})
        headline = row.get("headline") or story.get("title", "")
        blurb = row.get("body") or story.get("desc", "")
        out.append({
            "n": i,
            "headline": _strip_for_vcph(headline),
            "blurb": _strip_for_vcph(blurb),
        })
    return out


def _generate_daily_drop_magazine_cover(
    stories,
    out_dir,
    channel,
    voice=None,
    cover_headline="",
    cover_subtitle="",
):
    """Generate a VCPH OS-style full magazine cover and copy it into out_dir."""
    if channel.id != "vibecodersph":
        return None

    voice_stories = _daily_drop_voice_stories(stories, voice)
    if not voice_stories:
        return None
    if len(voice_stories) < 5:
        print("  [cover] Daily Drop magazine cover needs 5 stories; using montage fallback")
        return None

    cover_subject = ""
    cover_style = ""
    if voice:
        cover_subject = _strip_for_vcph(voice.get("cover_subject", ""))
        cover_style = _strip_for_vcph(voice.get("cover_style", ""))

    hero_story = stories[0] if stories else {}
    hero_cover_line = _plain_cover_headline(cover_headline)
    if hero_story and hero_cover_line and not _text_matches_story(hero_cover_line, hero_story):
        print("  [cover guard] cover headline did not match story #1; using story #1 headline")
        hero_cover_line = voice_stories[0].get("headline", hero_cover_line)

    if hero_story and cover_subject and not _text_matches_story(cover_subject, hero_story):
        print("  [cover guard] cover subject did not match story #1; rebuilding from story #1")
        cover_subject = ""

    if not cover_subject:
        fallback_title = hero_story.get("title") or hero_cover_line or "today's AI news"
        cover_subject = (
            "A full VIBECODERSPH Daily Drop magazine cover for the top story: "
            f"{fallback_title}. "
            "Use a specific editorial metaphor, not a generic tech dashboard."
        )

    digest_payload = {
        "stories": [s.get("title", "") for s in stories],
        "cover_headline": cover_headline,
        "cover_subtitle": cover_subtitle,
        "cover_subject": cover_subject,
        "cover_style": cover_style,
        "carousel_mode": True,
    }
    digest = hashlib.sha1(
        json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:10]
    img_path = out_dir / "images" / f"magazine-cover-{digest}.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    if img_path.exists():
        print(f"  [cover] using cached Daily Drop magazine cover -> {img_path.name}")
        return img_path

    try:
        generated = generate_daily_drop_full_cover(
            voice_stories,
            hero_cover_line=hero_cover_line,
            cover_subject=cover_subject,
            cover_style=cover_style,
            output_path=img_path,
            skip_logo_overlay=True,
        )
        if generated and Path(str(generated)).exists():
            return img_path
    except Exception as e:
        print(f"  [cover] Daily Drop magazine cover failed: {e}")
    return None


# ─────────────────────────── Image helpers ───────────────────────────

def _fetch_article_image(url, out_dir=DEFAULT_OUT):
    """Scrape og:image or first significant image from an article URL.

    Returns path to downloaded image, or None.
    """
    if not url or not url.startswith("http"):
        return None
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0 carousel-app/1.0"})
        with urlopen(req, timeout=15) as resp:
            html_bytes = resp.read(500_000)
        html_text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        return None

    # Try og:image meta tag
    og_match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        html_text, re.I,
    )
    if not og_match:
        og_match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            html_text, re.I,
        )
    if og_match:
        img_url = html.unescape(og_match.group(1))
        if img_url.startswith("/"):
            from urllib.parse import urljoin
            img_url = urljoin(url, img_url)
        if img_url.startswith("http"):
            path = download_image(img_url, out_dir / "images", "article")
            if path:
                print(f"  [img] scraped og:image -> {path.name}")
                return path

    # Fallback: find first <img> with reasonable size hints
    img_match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_text, re.I)
    if img_match:
        img_url = html.unescape(img_match.group(1))
        if img_url.startswith("/"):
            from urllib.parse import urljoin
            img_url = urljoin(url, img_url)
        if img_url.startswith("http") and not img_url.endswith(".svg"):
            path = download_image(img_url, out_dir / "images", "article")
            if path:
                print(f"  [img] scraped first <img> -> {path.name}")
                return path

    return None


def _generate_cover_image(stories, out_dir, channel, voice=None, cover_headline="", cover_subtitle=""):
    """Generate a cover image.

    Preferred path: VCPH OS Daily Drop full magazine cover. Fallback: the older
    carousel montage background.

    Returns path to generated PNG, or None.
    """
    magazine_cover = _generate_daily_drop_magazine_cover(
        stories,
        out_dir,
        channel,
        voice=voice,
        cover_headline=cover_headline,
        cover_subtitle=cover_subtitle,
    )
    if magazine_cover:
        return magazine_cover

    the_key = _load_openai_key()
    if not the_key:
        print("  [img] No OPENAI_API_KEY; skipping cover image generation")
        return None

    try:
        from openai import OpenAI
    except ImportError:
        print("  [img] openai package not installed; skipping cover image")
        return None

    # Build a compilation prompt from all stories
    story_summaries = []
    for s in stories:
        src = s.get("source", "")
        title = s.get("title", "")[:100]
        story_summaries.append(f"- {src}: {title}")

    stories_text = "\n".join(story_summaries)
    brand = channel.brand
    colors = brand.get("colors", {})
    bg_hex = colors.get("bg", "#F4F2EC")
    primary_hex = colors.get("primary", "#C0552E")

    prompt = (
        "An editorial magazine cover compilation for an AI/tech news Instagram carousel. "
        "The composition should feel like a Wired or Fast Company cover: a montage or "
        "collage of abstract editorial illustrations representing today's top stories. "
        f"Background: {bg_hex} warm cream paper texture with dark ink accents "
        f"and {primary_hex} terracotta/rust highlights. "
        "Style: abstract geometric, editorial magazine aesthetic, ink-wash technique, "
        "grain texture, restrained composition. No visible text, no logos, no letters, "
        "no numbers, no words, no UI elements. The carousel typography will be overlaid "
        "separately. Make it visually striking, a single hero image that represents "
        "the day's AI news collectively. Square 1:1 format.\n\n"
        "Today's stories to represent visually:\n"
        f"{stories_text}"
    )

    digest = hashlib.sha1(prompt.encode()).hexdigest()[:10]
    img_path = out_dir / "images" / f"cover-{digest}.png"
    img_path.parent.mkdir(parents=True, exist_ok=True)

    if img_path.exists():
        print(f"  [img] using cached cover -> {img_path.name}")
        return img_path

    try:
        client = OpenAI(api_key=the_key)
        response = client.images.generate(
            model="gpt-image-2",
            prompt=prompt,
            size=COVER_SIZE,
            n=1,
        )
        b64 = response.data[0].b64_json
        if b64:
            import base64
            img_path.write_bytes(base64.b64decode(b64))
            print(f"  [img] generated cover montage -> {img_path.name}")
            return img_path
    except Exception as e:
        print(f"  [img] cover generation failed: {e}")

    return None


def _generate_story_image(story, out_dir):
    """Generate a single-story image via GPT Image 2.0. Fallback when scraping fails.

    Returns path to generated PNG, or None.
    """
    the_key = _load_openai_key()
    if not the_key:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    title = story.get("title", "AI technology news")[:120]
    source = story.get("source", "")

    prompt = (
        "An editorial illustration for an Instagram carousel story slide about AI/tech news. "
        f"Topic: {title}. Source: {source}. "
        "Style: abstract geometric, editorial magazine aesthetic, warm cream background "
        "with dark ink and terracotta accents. No visible text, no letters, no words, "
        "no logos, no UI. Square 1:1 format. Restrained, sophisticated, abstract."
    )

    digest = hashlib.sha1(prompt.encode()).hexdigest()[:10]
    stem = re.sub(r"[^a-z0-9]+", "-", title.lower())[:40]
    img_path = out_dir / "images" / f"story-{stem}-{digest}.png"
    img_path.parent.mkdir(parents=True, exist_ok=True)

    if img_path.exists():
        return img_path

    try:
        client = OpenAI(api_key=the_key)
        response = client.images.generate(
            model="gpt-image-2",
            prompt=prompt,
            size=STORY_IMAGE_SIZE,
            n=1,
        )
        b64 = response.data[0].b64_json
        if b64:
            import base64
            img_path.write_bytes(base64.b64decode(b64))
            print(f"  [img] generated story image -> {img_path.name}")
            return img_path
    except Exception as e:
        print(f"  [img] story image generation failed: {e}")

    return None


# ─────────────────────────── Voice rewrite ───────────────────────────

def rewrite_stories_daily_carousel(stories, channel) -> dict[str, Any] | None:
    """Gemini rewrite: per-slide headlines + bodies in channel voice."""
    api_key = _load_gemini_key()
    if not api_key:
        print("  [warn] No GEMINI_API_KEY/GOOGLE_API_KEY; using raw feed titles")
        return None

    guide_path = channel.voice_doc if channel.voice_doc and channel.voice_doc.exists() else BASE_VOICE_GUIDE
    try:
        guide = Path(guide_path).read_text(encoding="utf-8")
    except Exception:
        guide = ""

    channel_voice = channel.voice_prompt or channel.default_cover_voice()

    raw_list = [
        {
            "n": i + 1,
            "source": s["source"],
            "title": s["title"],
            "desc": s["desc"][:500],
            "link": s.get("link", ""),
        }
        for i, s in enumerate(stories)
    ]

    prompt = f"""
You are the editor of a daily AI-news Instagram carousel for {channel.brand_name},
written for {channel.audience}. Write every public-facing word in {channel.language_name}.

Return JSON only with this exact shape:
{{
  "cover_headline": "4 to 8 words, Taglish-native hook about story n=1 only, with exactly one [accent] word in brackets",
  "cover_subtitle": "one short Taglish line explaining today's mix",
  "cover_swipe_line": "short natural swipe prompt, e.g. swipe mo",
  "cover_subject": "1 to 3 sentences of VCPH Daily Drop magazine-cover art direction for the hero story",
  "cover_style": "empty string, or one obvious Daily Drop style key only when the story clearly needs it",
  "instagram_caption": "short Taglish caption with one hook, one useful line, one CTA, clean hashtags",
  "stories": [
    {{"n": 1, "headline": "6 to 10 words", "body": "1 to 2 sentences, 18 to 25 words"}}
  ]
}}

Voice rules:
- Use Gemini to write in the VibeCoders PH Instagram voice below, not generic news voice.
- Taglish-native for the cover, caption, and framing lines. Body slides can lean English for technical precision, but should still sound like a Pinoy builder talking to barkada.
- Smart-funny, never clown-funny. Keep jokes mostly on the cover or caption. Story headlines and bodies should be crisp and factual.
- No em dashes. No en dashes. Use commas, periods, colons, or parentheses.
- No clickbait phrases, no BREAKING, no MUST READ, no corporate buzzwords, no emoji stacks.
- No slay, ate, bestie, mga kababayan, or campaign-flyer energy.
- Do not over-localize global stories. Keep original geography, money, product names, model names, dates, and amounts.
- Stay faithful to the input. Do not invent numbers, launches, claims, quotes, companies, places, use cases, products, or scenes.
- Do not turn a real product into a metaphor or joke noun on story slides. If the source says ultrasound scanner, keep ultrasound scanner, not spa, mall, tinder, magic, etc.
- Preserve the core noun and action from each source title in each story headline.
- Any number or factual claim in body copy must appear in the source title/description.
- Keep each story in the same order and preserve its n value.
- Use exactly one [accent] word in cover_headline only. Do not use brackets in story headlines or bodies.
- Treat story n=1 as the hero story. cover_headline and cover_subject must be about story n=1 only. Stories n=2 to n=5 are lower cover/list items.
- cover_subject should think like a Wired or Bloomberg Businessweek art director for story n=1: one specific hero visual metaphor, not a generic AI dashboard, phone, laptop, glowing robot, chart wallpaper, or model-name screen.
- cover_style should usually be empty so the VCPH OS style rotation can decide. Only choose a style when the source has an obvious medium match.

{channel.brand_name} channel voice block:
{channel_voice}

Base VibeCodersPH voice guide:
{guide[:7000]}

Stories JSON:
{json.dumps(raw_list, ensure_ascii=False, indent=2)}
""".strip()

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "responseMimeType": "application/json",
        },
    }

    try:
        response = gemini_generate_content(
            gemini_text_model(),
            api_key,
            payload,
            api_version=os.environ.get("GEMINI_TEXT_API_VERSION") or "v1beta",
            timeout=90,
        )
        data = parse_json_object(extract_gemini_text(response))
        if not isinstance(data, dict):
            print("  [warn] Gemini voice rewrite returned no JSON; using raw titles")
            return None

        for key in (
            "cover_headline", "cover_subtitle", "cover_swipe_line",
            "cover_subject", "cover_style", "instagram_caption",
        ):
            if key in data:
                data[key] = _strip_for_vcph(data[key])
        story_rows = data.get("stories")
        if not isinstance(story_rows, list):
            story_rows = []
            data["stories"] = story_rows
        for vs in story_rows:
            if isinstance(vs, dict):
                vs["headline"] = _strip_for_vcph(vs.get("headline", ""))
                vs["body"] = _strip_for_vcph(vs.get("body", ""))

        return data
    except Exception as e:
        print(f"  [warn] Gemini voice rewrite failed: {e}")
        return None


# ─────────────────────────── Accent word ───────────────────────────

def _headline_with_accent(headline: str) -> tuple[str, str]:
    """Return build_x-style phrase markup plus plain text.

    The Daily builder accepts the same `[accent]` convention as the normal
    carousel cover system, but its deterministic fallback also auto-accents the
    longest useful word so the cover never looks unbranded.
    """
    match = re.search(r"\[([^\]]+)\]", headline)
    if match:
        word = match.group(1)
        plain = headline[:match.start()] + word + headline[match.end():]
        return phrase_text_markup(plain, accent=word, max_chars=11), plain
    words = headline.split()
    if words:
        longest = max(words, key=lambda w: len(re.sub(r"[^A-Za-z0-9]", "", w)))
        return phrase_text_markup(headline, accent=longest, max_chars=11), headline
    return html.escape(headline), headline


def _headline_size(text: str, *, cover: bool = False) -> int:
    """Approximate build_x typography scale for dynamic headlines."""
    length = len(text)
    if cover:
        if length <= 34:
            return 90
        if length <= 48:
            return 78
        if length <= 64:
            return 68
        return 58
    if length <= 32:
        return 70
    if length <= 46:
        return 62
    if length <= 64:
        return 54
    return 48


# ─────────────────────────── Slide HTML templates ───────────────────────────

def _file_uri(path):
    """Convert a local path to a file:// URI for Playwright."""
    return path.resolve().as_uri()


def _magazine_cover_slide_html(channel, cover_image, swipe_line, total_slides):
    """Full-bleed magazine cover with carousel chrome and logo overlaid."""
    cover_uri = _file_uri(cover_image)
    logo_uri = _file_uri(ROOT / "assets" / "vibecodersph_logo.png")
    safe_swipe = html.escape(swipe_line or "swipe for more")
    safe_handle = html.escape(channel.handle)
    return textwrap.dedent(f"""\
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    {shared_css()}
    .slide {{ background: #0d0b08; }}
    .mag-bg {{
      position: absolute; inset: 0; z-index: 0;
      background: url('{cover_uri}') center/cover no-repeat;
    }}
    .mag-scrim {{
      position: absolute; inset: 0; z-index: 1;
      background: linear-gradient(
        180deg,
        rgba(13,11,8,0.55) 0%,
        rgba(13,11,8,0.08) 28%,
        rgba(13,11,8,0.06) 72%,
        rgba(13,11,8,0.58) 100%
      );
    }}
    .mag-handle {{
      position: absolute; top: 44px; left: 58px; z-index: 4;
      color: rgba(244,242,236,0.90);
      font-size: 23px; font-weight: 840; letter-spacing: 0.12em;
      text-transform: uppercase;
      text-shadow: 0 2px 12px rgba(0,0,0,0.45);
    }}
    .mag-progress {{
      position: absolute; top: 44px; right: 58px; z-index: 4;
      color: rgba(244,242,236,0.78);
      font-size: 23px; font-weight: 840; letter-spacing: 0.06em;
      text-shadow: 0 2px 12px rgba(0,0,0,0.45);
    }}
    .mag-logo {{
      position: absolute; bottom: 38px; right: 42px; z-index: 4;
      width: 144px; height: auto; opacity: 0.92;
      filter: drop-shadow(0 4px 16px rgba(0,0,0,0.35));
    }}
    .mag-swipe {{
      position: absolute; left: 50%; bottom: 24px; transform: translateX(-50%);
      z-index: 5;
      padding: 11px 28px 12px;
      border-radius: 999px;
      background: rgba(244,242,236,0.94);
      color: #C0552E;
      font-size: 22px;
      font-weight: 860;
      letter-spacing: 0.01em;
      line-height: 1;
      box-shadow: 0 8px 28px rgba(0,0,0,0.30);
    }}
    </style></head>
    <body>
    <div class="slide">
      <div class="mag-bg"></div>
      <div class="mag-scrim"></div>
      <div class="mag-handle">{safe_handle}</div>
      <div class="mag-progress">01 / {total_slides:02d}</div>
      <img class="mag-logo" src="{logo_uri}" alt="VibeCodersPH">
      <div class="mag-swipe">{safe_swipe}</div>
    </div>
    </body></html>
    """)


def _cover_slide_html(channel, headline, headline_text, subtitle, swipe_line, story_count, bg_image=None):
    if bg_image and bg_image.exists() and bg_image.name.startswith("magazine-cover-"):
        return _magazine_cover_slide_html(channel, bg_image, swipe_line, story_count + 2)

    safe_handle = html.escape(channel.account_name.upper())
    safe_subtitle = html.escape(str(subtitle or "").rstrip(" ."))
    story_label = f"{story_count} stories" if story_count > 1 else "1 story"
    font_size = _headline_size(headline_text, cover=True)

    visual_class = "visual-card"
    visual_inner = """<div class="visual-fallback"></div>"""
    if bg_image and bg_image.exists():
        bg_uri = _file_uri(bg_image)
        visual_inner = f"""<div class="visual-bg" style="background-image:url('{bg_uri}')"></div><div class="visual-fallback"></div>"""

    return textwrap.dedent(f"""\
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    {shared_css()}
    .visual-card {{
      position: absolute; top: 0; left: 0; width: 100%; height: 790px;
      overflow: hidden; background: #151713;
    }}
    .visual-bg, .visual-fallback {{ position: absolute; inset: 0; }}
    .visual-bg {{
      z-index: 1; background-position: center; background-size: cover;
      filter: saturate(0.96) contrast(1.02);
    }}
    .visual-card::after {{
      content: ''; position: absolute; z-index: 2; inset: 0;
      background:
        linear-gradient(180deg, rgba(var(--bg-rgb), 0) 42%, rgba(var(--bg-rgb), 0.24) 62%, var(--bg) 100%);
      pointer-events: none;
    }}
    .visual-fallback {{
      z-index: 0;
      background:
        radial-gradient(circle at 78% 22%, rgba(var(--primary-rgb), 0.46) 0 16%, transparent 17%),
        radial-gradient(circle at 24% 34%, rgba(var(--primary-rgb), 0.28) 0 12%, transparent 13%),
        linear-gradient(135deg, rgba(var(--primary-rgb), 0.74), rgba(22, 20, 15, 0.94)),
        repeating-linear-gradient(90deg, rgba(var(--bg-rgb), 0.12) 0 2px, transparent 2px 18px);
    }}
    .title-cluster {{
      position: absolute; left: 56px; right: 56px; top: 742px; bottom: 168px;
      display: flex; flex-direction: column; justify-content: flex-end;
      text-align: left; z-index: 3;
    }}
    .account-rule {{
      display: flex; align-items: center; gap: 22px; margin-bottom: 30px; color: var(--primary);
    }}
    .account-rule::before, .account-rule::after {{ content: ''; flex: 1; height: 2px; background: var(--rule); }}
    .account-rule span {{
      font-size: 24px; font-weight: 820; letter-spacing: 0; line-height: 1;
      text-transform: uppercase; color: var(--primary);
    }}
    .headline {{
      font-size: {font_size}px; font-weight: 850; letter-spacing: 0; line-height: 1.03;
      color: var(--fg); line-break: strict; overflow-wrap: normal;
      text-wrap: balance; word-break: normal;
    }}
    .headline .accent {{ color: var(--primary); }}
    .headline .jp-phrase {{ display: inline-block; }}
    .headline .term {{ white-space: nowrap; }}
    .cover-subtitle {{
      margin-top: 28px; max-width: 860px; color: var(--ink-soft);
      font-size: 31px; line-height: 1.26; font-weight: 650;
    }}
    .dots {{ bottom: 116px; }}
    </style></head>
    <body>
    <div class="slide">
      <div class="{visual_class}">{visual_inner}</div>
      <div class="title-cluster">
        <div class="account-rule"><span>{safe_handle}</span></div>
        <h1 class="headline">{headline}</h1>
        <div class="cover-subtitle">{safe_subtitle} &middot; {story_label} inside</div>
      </div>
      <div class="dots">{dot_markup(1, story_count + 2, swipe_line)}</div>
    </div>
    </body></html>
    """)


def _story_slide_html(channel, story, slide_num, total_stories, image_path=None):
    safe_source = html.escape(story.get("source", ""))
    headline_text = story.get("headline", story.get("title", ""))
    body_text = story.get("body", story.get("desc", ""))
    headline_markup = phrase_text_markup(headline_text, max_chars=11)
    body_markup = phrase_text_markup(body_text, max_chars=17)
    font_size = _headline_size(headline_text)

    img_html = """<div class="visual-fallback"></div>"""
    if image_path and image_path.exists():
        img_uri = _file_uri(image_path)
        img_html = f"""\
        <div class="visual-bg" style="background-image:url('{img_uri}')"></div>
        <div class="visual-fallback"></div>"""

    return textwrap.dedent(f"""\
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    {shared_css()}
    .visual-card {{
      position: absolute; top: 0; left: 0; width: 100%; height: 620px;
      overflow: hidden; background: #151713;
    }}
    .visual-bg, .visual-fallback {{ position: absolute; inset: 0; }}
    .visual-bg {{
      z-index: 1; background-position: center; background-size: cover;
      filter: saturate(0.96) contrast(1.02);
    }}
    .visual-card::after {{
      content: ''; position: absolute; z-index: 2; inset: 0;
      background:
        linear-gradient(180deg, rgba(var(--bg-rgb), 0) 32%, rgba(var(--bg-rgb), 0.23) 62%, var(--bg) 100%);
      pointer-events: none;
    }}
    .visual-fallback {{
      z-index: 0;
      background:
        linear-gradient(135deg, rgba(var(--primary-rgb), 0.74), rgba(22, 20, 15, 0.94)),
        repeating-linear-gradient(90deg, rgba(var(--bg-rgb), 0.12) 0 2px, transparent 2px 18px);
    }}
    .story-cluster {{
      position: absolute; left: 56px; right: 56px; top: 572px; bottom: 138px;
      display: flex; flex-direction: column; justify-content: flex-start; z-index: 3;
    }}
    .account-rule {{
      display: flex; align-items: center; gap: 22px; margin-bottom: 28px; color: var(--primary);
    }}
    .account-rule::before, .account-rule::after {{ content: ''; flex: 1; height: 2px; background: var(--rule); }}
    .account-rule span {{
      font-size: 24px; font-weight: 820; letter-spacing: 0; line-height: 1;
      text-transform: uppercase; color: var(--primary);
    }}
    .story-headline {{
      font-size: {font_size}px; font-weight: 850; letter-spacing: 0; line-height: 1.05;
      color: var(--fg); text-wrap: balance; word-break: normal;
    }}
    .story-headline .jp-phrase, .story-body .jp-phrase {{ display: inline-block; }}
    .story-headline .term, .story-body .term {{ white-space: nowrap; }}
    .story-rule {{ width: 100%; height: 2px; margin: 28px 0 24px; background: var(--rule); }}
    .story-body {{
      color: var(--ink-soft); font-size: 32px; line-height: 1.34; font-weight: 640;
      max-height: 176px; overflow: hidden;
    }}
    .story-source {{
      position: absolute; left: 72px; right: 72px; bottom: 96px; z-index: 3;
      color: var(--primary); font-size: 22px; font-weight: 820; letter-spacing: 0.10em;
      line-height: 1; text-align: center; text-transform: uppercase;
    }}
    .dots {{ bottom: 50px; }}
    </style></head>
    <body>
    <div class="slide">
      <div class="visual-card">{img_html}</div>
      <div class="story-cluster">
        <div class="account-rule"><span>{slide_num:02d} / {total_stories:02d}</span></div>
        <h1 class="story-headline">{headline_markup}</h1>
        <div class="story-rule"></div>
        <div class="story-body">{body_markup}</div>
      </div>
      <div class="story-source">{safe_source}</div>
      <div class="dots">{dot_markup(slide_num + 1, total_stories + 2)}</div>
    </div>
    </body></html>
    """)


def _cta_slide_html(channel, slide_num, total):
    cta = carousel_cta_copy()
    safe_handle = html.escape(channel.handle)
    safe_kicker = html.escape(cta.get("kicker", "FOLLOW"))
    safe_headline = html.escape(cta.get("headline", "Follow for more"))
    safe_action = html.escape(cta.get("action", "Follow + Save"))

    return textwrap.dedent(f"""\
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    {shared_css()}
    .cta-shell {{
      position: absolute; inset: 112px 72px 150px;
      display: flex; flex-direction: column;
      justify-content: center; align-items: center; text-align: center;
    }}
    .cta-kicker {{
      display: flex; align-items: center; gap: 20px;
      margin-bottom: 54px; color: var(--primary);
    }}
    .cta-kicker::before, .cta-kicker::after {{
      content: ''; flex: 1; height: 2px; background: var(--rule);
    }}
    .cta-kicker span {{
      font-size: 24px; font-weight: 700; letter-spacing: 0.22em;
      text-transform: uppercase;
    }}
    .cta-headline {{
      font-size: 68px; font-weight: 800; letter-spacing: -0.03em;
      line-height: 1.1; color: var(--fg); margin-bottom: 40px;
    }}
    .cta-action {{
      background: var(--primary); color: #fff;
      font-size: 28px; font-weight: 700; padding: 20px 56px;
      border-radius: 40px; letter-spacing: 0.04em; text-transform: uppercase;
    }}
    .cta-handle {{
      position: absolute; bottom: 80px; left: 0; right: 0;
      text-align: center; font-size: 26px; font-weight: 700;
      color: var(--muted); letter-spacing: 0.08em;
    }}
    .cta-counter {{
      position: absolute; top: 76px; right: 72px;
      font-size: 22px; font-weight: 700; color: var(--muted);
      letter-spacing: 0.06em;
    }}
    </style></head>
    <body>
    <div class="slide" style="width:{SLIDE_W}px;height:{SLIDE_H}px">
      <div class="cta-counter">{slide_num} / {total}</div>
      <div class="cta-shell">
        <div class="cta-kicker"><span>{safe_kicker}</span></div>
        <div class="cta-headline">{safe_headline}</div>
        <div class="cta-action">{safe_action}</div>
      </div>
      <div class="cta-handle">{safe_handle}</div>
    </div>
    </body></html>
    """)


# ─────────────────────────── Builder ───────────────────────────

def build_daily_carousel(stories, channel, voice, out_dir, *, use_images=True):
    """Render full daily carousel with images."""
    out_dir.mkdir(parents=True, exist_ok=True)
    total_slides = len(stories) + 2
    slides_manifest = []

    vmap = {}
    if voice and voice.get("stories"):
        for vs in voice["stories"]:
            vmap[vs.get("n", 0)] = vs

    # ── Slide 1: Cover ──
    cover_headline = "AI News Today"
    cover_subtitle = "The latest in AI and tech"
    cover_swipe_line = "swipe for more"

    if voice:
        cover_headline = voice.get("cover_headline") or cover_headline
        cover_subtitle = voice.get("cover_subtitle") or cover_subtitle
        cover_swipe_line = voice.get("cover_swipe_line") or cover_swipe_line
    elif stories:
        top = stories[0]
        cover_headline = "AI News Today"
        cover_subtitle = f"{_clamp_words(top['title'], 78)} + {max(0, len(stories) - 1)} more"

    # ── Images: cover ──
    cover_image = None
    if use_images:
        print("  Generating VCPH OS-style magazine cover via GPT Image 2.0...")
        cover_image = _generate_cover_image(
            stories,
            out_dir,
            channel,
            voice=voice,
            cover_headline=cover_headline,
            cover_subtitle=cover_subtitle,
        )

    headline_html, headline_text = _headline_with_accent(cover_headline)
    html_text = _cover_slide_html(
        channel, headline_html, headline_text, cover_subtitle, cover_swipe_line,
        len(stories), bg_image=cover_image,
    )
    (out_dir / "slide_01.html").write_text(html_text, encoding="utf-8")
    render_html_slide(out_dir / "slide_01.html", out_dir / "slide_01.png")
    slides_manifest.append({
        "file": "slide_01.png", "type": "cover",
        "headline": cover_headline, "subtitle": cover_subtitle,
        "cover_image": str(cover_image) if cover_image else None,
    })
    img_note = " [with image]" if cover_image else ""
    print(f"  [1/{total_slides}] cover{img_note}: {cover_headline[:60]}")

    # ── Slides 2..N+1: Stories ──
    for i, story in enumerate(stories):
        slide_num = i + 2
        vs = vmap.get(i + 1, {})
        enriched = dict(story)
        if vs.get("headline"):
            enriched["headline"] = vs["headline"]
        if vs.get("body"):
            enriched["body"] = vs["body"]

        # Try to get an image for this story
        story_image = None
        if use_images:
            article_url = story.get("link", "")
            if article_url:
                story_image = _fetch_article_image(article_url, out_dir)
            if not story_image and _load_openai_key():
                story_image = _generate_story_image(story, out_dir)

        html_text = _story_slide_html(
            channel, enriched, slide_num - 1, len(stories),
            image_path=story_image,
        )
        (out_dir / f"slide_{slide_num:02d}.html").write_text(html_text, encoding="utf-8")
        render_html_slide(
            out_dir / f"slide_{slide_num:02d}.html",
            out_dir / f"slide_{slide_num:02d}.png",
        )
        headline = enriched.get("headline", enriched.get("title", ""))
        img_note = " [img]" if story_image else ""
        slides_manifest.append({
            "file": f"slide_{slide_num:02d}.png", "type": "story",
            "source": story.get("source", ""),
            "headline": headline,
            "body": enriched.get("body", enriched.get("desc", ""))[:200],
            "url": story.get("link", ""),
            "image": str(story_image) if story_image else None,
        })
        print(f"  [{slide_num}/{total_slides}]{img_note} {story['source']}: {headline[:60]}")

    # ── Last slide: CTA ──
    cta_num = total_slides
    render_cta_slide(out_dir / f"slide_{cta_num:02d}.png", cta_num, total_slides)
    slides_manifest.append({
        "file": f"slide_{cta_num:02d}.png", "type": "cta",
        "action": "Follow + Save",
    })
    print(f"  [{cta_num}/{total_slides}] CTA: Follow {channel.handle}")

    # ── Manifest ──
    instagram_caption = ""
    if voice:
        instagram_caption = _strip_for_vcph(voice.get("instagram_caption", ""))
    if not instagram_caption:
        caption_lines = [cover_headline]
        for s in stories:
            caption_lines.append(f"\n{s.get('source', '')}: {s.get('title', '')}")
        caption_lines.append(f"\n\nFollow {channel.handle} for daily AI news.")
        caption_lines.append("#ai #tech #news #philippines #vibecodersph")
        instagram_caption = "\n".join(caption_lines)

    manifest = {
        "pipeline": "daily_carousel",
        "channel_id": channel.id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "story_count": len(stories),
        "total_slides": total_slides,
        "stories": [
            {"title": s["title"], "source": s["source"], "link": s.get("link", "")}
            for s in stories
        ],
        "slides": slides_manifest,
        "instagram_caption": instagram_caption,
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n  -> {manifest_path}")
    return manifest_path


# ─────────────────────────── CLI ───────────────────────────

def main():
    load_env_file(ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Build a daily AI/tech news carousel from RSS feeds"
    )
    parser.add_argument("--channel", default=os.environ.get("CAROUSEL_CHANNEL"),
                        help="Active channel ID")
    parser.add_argument("--max-stories", type=int, default=DEFAULT_MAX_STORIES,
                        help="Number of story slides")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT,
                        help="Output directory")
    parser.add_argument("--registry", type=Path,
                        default=ROOT / "vcph_source_registry.json",
                        help="Path to source registry JSON")
    parser.add_argument("--db-path", type=Path,
                        help="Path to SQLite dedupe DB")
    parser.add_argument("--no-x-trending", action="store_true",
                        help="Disable X trending story slot")
    parser.add_argument("--no-voice", action="store_true",
                        help="Skip LLM voice rewrite, use raw titles")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip image generation/scraping, text-only slides")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and score only, no render")
    parser.add_argument("--verbose", "-v", action="store_true", default=True,
                        help="Verbose output")
    args = parser.parse_args()

    if args.channel:
        os.environ["CAROUSEL_CHANNEL"] = args.channel
    channel = load_channel()
    use_images = not args.no_images
    print(f"Channel: {channel.id} | {channel.brand_name} | {channel.language_name}")
    print(f"Images: {'on' if use_images else 'off'}")

    print(f"\nFetching stories from {args.registry}...")
    stories = get_diverse_stories(
        registry_path=str(args.registry),
        db_path=str(args.db_path) if args.db_path else None,
        max_stories=args.max_stories,
        include_x_trending=not args.no_x_trending,
        verbose=args.verbose,
    )

    if not stories:
        print("  No stories found. Aborting.")
        return 1

    print(f"\nSelected {len(stories)} stories:")
    for i, s in enumerate(stories, 1):
        print(f"  {i}. [{s['source']}] {s['title'][:80]}")

    if args.dry_run:
        print("\n[Dry run] Skipping render.")
        return 0

    voice = None
    if not args.no_voice:
        print("\nRewriting in channel voice...")
        voice = rewrite_stories_daily_carousel(stories, channel)
        if voice:
            print(f"  Cover: {voice.get('cover_headline', '')[:80]}")
            story_rows = voice.get("stories")
            if isinstance(story_rows, list):
                for vs in story_rows:
                    if isinstance(vs, dict):
                        print(f"  Story {vs.get('n')}: {str(vs.get('headline', ''))[:60]}")
        else:
            print("  Voice rewrite skipped/unavailable, using raw titles")

    print(f"\nRendering {len(stories) + 2} slides...")
    manifest_path = build_daily_carousel(
        stories=stories, channel=channel, voice=voice, out_dir=args.out_dir,
        use_images=use_images,
    )

    record_posted_stories(
        stories,
        db_path=str(args.db_path) if args.db_path else None,
    )

    print(f"\nDone. {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
