#!/usr/bin/env python3
"""
Daily AI/tech news carousel builder for carousel-app.

Uses the same RSS feed pipeline as VCPH OS (via vcph_feed_pipeline.py) but
renders multi-slide carousels with images instead of a single magazine cover.

    uv run python build_daily_carousel.py
    uv run python build_daily_carousel.py --no-images  # text-only fallback

Slides:
  1. Cover/hook: Daily Drop cover photo background + carousel-owned text/branding
  2..N+1. Story slide: article image (scraped or generated) + headline + body
  Last. CTA: follow @handle

Cover image priority: Daily Drop cover-photo generator for 5-story VCPH runs,
then GPT Image 2.0 text-free compilation fallback.
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
COVER_SIZE = "1024x1280"
STORY_IMAGE_SIZE = "1024x1024"
BASE_VOICE_GUIDE = ROOT / "brand" / "VIBECODERS_IG_VOICE.md"
DEFAULT_SWIPE_CUE = "Swipe for more →"

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


_TAGLISH_MARKERS = {
    "ang", "ano", "ba", "bagong", "bakit", "dapat", "dito", "gamit", "gamitin",
    "hindi", "ito", "iyan", "kasi", "kay", "kung", "lang", "mas", "may",
    "mga", "mo", "na", "naka", "nasa", "ng", "ni", "pa", "para", "pero",
    "pwede", "sa", "si", "sila", "tayo", "wala",
}


def _is_taglish_channel(channel) -> bool:
    return str(getattr(channel, "language_name", "")).lower() == "taglish"


def _has_taglish_marker(text: str) -> bool:
    tokens = re.findall(r"[A-Za-zÀ-ÿ']+", str(text or "").lower())
    return any(token in _TAGLISH_MARKERS for token in tokens)


def _sanitize_daily_voice_data(data: dict[str, Any]) -> dict[str, Any]:
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


def _daily_voice_issues(data: dict[str, Any], expected_count: int, channel) -> list[str]:
    issues: list[str] = []
    story_rows = data.get("stories") if isinstance(data, dict) else None
    if not isinstance(story_rows, list):
        return ["stories is missing"]

    by_n = {
        row.get("n"): row
        for row in story_rows
        if isinstance(row, dict)
    }
    for n in range(1, expected_count + 1):
        row = by_n.get(n)
        if not row:
            issues.append(f"story {n} missing")
            continue
        headline = str(row.get("headline") or "")
        if not headline:
            issues.append(f"story {n} headline missing")
        if _is_taglish_channel(channel) and not _has_taglish_marker(headline):
            issues.append(f"story {n} headline is not Taglish enough")

    if _is_taglish_channel(channel):
        for key in ("cover_headline", "cover_subtitle", "instagram_caption"):
            value = str(data.get(key) or "")
            if not value:
                issues.append(f"{key} missing")
            elif not _has_taglish_marker(value):
                issues.append(f"{key} is not Taglish enough")
    return issues


def _repair_daily_voice_with_gemini(
    data: dict[str, Any],
    stories,
    channel,
    api_key: str,
    guide: str,
    channel_voice: str,
    issues: list[str],
) -> dict[str, Any] | None:
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
Repair this Instagram carousel JSON so it fully follows the VibeCoders PH Taglish voice.

Return JSON only, with the same schema and the same story order.

Problems found:
{json.dumps(issues, ensure_ascii=False, indent=2)}

Hard repair rules:
- Every story headline, including stories 2 to 5, must be Taglish-native, not straight English.
- Every story headline must contain at least one Filipino connector or phrase such as "may", "sa", "ng", "para", "nasa", "gamit", "ito", "pero", "kasi", "pwede", or "bagong".
- Keep source facts, product names, company names, dates, amounts, and technical nouns intact.
- Do not invent claims. Do not add jokes to story bodies.
- Keep cover_headline with exactly one [accent] word.
- Keep cover_subject as text-free cover-photo art direction using orange or terracotta, never purple, pink, violet, or magenta.
- No em dashes.

{channel.brand_name} channel voice block:
{channel_voice}

Base VibeCodersPH voice guide:
{guide[:7000]}

Source stories:
{json.dumps(raw_list, ensure_ascii=False, indent=2)}

Current JSON to repair:
{json.dumps(data, ensure_ascii=False, indent=2)}
""".strip()

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    response = gemini_generate_content(
        gemini_text_model(),
        api_key,
        payload,
        api_version=os.environ.get("GEMINI_TEXT_API_VERSION") or "v1beta",
        timeout=90,
    )
    repaired = parse_json_object(extract_gemini_text(response))
    if not isinstance(repaired, dict):
        return None
    return _sanitize_daily_voice_data(repaired)


