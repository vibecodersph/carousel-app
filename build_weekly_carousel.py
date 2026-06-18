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

Page cap: Instagram carousels top out at 10 slides. A roundup spends one on the
cover and one on the outro, so stories are capped at 8 (default 7).
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
    count_words,
    normalize_space,
    split_sentences,
)
from build_x_carousel import (
    SLIDE_H,
    SLIDE_W,
    build_title_enrichment,
    dot_markup,
    extract_gemini_text,
    gemini_api_key,
    gemini_generate_content,
    gemini_text_model,
    load_env_file,
    parse_json_object,
    render_html_slide,
    render_title_slide,
    shared_css,
    string_value,
)
from channel import Channel, load_channel

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "out" / "weekly_carousel"
DEFAULT_QUEUE = ROOT / "out" / "automation" / "candidates.json"

# Instagram caps a carousel at 10 slides. A roundup reserves one for the cover
# and one for the outro, so at most 8 stories fit; 7 reads best by default.
MAX_TOTAL_SLIDES = 10
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
            text = clean_article_text(string_value(item.get("text") or item.get("title")))
            url = string_value(item.get("url"))
            if not text or not url:
                continue
            raw.append(
                WeeklyStory(
                    rank=0,
                    author=string_value(item.get("author")),
                    handle=string_value(item.get("handle")),
                    url=url,
                    text=text,
                    date=string_value(item.get("date")),
                    score=int(item.get("score") or 0),
                    source_type=string_value(item.get("source_type")) or "x",
                    reasons=[str(r) for r in (item.get("reasons") or []) if r],
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
            "outro_headline": "保存しましたか？",
            "outro_body": "毎週のAIニュースまとめをフォローしよう。",
            "outro_cta": "フォロー & 保存",
        }
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    week_range = f"{months[start.month - 1]} {start.day} – {months[end.month - 1]} {end.day}, {end.year}"
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
    """Render the weekly cover as a regular image cover (AI art + CEO mix).

    Reuses build_x_carousel's title-cover system so the roundup cover matches the
    look of single-post carousels: an editorial AI image with up to three CEO
    portraits for the companies in this week's news, and channel-voice cover copy.
    Returns the title_context for manifest/caption use.
    """
    posts = weekly_cover_posts(stories, labels)
    title_context = build_title_enrichment(
        posts, title=None, out_dir=out_dir, source_type="x"
    )
    render_title_slide(posts[0], out_path, total, None, title_context, account_name)
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
    kicker = html.escape(story.kicker or labels["section_label"])
    headline = html.escape(story.headline)
    summary = html.escape(story.summary)
    source_name = story.author or story.handle.lstrip("@")
    source_bits = [source_name]
    if story.handle and story.handle.lstrip("@").lower() != source_name.lower():
        source_bits.append(story.handle)
    source = html.escape(f"{labels['source_prefix']}: {' · '.join(b for b in source_bits if b)}")
    headline_size = 76 if len(story.headline) <= 40 else (66 if len(story.headline) <= 58 else 58)

    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{channel_css(channel)}
.wk-top {{
  position: absolute; top: 84px; left: 96px; right: 96px;
  display: flex; align-items: baseline; justify-content: space-between;
}}
.wk-rank {{ font-size: 120px; font-weight: 880; line-height: 0.8; color: var(--primary); letter-spacing: -0.04em; }}
.wk-kicker {{
  font-size: 27px; font-weight: 820; letter-spacing: 0.16em; text-transform: uppercase;
  color: var(--ink-soft); padding: 14px 18px; border: 2px solid var(--rule);
}}
.wk-body {{
  position: absolute; left: 96px; right: 96px; top: 300px; bottom: 230px;
  display: flex; flex-direction: column; justify-content: center;
}}
.wk-headline {{
  font-size: {headline_size}px; line-height: 1.04; font-weight: 870;
  letter-spacing: -0.02em; color: var(--fg);
}}
.wk-rule {{ width: 100%; height: 2px; margin: 38px 0 34px; background: var(--rule); }}
.wk-summary {{ font-size: 34px; line-height: 1.3; font-weight: 600; color: var(--ink-soft); }}
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
    <div class="wk-kicker">{kicker}</div>
  </div>
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

    # Regular image cover (AI editorial art + up to 3 in-the-news CEO portraits).
    cover_path = out_dir / "slide_01.png"
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
    slides.append({"index": 1, "type": "cover", "path": str(cover_path)})

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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
