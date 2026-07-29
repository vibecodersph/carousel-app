#!/usr/bin/env python3
"""Build a curated "top AI news of the week" carousel, sensitive to the channel.

This is a *roundup* builder: instead of deep-diving one URL, it gathers the
highest-signal stories of the past week (scored by story_scout) and turns each
into one slide -- cover, one slide per story, then an outro.

Channel sensitivity is the point. A *channel* (see channel.py) bundles branding,
language, audience, and brand voice. The same week of news rendered through the
``vibecodersph`` channel speaks witty Taglish on the cream/ink VibeCoders theme;
rendered through ``aibrief_jp`` it speaks Japanese on the charcoal AI Brief JP
theme. Pick one with ``--channel`` (or CAROUSEL_CHANNEL); every load_channel()
call in the process then resolves to it.

    uv run python build_weekly_carousel.py --channel vibecodersph
    uv run python build_weekly_carousel.py --channel aibrief_jp --max-stories 6

Story source priority: an explicit ``--input stories.json``; otherwise the
story_scout candidate queue (out/automation/candidates.json) filtered to the
last ``--days`` days. Cover copy and per-story headlines/summaries are written
in the channel's language by Gemini when GOOGLE_API_KEY is set, with a local
fallback otherwise.

Page cap: Instagram carousels support up to 20 slides. A roundup spends one on
the cover and one on the outro, so stories are capped at 18 (default 7).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from build_article_carousel import (
    clamp_words,
    clean_article_text,
    compact_headline,
    split_sentences,
)
from build_x_carousel import (
    build_title_enrichment,
    cover_poster_path,
    dot_markup,
    extract_gemini_text,
    gemini_api_key,
    gemini_generate_content,
    gemini_text_model,
    load_env_file,
    parse_json_object,
    render_animated_title_slide,
    render_html_slide,
    shared_css,
    string_value,
)
from channel import Channel, load_channel
from weekly_verifier import (
    SlideRecord,
    assert_no_blocked,
    verify_records,
    write_run_manifest,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "out" / "weekly_carousel"
DEFAULT_QUEUE = ROOT / "out" / "automation" / "candidates.json"

# Instagram carousels support up to 20 slides. A roundup reserves one for the
# cover and one for the outro, so at most 18 stories fit; 7 reads best by default.
MAX_TOTAL_SLIDES = 20
MAX_STORIES = MAX_TOTAL_SLIDES - 2  # cover + outro
DEFAULT_STORIES = 7
MIN_STORIES = 3
DEFAULT_PER_SOURCE = 2  # avoid one loud account dominating the week


@dataclass
class WeeklyStory:
    rank: int
    author: str
    handle: str
    url: str
    text: str
    date: str
    score: int
    source_type: str
    reasons: list[str] = field(default_factory=list)
    source_text: str = ""
    source_name: str = ""
    source_handle: str = ""
    category: str = ""
    hero_metric: str = ""
    hero_label: str = ""
    copy_locked: bool = False
    # Channel-voice copy, filled in by curate_copy:
    kicker: str = ""
    headline: str = ""
    summary: str = ""


# --------------------------------------------------------------------------- #
# Story gathering
# --------------------------------------------------------------------------- #
def parse_iso(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _story_from_candidate(candidate: dict[str, Any]) -> WeeklyStory | None:
    post = candidate.get("post") if isinstance(candidate.get("post"), dict) else {}
    text = clean_article_text(string_value(post.get("text") or post.get("title")))
    url = string_value(post.get("url"))
    if not text or not url:
        return None
    handle = string_value(post.get("handle"))
    author = string_value(post.get("author")) or handle.lstrip("@")
    cid = str(candidate.get("id") or "")
    source_type = "article" if cid.startswith("article_") else "x"
    return WeeklyStory(
        rank=0,
        author=author,
        handle=handle,
        url=url,
        text=text,
        date=string_value(post.get("date")),
        score=int(candidate.get("score") or 0),
        source_type=source_type,
        reasons=[str(r) for r in (candidate.get("score_reasons") or []) if r],
        source_text=text,
        source_name=author,
        source_handle=handle,
    )


def gather_top_stories(
    *,
    queue_path: Path,
    input_path: Path | None,
    days: int,
    max_stories: int,
    per_source: int,
) -> list[WeeklyStory]:
    """Rank the week's stories: explicit input first, else the scout queue."""
    raw: list[WeeklyStory] = []
    if input_path:
        data = json.loads(input_path.read_text(encoding="utf-8"))
        items = data.get("stories", data) if isinstance(data, dict) else data
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            text = clean_article_text(
                string_value(item.get("text") or item.get("title") or item.get("source_text") or item.get("body"))
            )
            url = string_value(item.get("source_url") or item.get("url"))
            if not text or not url:
                continue
            raw.append(
                WeeklyStory(
                    rank=0,
                    author=string_value(item.get("author") or item.get("source_name")),
                    handle=string_value(item.get("handle") or item.get("source_handle")),
                    url=string_value(item.get("source_url")) or url,
                    text=text,
                    date=string_value(item.get("date")),
                    score=int(item.get("score") or 0),
                    source_type=string_value(item.get("source_type")) or "x",
                    reasons=[str(r) for r in (item.get("reasons") or []) if r],
                    source_text=string_value(item.get("source_text")) or text,
                    source_name=string_value(item.get("source_name") or item.get("author")),
                    source_handle=string_value(item.get("source_handle") or item.get("handle")),
                    category=string_value(item.get("category") or item.get("kicker")).upper(),
                    hero_metric=string_value(item.get("hero_metric")),
                    hero_label=string_value(item.get("hero_label")),
                    kicker=string_value(item.get("kicker") or item.get("category")).upper(),
                    headline=string_value(item.get("headline")),
                    summary=string_value(item.get("summary") or item.get("body")),
                    copy_locked=bool(item.get("headline") and (item.get("summary") or item.get("body"))),
                )
            )
    else:
        if not queue_path.exists():
            raise SystemExit(
                f"no story queue at {queue_path}. Run `uv run python story_scout.py "
                f"scan --config story_sources.json` first, or pass --input <stories.json>."
            )
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        candidates = queue.get("candidates", []) if isinstance(queue, dict) else []
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        in_window: list[WeeklyStory] = []
        all_stories: list[WeeklyStory] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            story = _story_from_candidate(candidate)
            if not story:
                continue
            all_stories.append(story)
            created = parse_iso(string_value(candidate.get("created_at")))
            if created is None or created >= cutoff:
                in_window.append(story)
        # Prefer the dated window; fall back to all-time top-N if it is too thin
        # (a quiet week, or a queue whose timestamps predate the window).
        raw = in_window
        if len({s.url for s in raw}) < min(MIN_STORIES, max_stories):
            print(
                f"[weekly] only {len(raw)} story(ies) in the last {days} days; "
                f"ranking the all-time top of the queue instead"
            )
            raw = all_stories

    # Dedupe by URL, then rank by score with a per-source cap for variety.
    best_by_url: dict[str, WeeklyStory] = {}
    for story in raw:
        existing = best_by_url.get(story.url)
        if existing is None or story.score > existing.score:
            best_by_url[story.url] = story
    ordered = sorted(best_by_url.values(), key=lambda s: -s.score)

    picked: list[WeeklyStory] = []
    per_handle: dict[str, int] = {}
    for story in ordered:
        key = (story.handle or story.author).lower()
        if per_source and per_handle.get(key, 0) >= per_source:
            continue
        per_handle[key] = per_handle.get(key, 0) + 1
        picked.append(story)
        if len(picked) >= max_stories:
            break
    # If the per-source cap starved us, top up ignoring the cap.
    if len(picked) < min(max_stories, len(ordered)):
        chosen = {s.url for s in picked}
        for story in ordered:
            if story.url in chosen:
                continue
            picked.append(story)
            if len(picked) >= max_stories:
                break

    for index, story in enumerate(picked, start=1):
        story.rank = index
    return picked