def _display_stories_for_render(stories, voice):
    """Story rows after voice rewrite, used by visible cover/list/story copy."""
    voice_rows = _daily_drop_voice_stories(stories, voice)
    display = []
    for story, row in zip(stories, voice_rows):
        enriched = dict(story)
        if row.get("headline"):
            enriched["headline"] = row["headline"]
        if row.get("blurb"):
            enriched["body"] = row["blurb"]
        display.append(enriched)
    return display


def _generate_daily_drop_magazine_cover(
    stories,
    out_dir,
    channel,
    voice=None,
    cover_headline="",
    cover_subtitle="",
):
    """Generate a VCPH Daily Drop cover photo background and copy it into out_dir."""
    if channel.id != "vibecodersph":
        return None

    voice_stories = _daily_drop_voice_stories(stories, voice)
    if not voice_stories:
        return None
    if len(voice_stories) < 5:
        print("  [cover] Daily Drop cover photo needs 5 stories; using montage fallback")
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
        # cover_subject now describes all 5 stories as a montage; check if it
        # references ANY story, not just story #1
        matches_any = any(_text_matches_story(cover_subject, s) for s in stories)
        if not matches_any:
            print("  [cover guard] cover subject did not match any story; rebuilding from stories")
            cover_subject = ""

    if not cover_subject:
        story_summaries = "; ".join(s.get("title", "")[:60] for s in stories)
        cover_subject = (
            "A hyperrealistic editorial montage for a VibeCoders PH Daily Drop cover photo "
            f"combining these stories into one unified image: {story_summaries}. "
            "Place subjects side-by-side in a shared environment with warm cream, deep ink, "
            "and burnt orange accents. No text, no logos."
        )

    digest_payload = {
        "stories": [s.get("title", "") for s in stories],
        "cover_headline": cover_headline,
        "cover_subtitle": cover_subtitle,
        "cover_subject": cover_subject,
        "cover_style": cover_style,
        "carousel_mode": True,
        "cover_asset_version": 2,
    }
    digest = hashlib.sha1(
        json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:10]
    img_path = out_dir / "images" / f"cover-photo-{digest}.jpg"
    img_path.parent.mkdir(parents=True, exist_ok=True)
    if img_path.exists():
        print(f"  [cover] using cached Daily Drop cover photo -> {img_path.name}")
        return img_path

    try:
        generated = generate_daily_drop_full_cover(
            voice_stories,
            hero_cover_line=hero_cover_line,
            cover_subject=cover_subject,
            cover_style=cover_style,
            output_path=img_path,
            skip_logo_overlay=True,
            cover_size="1024x1280",
        )
        if generated and Path(str(generated)).exists():
            return img_path
    except Exception as e:
        print(f"  [cover] Daily Drop cover photo failed: {e}")
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

    Preferred path: VCPH Daily Drop cover photo. Fallback: the older carousel
    montage background.

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
        "A text-free editorial cover photo background for an AI/tech news Instagram carousel. "
        "The composition should feel like a Wired or Fast Company cover: a montage or "
        "collage of abstract editorial illustrations representing today's top stories. "
        f"Background: {bg_hex} warm cream paper texture with dark ink accents "
        f"and {primary_hex} terracotta/rust highlights. "
        "Style: abstract geometric, editorial magazine aesthetic, ink-wash technique, "
        "grain texture, restrained composition. No visible text, no logos, no letters, "
        "no numbers, no words, no UI elements. The carousel typography will be overlaid "
        "separately. Keep the lower-left and bottom edge calm enough for overlaid text. "
        "Make it visually striking, a single hero image that represents the day's AI news "
        "collectively. Portrait 4:5 Instagram carousel format.\n\n"
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
  "cover_subtitle": "one short Taglish sentence that is a blurb ONLY about story n=1, 12 to 18 words. This is a subtitle for the hero story headline. Do NOT mention other stories, do not say 'plus', do not list anything. Just summarize what story n=1 is about.",
  "cover_swipe_line": "use exactly: Swipe for more →",
  "cover_subject": "2 to 4 sentences describing a HYPERREALISTIC EDITORIAL MONTAGE cover photo that combines ALL 5 stories into ONE unified image. Describe the scene: characters, objects, settings from each story placed side-by-side or in a shared environment. Think magazine cover illustration where every story has a visual presence.",
  "cover_style": "empty string, or one obvious Daily Drop style key only when the story clearly needs it",
  "instagram_caption": "short Taglish caption with one hook, one useful line, one CTA, clean hashtags",
  "stories": [
    {{"n": 1, "headline": "6 to 10 words", "body": "1 to 2 sentences, 18 to 25 words"}}
  ]
}}

Voice rules:
- Use Gemini to write in the VibeCoders PH Instagram voice below, not generic news voice.
- Taglish-native for the cover, caption, framing lines, and every story headline. Body slides can lean English for technical precision, but should still sound like a Pinoy builder talking to barkada.
- Every story headline, including stories n=2 to n=5, must contain at least one Filipino connector or phrase such as "may", "sa", "ng", "para", "nasa", "gamit", "ito", "pero", "kasi", "pwede", or "bagong". Do not return straight-English story headlines.
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
- Treat story n=1 as the hero story. cover_headline AND cover_subtitle must be about story n=1 ONLY. cover_subject must combine ALL 5 stories into one montage scene.
- cover_subtitle is a SUBTITLE for the hero headline. It expands on story n=1 with one additional sentence of context. Do NOT mention stories 2-5 in the subtitle. Do NOT write a table of contents or use the word \"plus\".
- cover_subject should think like a magazine cover illustrator: place subjects, characters, objects, or settings from each of the 5 stories into ONE shared hyperrealistic editorial image. Use side-by-side composition, a shared environment, or a surreal editorial tableau. Every story must have a recognizable visual element in the scene. Do not request text, logos, mastheads, screens full of labels, a generic AI dashboard, phone, laptop, glowing robot, chart wallpaper, or model-name screen.
- cover_subject should use the VibeCoders PH orange/terracotta accent system, not purple, pink, or magenta.
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

        data = _sanitize_daily_voice_data(data)
        issues = _daily_voice_issues(data, len(stories), channel)
        if issues:
            print("  [voice guard] Gemini output missed the channel voice; requesting repair")
            for issue in issues[:8]:
                print(f"    - {issue}")
            repaired = _repair_daily_voice_with_gemini(
                data, stories, channel, api_key, guide, channel_voice, issues
            )
            if repaired:
                repaired_issues = _daily_voice_issues(repaired, len(stories), channel)
                if not repaired_issues:
                    data = repaired
                else:
                    print("  [warn] Gemini repair still missed voice requirements")
                    for issue in repaired_issues[:8]:
                        print(f"    - {issue}")
                    if _is_taglish_channel(channel):
                        return None
            elif _is_taglish_channel(channel):
                return None

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


def _brand_logo_html(channel, css_class: str) -> str:
    if channel.id != "vibecodersph":
        return ""
    logo_path = ROOT / "assets" / "vibecodersph_logo.png"
    if not logo_path.exists():
        return ""
    logo_uri = _file_uri(logo_path)
    return (
        f'<img class="{css_class}" src="{logo_uri}" '
        f'alt="{html.escape(channel.brand_name)}">'
    )