# --------------------------------------------------------------------------- #
# Channel-voice copy
# --------------------------------------------------------------------------- #
def is_japanese(channel: Channel) -> bool:
    return channel.language_name.lower().startswith("japanese")


def localized_labels(channel: Channel, *, start: datetime, end: datetime, count: int) -> dict[str, str]:
    """Structural slide strings localized per channel language (not AI-written)."""
    if is_japanese(channel):
        return {
            "section_label": "今週のAIニュース",
            "week_range": f"{start.month}月{start.day}日〜{end.month}月{end.day}日",
            "cover_headline_fallback": "今週のAI、[まとめ]ました。",
            "swipe_fallback": "スワイプして続きを",
            "source_prefix": "出典",
            "outro_headline": "AIニュースを毎週整理",
            "outro_body": "企業AIの重要ニュースだけを、一次情報ベースで整理します。",
            "outro_cta": "フォローする",
        }
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    week_range = f"{months[start.month - 1]} {start.day} - {months[end.month - 1]} {end.day}, {end.year}"
    return {
        "section_label": "AI NEWS THIS WEEK",
        "week_range": week_range,
        "cover_headline_fallback": "Ang [pinakamainit] na AI news ng linggo.",
        "swipe_fallback": "Swipe for the rundown",
        "source_prefix": "Source",
        "outro_headline": "Na-save mo na ba?",
        "outro_body": "Follow for the weekly AI rundown, builder-style.",
        "outro_cta": "Follow + Save",
    }


def gemini_weekly_copy(channel: Channel, stories: list[WeeklyStory]) -> dict[str, Any] | None:
    api_key = gemini_api_key()
    if not api_key:
        return None
    voice = channel.voice_prompt or channel.default_cover_voice()
    story_payload = [
        {
            "rank": s.rank,
            "author": s.author or s.handle,
            "handle": s.handle,
            "text": s.text[:600],
            "signals": s.reasons[:4],
        }
        for s in stories
    ]
    prompt = f"""
You are the editor of a weekly AI-news Instagram carousel for {channel.brand_name},
written for a {channel.audience}. Write every word in {channel.language_name}.

Return JSON only with this exact shape:
{{
  "cover": {{
    "headline": "{channel.language_name} cover hook with exactly one [accent] word in brackets",
    "swipe_line": "short {channel.language_name} swipe prompt",
    "instagram_caption": "{channel.language_name} caption: hook, what readers learn, one CTA, clean hashtags"
  }},
  "stories": [
    {{"rank": 1, "kicker": "1-3 word ALL-CAPS label", "headline": "headline", "summary": "one sentence"}}
  ]
}}

Rules:
- Apply the {channel.brand_name} voice guide below to the cover and every story.
- cover.headline must contain exactly one [accent] word in square brackets.
- One stories entry per input story, same rank, same order. Do not invent stories.
- kicker: 1-3 word ALL-CAPS category (e.g. LAUNCH, BENCHMARK, FUNDING, RESEARCH, POLICY, OPEN SOURCE).
- headline: at most 9 words, concrete, lead with the company or product, no ending period.
- summary: one sentence, at most 24 words, factual, active voice, no hype words.
- Stay faithful to each story's text; never overstate or fabricate numbers.
- Keep load-bearing product names, model names, dates, and amounts from the source text.
- Do not change entity type: a model must not become a company, and access suspension must not become company closure.
- Any number or enumerated claim in the summary must appear in the story text.
- No markdown, no emojis, no quotation marks around values, no extra keys.

{channel.brand_name} voice guide:
{voice}

Stories JSON:
{json.dumps(story_payload, ensure_ascii=False)}
""".strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }
    response = gemini_generate_content(
        gemini_text_model(),
        api_key,
        payload,
        api_version=os.environ.get("GEMINI_TEXT_API_VERSION") or "v1beta",
        timeout=60,
    )
    parsed = parse_json_object(extract_gemini_text(response))
    return parsed if isinstance(parsed, dict) else None


def kicker_from_reasons(reasons: list[str]) -> str:
    joined = " ".join(reasons).lower()
    for needle, label in (
        ("launch", "LAUNCH"),
        ("release", "LAUNCH"),
        ("benchmark", "BENCHMARK"),
        ("open", "OPEN SOURCE"),
        ("research", "RESEARCH"),
        ("policy", "POLICY"),
        ("fund", "FUNDING"),
        ("agent", "AGENTS"),
        ("model", "NEW MODEL"),
    ):
        if needle in joined:
            return label
    return "THIS WEEK"


def local_story_copy(story: WeeklyStory) -> tuple[str, str, str]:
    """Deterministic fallback copy when Gemini is unavailable."""
    sentences = split_sentences(story.text) or [story.text]
    lead = sentences[0]
    author = story.author or story.handle.lstrip("@")
    headline = compact_headline(f"{author}: {lead}" if author else lead, limit=9)
    summary = clamp_words(lead, 24)
    return kicker_from_reasons(story.reasons), headline, summary


def curate_copy(channel: Channel, stories: list[WeeklyStory]) -> tuple[dict[str, str], str]:
    """Fill each story's channel-voice copy; return (cover_copy, backend)."""
    if all(story.copy_locked for story in stories):
        for story in stories:
            story.kicker = (story.kicker or story.category or kicker_from_reasons(story.reasons)).upper()
            story.category = story.category or story.kicker
        return {}, "curated"

    parsed = gemini_weekly_copy(channel, stories)
    backend = "local"
    cover_copy: dict[str, str] = {}
    by_rank: dict[int, dict[str, Any]] = {}
    if isinstance(parsed, dict):
        cover = parsed.get("cover")
        if isinstance(cover, dict):
            cover_copy = {
                "headline": string_value(cover.get("headline")),
                "swipe_line": string_value(cover.get("swipe_line")),
                "instagram_caption": string_value(cover.get("instagram_caption")),
            }
        for entry in parsed.get("stories", []) if isinstance(parsed.get("stories"), list) else []:
            if isinstance(entry, dict) and entry.get("rank") is not None:
                try:
                    by_rank[int(entry["rank"])] = entry
                except (TypeError, ValueError):
                    continue
        if by_rank:
            backend = "gemini"

    for story in stories:
        if story.copy_locked:
            story.kicker = (story.kicker or story.category or kicker_from_reasons(story.reasons)).upper()
            story.category = story.category or story.kicker
            continue
        entry = by_rank.get(story.rank)
        if entry:
            story.kicker = string_value(entry.get("kicker"))[:24].upper()
            story.headline = compact_headline(string_value(entry.get("headline")), limit=10)
            story.summary = clamp_words(string_value(entry.get("summary")), 28)
        if not story.headline or not story.summary:
            k, h, s = local_story_copy(story)
            story.kicker = story.kicker or k
            story.headline = story.headline or h
            story.summary = story.summary or s
        story.kicker = story.kicker or kicker_from_reasons(story.reasons)
        story.category = story.category or story.kicker
    return cover_copy, backend