def _cover_slide_html(channel, headline, headline_text, subtitle, swipe_line, stories, bg_image=None):
    story_count = len(stories)
    safe_handle = html.escape(channel.handle)
    safe_subtitle = html.escape(str(subtitle or "").rstrip(" ."))
    font_size = _headline_size(headline_text, cover=True)
    total_slides = story_count + 2
    logo_html = _brand_logo_html(channel, "cover-logo")
    lower_items = []
    for index, story in enumerate(stories[1:5], start=2):
        title = story.get("headline") or story.get("title") or ""
        lower_items.append(
            f'<li><span>{index:02d}</span><b>{html.escape(_clamp_words(title, 86))}</b></li>'
        )
    lower_html = "\n".join(lower_items)

    visual_inner = """<div class="cover-fallback"></div>"""
    if bg_image and bg_image.exists():
        bg_uri = _file_uri(bg_image)
        visual_inner = f"""<div class="cover-bg" style="background-image:url('{bg_uri}')"></div><div class="cover-fallback"></div>"""

    return textwrap.dedent(f"""\
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    {shared_css()}
    .slide {{ background: #0d0b08; }}
    .cover-art {{
      position: absolute; inset: 0; overflow: hidden; background: #151713;
    }}
    .cover-bg, .cover-fallback {{ position: absolute; inset: 0; }}
    .cover-bg {{
      z-index: 1; background-position: center; background-size: cover;
      filter: saturate(0.98) contrast(1.03);
    }}
    .cover-art::after {{
      content: ''; position: absolute; z-index: 2; inset: 0;
      background:
        linear-gradient(90deg, rgba(13,11,8,0.78) 0%, rgba(13,11,8,0.48) 48%, rgba(13,11,8,0.16) 100%),
        linear-gradient(180deg, rgba(13,11,8,0.62) 0%, rgba(13,11,8,0.04) 34%, rgba(13,11,8,0.20) 64%, rgba(13,11,8,0.78) 100%);
      pointer-events: none;
    }}
    .cover-fallback {{
      z-index: 0;
      background:
        radial-gradient(circle at 78% 24%, rgba(var(--primary-rgb), 0.34) 0 16%, transparent 17%),
        linear-gradient(135deg, rgba(22, 20, 15, 0.98), rgba(var(--primary-rgb), 0.64)),
        repeating-linear-gradient(90deg, rgba(var(--bg-rgb), 0.12) 0 2px, transparent 2px 18px);
    }}
    .cover-handle, .cover-progress {{
      position: absolute; top: 48px; z-index: 4;
      color: rgba(244,242,236,0.92);
      font-size: 23px; font-weight: 840; letter-spacing: 0.12em;
      line-height: 1; text-transform: uppercase;
      text-shadow: 0 3px 18px rgba(0,0,0,0.52);
    }}
    .cover-handle {{ left: 58px; }}
    .cover-progress {{ right: 58px; letter-spacing: 0.06em; }}
    .cover-title {{
      position: absolute; left: 58px; right: 250px; top: 400px; z-index: 4;
      color: #fff;
      text-shadow: 0 4px 24px rgba(0,0,0,0.45);
    }}
    .cover-kicker {{
      display: flex; align-items: center; gap: 18px; width: 360px;
      margin-bottom: 18px; color: var(--primary);
    }}
    .cover-kicker::before {{ content: ''; width: 68px; height: 4px; background: var(--primary); }}
    .cover-kicker span {{
      font-size: 23px; font-weight: 860; letter-spacing: 0.10em;
      line-height: 1; text-transform: uppercase;
    }}
    .headline {{
      font-size: {font_size}px; font-weight: 850; letter-spacing: 0; line-height: 1.03;
      color: #fff; line-break: strict; overflow-wrap: normal;
      text-wrap: balance; word-break: normal;
    }}
    .headline .accent {{ color: var(--primary); }}
    .headline .jp-phrase {{ display: inline-block; }}
    .headline .term {{ white-space: nowrap; }}
    .cover-subtitle {{
      margin-top: 22px; max-width: 760px; color: rgba(244,242,236,0.86);
      font-size: 31px; line-height: 1.26; font-weight: 650;
    }}
    .cover-list {{
      position: absolute; left: 58px; right: 58px; bottom: 84px; z-index: 4;
      display: grid; grid-template-columns: 1fr; gap: 0;
      color: rgba(244,242,236,0.72);
    }}
    .cover-list-label {{
      color: var(--primary); font-size: 22px; font-weight: 860;
      letter-spacing: 0.16em; line-height: 1; text-align: center;
      text-transform: uppercase; margin-bottom: 18px;
    }}
    .cover-list ol {{ list-style: none; display: grid; gap: 0; }}
    .cover-list li {{
      display: grid; grid-template-columns: 58px 1fr; align-items: center;
      min-height: 45px; border-top: 1px solid rgba(244,242,236,0.22);
      font-size: 21px; line-height: 1.12;
    }}
    .cover-list li:last-child {{ border-bottom: 1px solid rgba(244,242,236,0.22); }}
    .cover-list li span {{ color: rgba(244,242,236,0.42); font-weight: 860; }}
    .cover-list li b {{
      color: rgba(244,242,236,0.78); font-weight: 760;
      overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
    }}
    .cover-logo {{
      position: absolute; right: 48px; bottom: 34px; z-index: 5;
      width: 148px; height: auto; opacity: 0.92;
      filter: drop-shadow(0 5px 18px rgba(0,0,0,0.46));
    }}
    .dots {{
      left: 50%; right: auto; bottom: 24px; transform: translateX(-50%);
      z-index: 6; padding: 11px 28px 12px; border-radius: 999px;
      background: rgba(244,242,236,0.94); color: var(--primary);
      box-shadow: 0 8px 28px rgba(0,0,0,0.30);
    }}
    </style></head>
    <body>
    <div class="slide">
      <div class="cover-art">{visual_inner}</div>
      <div class="cover-handle">{safe_handle}</div>
      <div class="cover-progress">01 / {total_slides:02d}</div>
      <section class="cover-title">
        <div class="cover-kicker"><span>Today's Story</span></div>
        <h1 class="headline">{headline}</h1>
        <div class="cover-subtitle">{safe_subtitle}</div>
      </section>
      <section class="cover-list">
        <div class="cover-list-label">Also in this drop</div>
        <ol>{lower_html}</ol>
      </section>
      {logo_html}
      <div class="dots">{dot_markup(1, total_slides, swipe_line)}</div>
    </div>
    </body></html>
    """)


def _story_slide_html(channel, story, slide_num, total_stories, image_path=None):
    safe_source = html.escape(story.get("source", ""))
    safe_handle = html.escape(channel.handle)
    headline_text = story.get("headline", story.get("title", ""))
    body_text = story.get("body", story.get("desc", ""))
    headline_markup = phrase_text_markup(headline_text, max_chars=11)
    body_markup = phrase_text_markup(body_text, max_chars=17)
    font_size = _headline_size(headline_text)
    total_slides = total_stories + 2

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
      position: absolute; top: 0; left: 0; width: 100%; height: 1012px;
      overflow: hidden; background: #151713;
    }}
    .visual-bg, .visual-fallback {{ position: absolute; inset: 0; }}
    .visual-bg {{
      z-index: 1; background-position: center; background-size: contain;
      background-repeat: no-repeat; background-color: #151713;
      filter: saturate(0.96) contrast(1.02);
    }}
    .visual-card::before {{
      content: ''; position: absolute; z-index: 2; left: 0; right: 0; top: 0; height: 100px;
      background: linear-gradient(180deg, rgba(13,11,8,0.52), rgba(13,11,8,0));
      pointer-events: none;
    }}
    .visual-card::after {{
      content: ''; position: absolute; z-index: 2; inset: 0;
      background:
        linear-gradient(180deg, rgba(var(--bg-rgb), 0) 56%, rgba(var(--bg-rgb), 0.34) 76%, var(--bg) 100%);
      pointer-events: none;
    }}
    .visual-fallback {{
      z-index: 0;
      background:
        linear-gradient(135deg, rgba(var(--primary-rgb), 0.74), rgba(22, 20, 15, 0.94)),
        repeating-linear-gradient(90deg, rgba(var(--bg-rgb), 0.12) 0 2px, transparent 2px 18px);
    }}
    .story-brand, .story-progress {{
      position: absolute; top: 48px; z-index: 4;
      color: rgba(244,242,236,0.94); font-size: 23px; font-weight: 840;
      letter-spacing: 0.12em; line-height: 1; text-transform: uppercase;
      text-shadow: 0 3px 18px rgba(0,0,0,0.52);
    }}
    .story-brand {{ left: 58px; }}
    .story-progress {{ right: 58px; letter-spacing: 0.06em; }}
    .story-cluster {{
      position: absolute; left: 56px; right: 56px; top: 900px; bottom: 138px;
      display: flex; flex-direction: column; justify-content: flex-start; z-index: 3;
      overflow: hidden;
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
    .story-rule {{ width: 100%; height: 2px; margin: 22px 0 18px; background: var(--rule); }}
    .story-body {{
      color: var(--ink-soft); font-size: 28px; line-height: 1.32; font-weight: 640;
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
      <div class="story-brand">{safe_handle}</div>
      <div class="story-progress">{slide_num + 1:02d} / {total_slides:02d}</div>
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
    headline_markup = phrase_text_markup(cta.get("headline", "Follow for more"), max_chars=11)
    body_markup = phrase_text_markup(cta.get("body", ""), max_chars=17)
    safe_action = html.escape(cta.get("action", "Follow + Save"))

    return textwrap.dedent(f"""\
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    {shared_css()}
    .cta-progress {{
      position: absolute; top: 76px; right: 72px;
      font-size: 23px; font-weight: 820; letter-spacing: 0.04em;
      color: rgba(20, 18, 14, 0.42);
    }}
    .cta-shell {{
      position: absolute; inset: 112px 72px 150px;
      display: flex; flex-direction: column;
      justify-content: center; text-align: left;
    }}
    .cta-kicker {{
      display: flex; align-items: center; gap: 20px;
      margin-bottom: 54px; color: var(--primary);
    }}
    .cta-kicker::before, .cta-kicker::after {{
      content: ''; flex: 1; height: 2px; background: var(--rule);
    }}
    .cta-kicker span {{
      font-size: 24px; font-weight: 840; letter-spacing: 0.18em;
      line-height: 1; text-transform: uppercase; white-space: nowrap;
    }}
    .cta-title {{
      max-width: 900px; font-size: 96px; line-height: 0.98;
      font-weight: 880; letter-spacing: 0; color: var(--fg);
      text-wrap: balance;
    }}
    .cta-title .jp-phrase, .cta-body .jp-phrase {{ display: inline-block; }}
    .cta-title .term, .cta-body .term {{ white-space: nowrap; }}
    .cta-body {{
      max-width: 850px; margin-top: 42px; color: var(--ink-soft);
      font-size: 39px; line-height: 1.28; font-weight: 640;
    }}
    .cta-action {{
      align-self: flex-start; margin-top: 54px; padding: 18px 24px 17px;
      border: 3px solid var(--primary); color: var(--primary);
      font-size: 34px; line-height: 1; font-weight: 860; letter-spacing: 0;
    }}
    .cta-handle {{
      position: absolute; left: 72px; right: 72px; bottom: 86px;
      color: var(--primary); font-size: 24px; font-weight: 840;
      letter-spacing: 0.16em; line-height: 1; text-align: center;
      text-transform: uppercase;
    }}
    </style></head>
    <body>
    <div class="slide" style="width:{SLIDE_W}px;height:{SLIDE_H}px">
      <div class="cta-progress">{slide_num:02d} / {total:02d}</div>
      <div class="cta-shell">
        <div class="cta-kicker"><span>{safe_kicker}</span></div>
        <h1 class="cta-title">{headline_markup}</h1>
        <div class="cta-body">{body_markup}</div>
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
    display_stories = _display_stories_for_render(stories, voice)

    vmap = {}
    if voice and voice.get("stories"):
        for vs in voice["stories"]:
            vmap[vs.get("n", 0)] = vs

    # ── Slide 1: Cover ──
    cover_headline = "AI News Today"
    cover_subtitle = "The latest in AI and tech"
    cover_swipe_line = DEFAULT_SWIPE_CUE

    if voice:
        cover_headline = voice.get("cover_headline") or cover_headline
        cover_subtitle = voice.get("cover_subtitle") or cover_subtitle
        cover_swipe_line = DEFAULT_SWIPE_CUE
    elif stories:
        top = stories[0]
        cover_headline = "AI News Today"
        cover_subtitle = f"{_clamp_words(top['title'], 78)} + {max(0, len(stories) - 1)} more"

    # ── Images: cover ──
    cover_image = None
    if use_images:
        print("  Generating VCPH Daily Drop cover photo via GPT Image 2.0...")
        cover_image = _generate_cover_image(
            stories,
            out_dir,
            channel,
            voice=voice,
            cover_headline=cover_headline,
            cover_subtitle=cover_subtitle,
        )
        if channel.id == "vibecodersph" and not cover_image:
            raise RuntimeError(
                "VibeCoders PH image builds require a generated GPT Image 2.0 cover photo. "
                "Check OPENAI_API_KEY or rerun with --no-images only for diagnostics."
            )

    headline_html, headline_text = _headline_with_accent(cover_headline)
    html_text = _cover_slide_html(
        channel, headline_html, headline_text, cover_subtitle, cover_swipe_line,
        display_stories, bg_image=cover_image,
    )
    (out_dir / "slide_01.html").write_text(html_text, encoding="utf-8")
    render_html_slide(out_dir / "slide_01.html", out_dir / "slide_01.png")
    slides_manifest.append({
        "file": "slide_01.png", "type": "cover",
        "path": str(out_dir / "slide_01.png"),
        "headline": cover_headline, "subtitle": cover_subtitle,
        "cover_image": str(cover_image) if cover_image else None,
    })
    img_note = " [with image]" if cover_image else ""
    print(f"  [1/{total_slides}] cover{img_note}: {cover_headline[:60]}")

    # ── Slides 2..N+1: Stories ──
    for i, story in enumerate(stories):
        slide_num = i + 2
        vs = vmap.get(i + 1, {})
        enriched = dict(display_stories[i])
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
            "path": str(out_dir / f"slide_{slide_num:02d}.png"),
            "source": story.get("source", ""),
            "headline": headline,
            "body": enriched.get("body", enriched.get("desc", ""))[:200],
            "url": story.get("link", ""),
            "image": str(story_image) if story_image else None,
        })
        print(f"  [{slide_num}/{total_slides}]{img_note} {story['source']}: {headline[:60]}")

    # ── Last slide: CTA ──
    cta_num = total_slides
    cta_html = _cta_slide_html(channel, cta_num, total_slides)
    (out_dir / f"slide_{cta_num:02d}.html").write_text(cta_html, encoding="utf-8")
    render_html_slide(
        out_dir / f"slide_{cta_num:02d}.html",
        out_dir / f"slide_{cta_num:02d}.png",
    )
    slides_manifest.append({
        "file": f"slide_{cta_num:02d}.png", "type": "cta",
        "path": str(out_dir / f"slide_{cta_num:02d}.png"),
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
            {
                "title": s["title"],
                "display_title": display_stories[i].get("headline", s["title"]),
                "source": s["source"],
                "link": s.get("link", ""),
            }
            for i, s in enumerate(stories)
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
            print("  Voice rewrite skipped/unavailable.")
            if channel.language_name.lower() == "taglish":
                print(
                    "  Error: Taglish carousel builds require Gemini voice generation. "
                    "Set GOOGLE_API_KEY/GEMINI_API_KEY or pass --no-voice only for diagnostics.",
                    file=sys.stderr,
                )
                return 1
            print("  Using raw titles for this non-Taglish channel")

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