# --------------------------------------------------------------------------- #
# Rendering (channel-themed)
# --------------------------------------------------------------------------- #
def channel_css(channel: Channel) -> str:
    """shared_css() plus a :root + .slide override from the channel's colors."""
    brand = channel.brand if isinstance(channel.brand, dict) else {}
    colors = brand.get("colors", {}) if isinstance(brand.get("colors"), dict) else {}
    typography = brand.get("typography", {}) if isinstance(brand.get("typography"), dict) else {}

    bg = colors.get("bg", "#F4F2EC")
    bg_top = colors.get("bg_top", "#E9E6DF")
    fg = colors.get("fg", "#16140F")
    ink_soft = colors.get("ink_soft", "rgba(20, 18, 14, 0.78)")
    primary = colors.get("primary", "#C0552E")
    muted = colors.get("muted", "rgba(20, 18, 14, 0.55)")
    rule = colors.get("rule", "rgba(20, 18, 14, 0.28)")
    heading_font = typography.get("heading_font", "Archivo")
    body_font = typography.get("body_font", "Archivo")

    return f"""{shared_css()}
:root {{
  --bg: {bg};
  --bg-top: {bg_top};
  --fg: {fg};
  --ink-soft: {ink_soft};
  --primary: {primary};
  --muted: {muted};
  --rule: {rule};
}}
body {{ font-family: {body_font}, 'Archivo', sans-serif; }}
.slide {{
  background: linear-gradient(180deg, var(--bg-top) 0%, var(--bg) 100%);
  font-family: {body_font}, 'Archivo', sans-serif;
}}
.wk-headline, .wk-section, .wk-cover-headline {{ font-family: {heading_font}, 'Archivo', sans-serif; }}
"""


def accent_markup(text: str) -> tuple[str, str]:
    """Render a `[word]` accent span; return (html_markup, plain_text)."""
    match = re.search(r"\[([^\]]+)\]", text)
    if not match:
        return html.escape(text), text
    before, word, after = text[: match.start()], match.group(1), text[match.end() :]
    markup = (
        f"{html.escape(before)}"
        f'<span class="accent">{html.escape(word)}</span>'
        f"{html.escape(after)}"
    )
    return markup, f"{before}{word}{after}"


def _write_slide(html_text: str, out_path: Path) -> Path:
    html_path = out_path.with_suffix(".html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_text)
    render_html_slide(html_path, out_path)
    return out_path


def category_icon(category: str) -> str:
    stroke = 'stroke="currentColor" stroke-width="2.2" fill="none" stroke-linecap="round" stroke-linejoin="round"'
    icons = {
        "SECURITY": f'<svg viewBox="0 0 24 24" aria-hidden="true"><path {stroke} d="M12 3l7 3v5c0 4.5-2.8 8-7 10-4.2-2-7-5.5-7-10V6l7-3z"/><path {stroke} d="M9 12l2 2 4-5"/></svg>',
        "LAUNCH": f'<svg viewBox="0 0 24 24" aria-hidden="true"><path {stroke} d="M5 19l4-1 9-9 1-4-4 1-9 9-1 4z"/><path {stroke} d="M14 6l4 4"/><path {stroke} d="M5 19l4-4"/></svg>',
        "RESEARCH": f'<svg viewBox="0 0 24 24" aria-hidden="true"><circle {stroke} cx="6" cy="7" r="2.5"/><circle {stroke} cx="18" cy="7" r="2.5"/><circle {stroke} cx="12" cy="17" r="2.5"/><path {stroke} d="M8 8.5l3 6M16 8.5l-3 6M8.5 7h7"/></svg>',
        "POLICY": f'<svg viewBox="0 0 24 24" aria-hidden="true"><path {stroke} d="M7 4h7l3 3v13H7z"/><path {stroke} d="M14 4v4h4"/><path {stroke} d="M9.5 12h5M9.5 16h4"/></svg>',
        "BUSINESS": f'<svg viewBox="0 0 24 24" aria-hidden="true"><path {stroke} d="M4 18h16"/><path {stroke} d="M6 15l4-4 3 2 5-6"/><path {stroke} d="M16 7h2v2"/></svg>',
    }
    return icons.get(category.upper(), f'<svg viewBox="0 0 24 24" aria-hidden="true"><circle {stroke} cx="12" cy="12" r="7"/><path {stroke} d="M8 12h8"/></svg>')


def editorial_mark() -> str:
    return """<div class="wk-watermark" aria-hidden="true">
      <span></span><span></span><span></span><span></span>
    </div>"""


def weekly_cover_posts(stories: list[WeeklyStory], labels: dict[str, str]) -> list[dict[str, str]]:
    """Posts fed to the regular cover-enrichment path.

    Index 0 is a synthetic digest with no handle, so source_person_from_post
    returns nothing and no single author dominates the cover -- which keeps every
    in-the-news company's CEO eligible for the mixed CEO portrait. The remaining
    posts carry the real story text so Gemini can name the (<=3) companies.
    """
    digest_text = labels["section_label"] + ": " + "; ".join(
        (story.headline or story.text[:90]) for story in stories[:6]
    )
    digest = {
        "url": "", "id": "weekly", "author": "", "handle": "",
        "text": digest_text, "date": labels["week_range"],
        "views": "", "likes": "", "reposts": "", "replies": "",
    }
    story_posts = [
        {
            "url": story.url, "id": "", "author": story.author, "handle": story.handle,
            "text": story.text, "date": story.date,
            "views": "", "likes": "", "reposts": "", "replies": "",
        }
        for story in stories
    ]
    return [digest] + story_posts


def render_cover_slide(
    channel: Channel,
    out_path: Path,
    *,
    stories: list[WeeklyStory],
    labels: dict[str, str],
    account_name: str,
    out_dir: Path,
    total: int,
) -> dict[str, Any]:
    """Render the weekly cover as an animated MP4 cover (AI art + CEO mix).

    Reuses build_x_carousel's title-cover system so the roundup cover matches the
    look of single-post carousels: an editorial AI image with up to three CEO
    portraits for the companies in this week's news, and channel-voice cover copy.
    Returns the title_context for manifest/caption use.
    """
    posts = weekly_cover_posts(stories, labels)
    title_context = build_title_enrichment(
        posts, title=None, out_dir=out_dir, source_type="x"
    )
    render_animated_title_slide(posts[0], out_path, total, None, title_context, account_name)
    return title_context


def render_news_slide(
    channel: Channel,
    story: WeeklyStory,
    out_path: Path,
    *,
    labels: dict[str, str],
    active: int,
    total: int,
) -> Path:
    rank_label = f"{story.rank:02d}"
    category = (story.category or story.kicker or labels["section_label"]).upper()
    kicker = html.escape(story.kicker or category)
    headline = html.escape(story.headline)
    summary = html.escape(story.summary)
    source_name = story.source_name or story.author or story.handle.lstrip("@")
    source_handle = story.source_handle or story.handle
    source_bits = [source_name]
    if source_handle and source_handle.lstrip("@").lower() != source_name.lower():
        source_bits.append(source_handle)
    source = html.escape(f"{labels['source_prefix']}: {' · '.join(b for b in source_bits if b)}")
    headline_size = 68 if len(story.headline) <= 28 else (60 if len(story.headline) <= 42 else 54)
    tag_class = "lead" if story.rank == 1 else "standard"
    hero_html = (
        f"""<div class="wk-hero wk-hero-metric">
    <div class="wk-metric">{html.escape(story.hero_metric)}</div>
    <div class="wk-metric-label">{html.escape(story.hero_label)}</div>
  </div>"""
        if story.hero_metric
        else f'<div class="wk-hero">{editorial_mark()}</div>'
    )

    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{channel_css(channel)}
.wk-top {{
  position: absolute; top: 82px; left: 96px; right: 96px;
  display: flex; align-items: center; justify-content: space-between;
}}
.wk-rank {{ font-size: 104px; font-weight: 880; line-height: 0.8; color: var(--primary); letter-spacing: 0; }}
.wk-kicker {{
  min-height: 58px; display: inline-flex; gap: 12px; align-items: center;
  font-size: 24px; font-weight: 820; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--ink-soft); padding: 11px 16px; border: 2px solid var(--rule);
}}
.wk-kicker svg {{ width: 28px; height: 28px; flex: 0 0 28px; }}
.wk-kicker.lead {{
  color: var(--primary);
  border-color: color-mix(in srgb, var(--primary) 70%, var(--rule));
}}
.wk-hero {{
  position: absolute; top: 198px; left: 96px; right: 96px; height: 220px;
  display: flex; align-items: center; justify-content: center; text-align: center;
}}
.wk-hero-metric {{ flex-direction: column; align-items: flex-start; text-align: left; }}
.wk-metric {{
  font-size: 128px; line-height: 0.9; font-weight: 900; letter-spacing: 0;
  color: var(--fg);
}}
.wk-metric-label {{
  margin-top: 22px; font-size: 22px; font-weight: 860; letter-spacing: 0.18em;
  color: var(--muted); text-transform: uppercase;
}}
.wk-watermark {{
  width: 390px; height: 168px; position: relative; opacity: 0.11;
}}
.wk-watermark span {{
  position: absolute; left: 0; right: 0; height: 2px; background: var(--fg);
}}
.wk-watermark span:nth-child(1) {{ top: 18px; transform: rotate(-14deg); }}
.wk-watermark span:nth-child(2) {{ top: 62px; transform: rotate(14deg); }}
.wk-watermark span:nth-child(3) {{ top: 106px; transform: rotate(-14deg); }}
.wk-watermark span:nth-child(4) {{ top: 150px; transform: rotate(14deg); }}
.wk-watermark::before, .wk-watermark::after {{
  content: ""; position: absolute; top: 0; bottom: 0; width: 2px; background: var(--fg);
}}
.wk-watermark::before {{ left: 112px; transform: rotate(14deg); }}
.wk-watermark::after {{ right: 112px; transform: rotate(-14deg); }}
.wk-body {{
  position: absolute; left: 96px; right: 96px; top: 462px; bottom: 232px;
  display: flex; flex-direction: column; justify-content: flex-start;
}}
.wk-headline {{
  font-size: {headline_size}px; line-height: 1.04; font-weight: 870;
  letter-spacing: 0; color: var(--fg); margin: 0;
}}
.wk-rule {{ width: 100%; height: 2px; margin: 32px 0 28px; background: var(--rule); }}
.wk-summary {{
  font-size: 29px; line-height: 1.38; font-weight: 610; color: var(--ink-soft);
  max-height: 82px; overflow: hidden;
}}
.wk-source {{
  position: absolute; left: 96px; right: 96px; bottom: 132px; text-align: center;
  font-size: 22px; font-weight: 800; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--primary);
}}
.dots {{ bottom: 70px; }}
</style></head>
<body>
<div class="slide">
  <div class="wk-top">
    <div class="wk-rank">{rank_label}</div>
    <div class="wk-kicker {tag_class}">{category_icon(category)}<span>{kicker}</span></div>
  </div>
  {hero_html}
  <div class="wk-body">
    <h1 class="wk-headline">{headline}</h1>
    <div class="wk-rule"></div>
    <div class="wk-summary">{summary}</div>
  </div>
  <div class="wk-source">{source}</div>
  <div class="dots">{dot_markup(active, total)}</div>
</div>
</body></html>"""
    return _write_slide(html_text, out_path)


def render_outro_slide(
    channel: Channel,
    out_path: Path,
    *,
    labels: dict[str, str],
    total: int,
) -> Path:
    handle = html.escape(channel.handle)
    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{channel_css(channel)}
.wk-outro {{
  position: absolute; left: 96px; right: 96px; top: 0; bottom: 0;
  display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center;
}}
.wk-outro .wk-headline {{ font-size: 88px; font-weight: 880; line-height: 1.02; letter-spacing: -0.02em; color: var(--fg); }}
.wk-outro .wk-body {{ margin-top: 30px; font-size: 36px; font-weight: 620; line-height: 1.3; color: var(--ink-soft); max-width: 760px; }}
.wk-cta {{
  margin-top: 56px; padding: 22px 34px; background: var(--primary); color: var(--bg);
  font-size: 32px; font-weight: 860; letter-spacing: 0.04em; text-transform: uppercase;
}}
.wk-outro-handle {{
  position: absolute; left: 96px; right: 96px; bottom: 150px; text-align: center;
  font-size: 30px; font-weight: 840; letter-spacing: 0.04em; color: var(--primary);
}}
.dots {{ bottom: 70px; }}
</style></head>
<body>
<div class="slide">
  <div class="wk-outro">
    <h1 class="wk-headline">{html.escape(labels['outro_headline'])}</h1>
    <div class="wk-body">{html.escape(labels['outro_body'])}</div>
    <div class="wk-cta">{html.escape(labels['outro_cta'])}</div>
  </div>
  <div class="wk-outro-handle">{handle}</div>
  <div class="dots">{dot_markup(total, total)}</div>
</div>
</body></html>"""
    return _write_slide(html_text, out_path)


def story_slide_records(stories: list[WeeklyStory]) -> list[SlideRecord]:
    records: list[SlideRecord] = []
    for story in stories:
        slide_index = story.rank + 1
        category = (story.category or story.kicker or "THIS WEEK").upper()
        records.append(
            SlideRecord(
                slide=slide_index,
                label=f"{story.rank:02d} {category}",
                headline=story.headline,
                body=story.summary,
                category=category,
                source_url=story.url,
                source_text=story.source_text or story.text,
                source_name=story.source_name or story.author or story.handle,
            )
        )
    return records


def run_verification(
    *,
    stories: list[WeeklyStory],
    out_dir: Path,
    channel: Channel,
    labels: dict[str, str],
    total: int,
    render_block: bool,
) -> Path:
    records = verify_records(story_slide_records(stories))
    extra_slides = [
        {
            "slide": 1,
            "label": "COVER",
            "headline": labels["cover_headline_fallback"],
            "body": labels["week_range"],
            "category": "COVER",
            "source_url": "multiple news sources",
            "claims": [],
            "verdict": "verified",
            "verified": True,
            "notes": ["Cover uses the verified news set as source context."],
        },
        {
            "slide": total,
            "label": "CTA",
            "headline": labels["outro_headline"],
            "body": labels["outro_body"],
            "category": "CTA",
            "source_url": "n/a",
            "claims": [],
            "verdict": "verified",
            "verified": True,
            "notes": ["Structural follow CTA. No factual news claim."],
        },
    ]
    manifest_path = write_run_manifest(
        out_dir / "run_manifest.json",
        records,
        meta={
            "channel_id": channel.id,
            "story_count": len(stories),
            "slide_count": total,
        },
        extra_slides=extra_slides,
    )
    if render_block:
        assert_no_blocked(records)
    return manifest_path


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build_weekly_carousel(
    *,
    out_dir: Path,
    queue_path: Path,
    input_path: Path | None,
    days: int,
    max_stories: int,
    per_source: int,
    account_name: str | None,
    verify_only: bool = False,
    reuse_cover: bool = False,
) -> Path:
    channel = load_channel()
    max_stories = max(MIN_STORIES, min(max_stories, MAX_STORIES))
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stories = gather_top_stories(
        queue_path=queue_path,
        input_path=input_path,
        days=days,
        max_stories=max_stories,
        per_source=per_source,
    )
    if not stories:
        raise SystemExit("no stories available to build a weekly carousel")

    account_label = account_name or channel.account_name
    print(f"[weekly] channel={channel.id} ({channel.language_name}) -- {len(stories)} stories")

    # Per-story channel-voice copy for the news slides.
    _, copy_backend = curate_copy(channel, stories)
    print(f"[weekly] story copy backend: {copy_backend}")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    labels = localized_labels(channel, start=start, end=end, count=len(stories))

    total = len(stories) + 2  # cover + stories + outro
    slides: list[dict[str, Any]] = []

    run_manifest_path = run_verification(
        stories=stories,
        out_dir=out_dir,
        channel=channel,
        labels=labels,
        total=total,
        render_block=not verify_only,
    )
    print(f"[weekly] verification report -> {run_manifest_path}")
    if verify_only:
        return run_manifest_path

    # Animated cover (AI editorial art + up to 3 in-the-news CEO portraits).
    cover_path = out_dir / "slide_01.mp4"
    cover_poster = cover_poster_path(cover_path)
    legacy_cover_path = out_dir / "slide_01.png"
    existing_manifest_path = out_dir / "manifest.json"
    existing_manifest = {}
    if existing_manifest_path.exists():
        try:
            existing_manifest = json.loads(existing_manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing_manifest = {}
    existing_slides = existing_manifest.get("slides") if isinstance(existing_manifest.get("slides"), list) else []
    existing_cover_slide = next(
        (
            slide
            for slide in existing_slides
            if isinstance(slide, dict)
            and int(slide.get("index") or 0) == 1
            and string_value(slide.get("path"))
        ),
        None,
    )
    legacy_manifest_cover = (
        legacy_cover_path
        if isinstance(existing_cover_slide, dict)
        and Path(string_value(existing_cover_slide.get("path"))).name == legacy_cover_path.name
        and legacy_cover_path.exists()
        else None
    )
    reusable_cover_path = cover_path if cover_path.exists() else legacy_manifest_cover
    if reuse_cover and reusable_cover_path:
        cover_path = reusable_cover_path
        cover_poster = (
            Path(string_value(existing_cover_slide.get("poster")))
            if isinstance(existing_cover_slide, dict) and string_value(existing_cover_slide.get("poster"))
            else cover_poster_path(cover_path)
        )
        title_context = {
            "cover_copy": existing_manifest.get("cover_copy")
            if isinstance(existing_manifest.get("cover_copy"), dict)
            else {
                "kicker": labels["section_label"],
                "headline": labels["cover_headline_fallback"],
                "swipe_line": labels["swipe_fallback"],
            },
            "image_provider": "reused",
            "ceos": [{"name": name} for name in existing_manifest.get("cover_ceos", [])]
            if isinstance(existing_manifest.get("cover_ceos"), list)
            else [],
            "companies": [{"name": name} for name in existing_manifest.get("cover_companies", [])]
            if isinstance(existing_manifest.get("cover_companies"), list)
            else [],
            "instagram_caption": string_value(existing_manifest.get("instagram_caption")),
        }
        print(f"[weekly] cover: reused existing {cover_path}")
    else:
        title_context = render_cover_slide(
            channel,
            cover_path,
            stories=stories,
            labels=labels,
            account_name=account_label,
            out_dir=out_dir,
            total=total,
        )
    cover_copy = title_context.get("cover_copy") if isinstance(title_context.get("cover_copy"), dict) else {}
    instagram_caption = string_value(title_context.get("instagram_caption"))
    ceos = [string_value(c.get("name")) for c in (title_context.get("ceos") or []) if isinstance(c, dict)]
    companies = [string_value(c.get("name")) for c in (title_context.get("companies") or []) if isinstance(c, dict)]
    print(f"[weekly] cover: image={title_context.get('image_provider') or 'fallback'} ceos={ceos}")
    cover_slide: dict[str, Any] = {"index": 1, "type": "cover", "path": str(cover_path)}
    if cover_poster.exists() or cover_path.suffix.lower() == ".mp4":
        cover_slide["poster"] = str(cover_poster)
    slides.append(cover_slide)

    for offset, story in enumerate(stories):
        index = offset + 2
        slide_path = out_dir / f"slide_{index:02d}.png"
        render_news_slide(channel, story, slide_path, labels=labels, active=index, total=total)
        slides.append(
            {
                "index": index,
                "type": "news",
                "path": str(slide_path),
                "rank": story.rank,
                "kicker": story.kicker,
                "headline": story.headline,
                "summary": story.summary,
                "source_url": story.url,
                "author": story.author,
                "handle": story.handle,
                "score": story.score,
                "source_type": story.source_type,
            }
        )

    outro_path = out_dir / f"slide_{total:02d}.png"
    render_outro_slide(channel, outro_path, labels=labels, total=total)
    slides.append({"index": total, "type": "outro", "path": str(outro_path)})

    manifest = {
        "source_type": "weekly_roundup",
        "channel_id": channel.id,
        "channel": {
            "id": channel.id,
            "account_name": account_name or channel.account_name,
            "brand_name": channel.brand_name,
            "handle": channel.handle,
            "language_name": channel.language_name,
            "audience": channel.audience,
            "voice_doc": channel.voice_doc_rel,
        },
        "week_start": start.date().isoformat(),
        "week_end": end.date().isoformat(),
        "week_range_label": labels["week_range"],
        "days": days,
        "story_count": len(stories),
        "slide_count": total,
        "max_total_slides": MAX_TOTAL_SLIDES,
        "copy_backend": copy_backend,
        "cover_copy": cover_copy,
        "cover_image_provider": string_value(title_context.get("image_provider")),
        "cover_ceos": ceos,
        "cover_companies": companies,
        "instagram_caption": instagram_caption,
        "slides": slides,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    for stale in sorted(out_dir.glob("slide_*.*")):
        match = re.match(r"slide_(\d+)\.", stale.name)
        if match and int(match.group(1)) > total:
            stale.unlink()
    print(f"[weekly] wrote {total} slides ({len(stories)} stories) -> {manifest_path}")
    return manifest_path


def main() -> int:
    load_env_file(ROOT / ".env")
    ap = argparse.ArgumentParser(
        description="Build a curated weekly AI-news carousel (--channel selects branding/language/voice)"
    )
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--channel",
        default=os.environ.get("CAROUSEL_CHANNEL"),
        help=(
            "Channel id selecting branding + language + voice as one bundle "
            "(see channels/<id>/channel.json). Defaults to channels.json's "
            "default_channel; also settable with CAROUSEL_CHANNEL."
        ),
    )
    ap.add_argument(
        "--max-stories",
        type=int,
        default=DEFAULT_STORIES,
        help=f"Story slides to include (clamped to {MIN_STORIES}-{MAX_STORIES}; "
        f"Instagram caps carousels at {MAX_TOTAL_SLIDES} slides, minus cover and outro)",
    )
    ap.add_argument("--days", type=int, default=7, help="How many days back counts as 'this week'")
    ap.add_argument(
        "--per-source",
        type=int,
        default=DEFAULT_PER_SOURCE,
        help="Max stories from one account, for variety (0 = no cap)",
    )
    ap.add_argument(
        "--queue",
        type=Path,
        default=DEFAULT_QUEUE,
        help="story_scout candidate queue to rank (default: out/automation/candidates.json)",
    )
    ap.add_argument(
        "--input",
        type=Path,
        help="Explicit stories JSON (list, or {\"stories\": [...]}); overrides --queue",
    )
    ap.add_argument(
        "--account-name",
        default=os.environ.get("WEEKLY_CAROUSEL_ACCOUNT_NAME"),
        help="Override the account/publisher name recorded in the manifest (default: channel account_name)",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="Run source and copy verification, write run_manifest.json, and skip rendering",
    )
    ap.add_argument(
        "--reuse-cover",
        action="store_true",
        help="Keep an existing slide_01.mp4 in the output folder instead of regenerating the cover",
    )
    args = ap.parse_args()

    # Select the active channel for every load_channel() call in this process.
    if args.channel:
        os.environ["CAROUSEL_CHANNEL"] = args.channel
    account_name = args.account_name or load_channel().account_name

    build_weekly_carousel(
        out_dir=args.out_dir,
        queue_path=args.queue,
        input_path=args.input,
        days=args.days,
        max_stories=args.max_stories,
        per_source=args.per_source,
        account_name=account_name,
        verify_only=args.verify,
        reuse_cover=args.reuse_cover,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
