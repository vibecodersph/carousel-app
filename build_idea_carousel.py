#!/usr/bin/env python3
"""Render one research carousel JSON object into carousel media.

Input defaults to the research_idea_generator ``carousel_briefs.json`` standard.
Research briefs are normalized into the render schema at the edge so the
generator can stay focused on story selection and evidence. Already-normalized
carousel-shaped JSON can still be rendered when passed explicitly.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from build_article_carousel import clamp_words, normalize_space
from build_video_slide import run
from build_x_carousel import (
    DEFAULT_ACCOUNT_NAME,
    SLIDE_H,
    SLIDE_W,
    cover_poster_path,
    dot_markup,
    extract_gemini_text,
    gemini_api_key,
    gemini_generate_content,
    gemini_text_model,
    load_env_file,
    openai_title_image_model,
    openai_title_image_size,
    parse_json_object,
    render_animated_title_slide,
    render_cta_slide,
    render_html_slide,
    shared_css,
    string_value,
)
from channel import load_channel
from generate_cover import generate_openai, openai_api_key

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "out" / "research_idea_generator" / "carousel_briefs.json"
DEFAULT_OUT = ROOT / "out" / "research_idea_generator" / "idea_carousel_render"
DEFAULT_IDEA_ITEM_IMAGE_SIZE = "2048x1152"
DEFAULT_COVER_STYLE = "default"
KINETIC_FLY_COVER_STYLE = "kinetic-fly"
DEFAULT_COVER_TEMPLATE = "auto"
KINETIC_FLY_CYCLE_SECONDS = 5.2
KINETIC_FLY_FPS = 30
RESEARCH_BRIEF_RENDER_SOURCE = "research_idea_generator"

KINETIC_COVER_TEMPLATE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "stop-signal",
        "name": "Stop Signal",
        "motion": "Abrupt diagonal cuts, snapping warning nodes, and a hard hook entrance.",
        "why_it_works": "The sudden visual break matches contrarian hooks that ask the viewer to stop or rethink.",
        "best_use": "Contrarian, risk, warning, and anti-default hooks.",
    },
    {
        "id": "pattern-break",
        "name": "Pattern Break",
        "motion": "A stable grid with one moving odd tile behind the headline.",
        "why_it_works": "The singleton motion makes list hooks feel like a scan-worthy set with one surprise.",
        "best_use": "Numbered lists, capability roundups, and multi-point research stories.",
    },
    {
        "id": "metric-snap",
        "name": "Metric Snap",
        "motion": "Bars and dots snap upward like a dashboard crossing a threshold.",
        "why_it_works": "A fast quantitative change supports cost, token, benchmark, and performance hooks.",
        "best_use": "Percentages, token counts, cost savings, benchmark, and performance claims.",
    },
    {
        "id": "split-switch",
        "name": "Split Switch",
        "motion": "Two panels trade dominance while a center divider snaps between them.",
        "why_it_works": "The before-after switch makes comparisons and old-vs-new decisions readable instantly.",
        "best_use": "Cloud vs local, default vs alternative, before-after, and ecosystem shift hooks.",
    },
    {
        "id": "loom-reveal",
        "name": "Loom Reveal",
        "motion": "Concentric rings zoom toward the viewer around the source visual.",
        "why_it_works": "The approach cue gives product, repository, and launch hooks a stronger reveal.",
        "best_use": "New tools, product reveals, repository spotlights, and implementation stories.",
    },
)

BRIEF_LABEL_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "right",
    "the",
    "to",
    "via",
    "with",
}
BRIEF_KINETIC_DROP_WORDS = BRIEF_LABEL_STOP_WORDS | {
    "developers",
    "builders",
    "building",
    "assuming",
    "choosing",
    "asking",
    "your",
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def is_render_ready_carousel(carousel: dict[str, Any]) -> bool:
    return isinstance(carousel.get("page_order"), list) and isinstance(carousel.get("cover_page"), dict)


def is_research_carousel_brief(carousel: dict[str, Any]) -> bool:
    return (
        isinstance(carousel.get("slides"), list)
        and not isinstance(carousel.get("page_order"), list)
        and not isinstance(carousel.get("cover_page"), dict)
    )


def carousel_slug(carousel: dict[str, Any], index: int) -> str:
    raw = string_value(carousel.get("id")) or f"carousel-{index + 1}"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    return slug[:72] or f"carousel-{index + 1}"


def selected_carousel(payload: dict[str, Any], index: int) -> dict[str, Any]:
    if is_render_ready_carousel(payload) or is_research_carousel_brief(payload):
        if index != 0:
            raise SystemExit("--index must be 0 when the input JSON is a single carousel object")
        return payload
    carousels = payload.get("carousels")
    if not isinstance(carousels, list) or not carousels:
        raise SystemExit("input JSON has no carousels[]")
    if index < 0 or index >= len(carousels):
        raise SystemExit(f"--index must be between 0 and {len(carousels) - 1}")
    carousel = carousels[index]
    if not isinstance(carousel, dict):
        raise SystemExit(f"carousels[{index}] is not an object")
    return carousel


def item_keys(carousel: dict[str, Any]) -> list[str]:
    order = carousel.get("page_order")
    if not isinstance(order, list):
        return sorted(
            key
            for key, value in carousel.items()
            if re.fullmatch(r"item_\d+", key) and isinstance(value, dict)
        )
    return [
        key
        for key in order
        if isinstance(key, str)
        and re.fullmatch(r"item_\d+", key)
        and isinstance(carousel.get(key), dict)
    ]


def first_source_url(page: dict[str, Any]) -> str:
    sources = page.get("sources")
    if not isinstance(sources, list):
        return ""
    for source in sources:
        if isinstance(source, dict) and string_value(source.get("url")):
            return string_value(source.get("url"))
    return ""


def brief_clean_text(value: object, *, words: int = 18) -> str:
    text = normalize_space(value).replace("...", "").replace("\u2026", "")
    return clamp_words(text, words, ellipsis=False)


def brief_slide_lines(slide: dict[str, Any]) -> list[str]:
    lines = slide.get("lines")
    if not isinstance(lines, list):
        return []
    return [brief_clean_text(line, words=18) for line in lines if brief_clean_text(line, words=18)]


def brief_source_title(url: str) -> str:
    text = normalize_space(url)
    if not text:
        return "Source"
    match = re.search(r"github\.com/([^/]+/[^/?#]+)", text)
    if match:
        return match.group(1)
    tail = text.rstrip("/").rsplit("/", 1)[-1]
    return tail or text


def brief_source_records(urls: object) -> list[dict[str, str]]:
    if not isinstance(urls, list):
        return []
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_url in urls:
        url = string_value(raw_url)
        if not url or url in seen:
            continue
        seen.add(url)
        records.append({"title": brief_source_title(url), "url": url})
    return records


def brief_slide_sources(slide: dict[str, Any]) -> list[dict[str, str]]:
    urls = slide.get("sourceUrls")
    if not isinstance(urls, list):
        urls = slide.get("source_urls")
    return brief_source_records(urls)


def brief_image_meta(slide: dict[str, Any]) -> dict[str, Any]:
    image = slide.get("image")
    return image if isinstance(image, dict) else {}


def first_string(values: object) -> str:
    if not isinstance(values, list):
        return ""
    for value in values:
        text = string_value(value)
        if text:
            return text
    return ""


def brief_image_url(image: dict[str, Any]) -> str:
    return string_value(image.get("sourceImageUrl")) or first_string(image.get("sourceImageUrls"))


def brief_image_urls(image: dict[str, Any]) -> list[str]:
    raw_values: list[object] = []
    source_url = string_value(image.get("sourceImageUrl"))
    if source_url:
        raw_values.append(source_url)
    urls = image.get("sourceImageUrls")
    if isinstance(urls, list):
        raw_values.extend(urls)
    seen: set[str] = set()
    values: list[str] = []
    for raw_url in raw_values:
        url = string_value(raw_url)
        if url and url not in seen:
            seen.add(url)
            values.append(url)
    return values


def brief_source_image_queue(cover_slide: dict[str, Any], story_slides: list[dict[str, Any]]) -> list[str]:
    queue: list[str] = []
    seen: set[str] = set()
    for slide in [cover_slide, *story_slides]:
        for url in brief_image_urls(brief_image_meta(slide)):
            if url and url not in seen:
                seen.add(url)
                queue.append(url)
    return queue


def brief_description_sections(brief: dict[str, Any]) -> dict[str, str]:
    raw = string_value(brief.get("instagramDescription"))
    paragraphs = [
        normalize_space(part)
        for part in re.split(r"\n\s*\n+", raw)
        if normalize_space(part)
    ]
    hook = string_value(brief.get("hook"))
    if paragraphs and hook and paragraphs[0].lower() == hook.lower():
        paragraphs = paragraphs[1:]

    sections = {
        "claim": "",
        "why": "",
        "evidence": "",
        "content_angle": "",
        "publish_note": "",
    }
    free_parts: list[str] = []
    for paragraph in paragraphs:
        lower = paragraph.lower()
        if lower.startswith("evidence base:"):
            sections["evidence"] = paragraph
        elif lower.startswith("content angle:"):
            sections["content_angle"] = re.sub(r"^content angle:\s*", "", paragraph, flags=re.I)
        elif lower.startswith("publish note:"):
            sections["publish_note"] = re.sub(r"^publish note:\s*", "", paragraph, flags=re.I)
        elif not paragraph.startswith("#"):
            free_parts.append(paragraph)
    if free_parts:
        sections["claim"] = free_parts[0]
    if len(free_parts) > 1:
        sections["why"] = free_parts[1]
    return sections


def has_japanese_text(value: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value))


def visible_character_count(value: str) -> int:
    return len(re.sub(r"\s+", "", normalize_space(value)))


def multiline_string_value(value: object) -> str:
    return str(value or "").strip()


def brief_localization_payload(brief: dict[str, Any]) -> dict[str, Any]:
    slides = []
    for slide in brief.get("slides", []):
        if not isinstance(slide, dict):
            continue
        slides.append(
            {
                "slideNumber": slide.get("slideNumber"),
                "type": string_value(slide.get("type")),
                "headline": string_value(slide.get("headline")),
                "lines": brief_slide_lines(slide),
                "altText": string_value(slide.get("altText")),
            }
        )
    return {
        "id": string_value(brief.get("id")),
        "workingTitle": string_value(brief.get("workingTitle")),
        "hook": string_value(brief.get("hook")),
        "instagramDescription": multiline_string_value(brief.get("instagramDescription")),
        "evidenceUrls": brief.get("evidenceUrls") if isinstance(brief.get("evidenceUrls"), list) else [],
        "slides": slides,
    }


def qa_localized_research_brief(
    brief: dict[str, Any],
    *,
    channel_language: str,
) -> dict[str, Any]:
    fields: list[tuple[str, str]] = [
        ("workingTitle", string_value(brief.get("workingTitle"))),
        ("hook", string_value(brief.get("hook"))),
    ]
    for index, slide in enumerate(brief.get("slides", []), start=1):
        if not isinstance(slide, dict):
            continue
        fields.append((f"slides[{index}].headline", string_value(slide.get("headline"))))
        for line_index, line in enumerate(brief_slide_lines(slide), start=1):
            fields.append((f"slides[{index}].lines[{line_index}]", line))

    errors: list[str] = []
    warnings: list[str] = []
    japanese = channel_language.lower().startswith("japanese")
    for name, value in fields:
        if not value:
            errors.append(f"{name} is empty")
            continue
        if "..." in value or "\u2026" in value:
            errors.append(f"{name} contains ellipsis")
        if "\u2014" in value:
            errors.append(f"{name} contains em dash")
        if japanese and ("[" in value or "]" in value):
            errors.append(f"{name} contains bracket markup")
        if japanese and name in {"hook"} | {field_name for field_name, _ in fields if ".headline" in field_name}:
            if not has_japanese_text(value):
                errors.append(f"{name} does not contain Japanese text")
        if japanese and name == "hook":
            if visible_character_count(value) > 25:
                errors.append("hook is too long for the Japanese cover template")
            if "スワイプ" in value or "次のスライド" in value:
                errors.append("hook contains swipe/navigation copy")
        if japanese and ".headline" in name and visible_character_count(value) > 28:
            errors.append(f"{name} is too long for the Japanese slide template")
        latin_words = re.findall(r"\b[A-Za-z][A-Za-z-]{3,}\b", value)
        if japanese and name != "workingTitle" and len(latin_words) >= 4:
            warnings.append(f"{name} has several Latin words: {', '.join(latin_words[:5])}")

    caption = multiline_string_value(brief.get("instagramDescription"))
    if caption:
        if "..." in caption or "\u2026" in caption:
            errors.append("instagramDescription contains ellipsis")
        if "\u2014" in caption:
            errors.append("instagramDescription contains em dash")
        if japanese and not has_japanese_text(caption):
            warnings.append("instagramDescription does not contain Japanese text")

    return {
        "passed": not errors,
        "language": channel_language,
        "checked_fields": len(fields),
        "errors": errors,
        "warnings": warnings,
    }


def apply_localized_research_copy(
    brief: dict[str, Any],
    localized: dict[str, Any],
) -> dict[str, Any]:
    updated = copy.deepcopy(brief)
    for key in ("workingTitle", "hook", "instagramDescription"):
        value = (
            multiline_string_value(localized.get(key))
            if key == "instagramDescription"
            else string_value(localized.get(key))
        )
        if value:
            updated[key] = value

    localized_slides = localized.get("slides")
    if isinstance(localized_slides, list) and isinstance(updated.get("slides"), list):
        for index, slide in enumerate(updated["slides"]):
            if not isinstance(slide, dict) or index >= len(localized_slides):
                continue
            localized_slide = localized_slides[index]
            if not isinstance(localized_slide, dict):
                continue
            headline = string_value(localized_slide.get("headline"))
            if headline:
                slide["headline"] = headline
            lines = localized_slide.get("lines")
            if isinstance(lines, list):
                slide["lines"] = [string_value(line) for line in lines if string_value(line)]
            alt_text = string_value(localized_slide.get("altText"))
            if alt_text:
                slide["altText"] = alt_text
    return updated


def localize_research_brief_copy(
    brief: dict[str, Any],
    *,
    channel: Any,
    source_payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    load_env_file(ROOT / ".env")
    load_env_file(Path.home() / ".hermes" / ".env")
    api_key = gemini_api_key()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY or GOOGLE_API_KEY is required for --localize-copy")

    voice = channel.voice_prompt or channel.default_cover_voice()
    payload = brief_localization_payload(brief)
    language = string_value(channel.language_name) or "Japanese"
    prompt = f"""
You are localizing one research-idea Instagram carousel for {channel.brand_name}.
Translate and adapt the reader-facing copy into natural {language} for {channel.audience}.

Return JSON only with this exact shape:
{{
  "workingTitle": "localized internal/editorial title",
  "hook": "localized cover hook",
  "instagramDescription": "localized Instagram caption",
  "slides": [
    {{"slideNumber": 1, "headline": "localized slide headline", "lines": [], "altText": "localized alt text"}}
  ]
}}

Rules:
- Localize naturally, not line-by-line. It should sound like a working Japanese engineer if the target language is Japanese.
- Keep model names, product names, company names, benchmark names, numbers, URLs, and source titles in their standard spelling.
- Do not invent facts, numbers, claims, source names, or conclusions.
- Slide 1 is the cover. Its headline should be the localized hook only: no swipe instruction, no subtitle, no bracket markup.
- Every slide after the cover must use only the localized JSON headline plus localized lines when lines exist.
- Keep the same slide count and order. Return one slides item for every input slide.
- Preserve empty lines arrays as empty unless the source slide has lines.
- Avoid hype phrases, markdown bullets, emoji, quotes around values, em dashes, ellipsis, and the literal string "...".
- For Japanese, use natural punctuation such as 、。：（） and concise です・ます調 where it fits.
- For Japanese, keep hook at 25 visible characters or fewer. Good example: AI開発はQAからループ設計へ
- For Japanese, keep every slide headline at 28 visible characters or fewer. Lines may carry the nuance.
- Keep captions short enough for Instagram and include source attribution when the input caption includes it.

{channel.brand_name} voice guide:
{voice}

Source payload generatedAt: {string_value((source_payload or {}).get("generatedAt"))}
Brief JSON:
{json.dumps(payload, ensure_ascii=False)}
""".strip()

    response = gemini_generate_content(
        gemini_text_model(),
        api_key,
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.25,
                "responseMimeType": "application/json",
            },
        },
        api_version=os.environ.get("GEMINI_TEXT_API_VERSION") or "v1beta",
        timeout=75,
    )
    parsed = parse_json_object(extract_gemini_text(response))
    if not isinstance(parsed, dict):
        raise SystemExit("Gemini did not return localization JSON")

    localized = apply_localized_research_copy(brief, parsed)
    qa = qa_localized_research_brief(localized, channel_language=language)
    qa["provider"] = "gemini"
    qa["model"] = gemini_text_model()
    qa["channel_id"] = channel.id
    qa["source_brief_id"] = string_value(brief.get("id"))
    if not qa["passed"]:
        raise SystemExit("localized brief failed QA: " + "; ".join(qa["errors"]))
    return localized, qa


def strip_numbered_prefix(value: str) -> str:
    return re.sub(r"^\s*\d+[.)]\s*", "", normalize_space(value))


def brief_item_name(slide: dict[str, Any], index: int) -> str:
    headline = strip_numbered_prefix(string_value(slide.get("headline")))
    lower = headline.lower()
    if "terminal" in lower and "agent" in lower:
        return "Terminal Agents"
    if "api traffic" in lower or "interceptor" in lower:
        return "API Debugging"
    if "self-hosted" in lower and "workflow" in lower:
        return "Self-Hosted Workflows"
    if "local" in lower and "agent" in lower:
        return "Local Agents"
    tokens = [
        token
        for token in re.split(r"[^A-Za-z0-9+#/-]+", headline)
        if token and token.lower() not in BRIEF_LABEL_STOP_WORDS
    ]
    if not tokens:
        tokens = [token for token in re.split(r"\s+", headline) if token]
    if not tokens:
        return f"Story {index:02d}"
    return brief_clean_text(" ".join(tokens[:3]), words=4)


def brief_copy_from_headline(headline: str) -> tuple[str, str] | None:
    lower = headline.lower()
    if "terminal" in lower and "agent" in lower:
        return (
            "Terminal agents bring local files, shells, and repo context into the workflow.",
            "Use local tools with explicit permissions.",
        )
    if "api traffic" in lower or "interceptor" in lower:
        return (
            "Local interceptors make prompt, tool, and API payload debugging visible before production.",
            "Debug the invisible agent I/O layer.",
        )
    if "self-hosted" in lower and "workflow" in lower:
        return (
            "Self-hosted frameworks turn agent demos into repeatable multi-step workflows.",
            "Make workflows repeatable before adding autonomy.",
        )
    if "local-first" in lower and "agent" in lower:
        return (
            "Local-first runtimes keep agent execution close to private data and offline environments.",
            "Keep retrieval close to the data.",
        )
    if "model routing" in lower or "routing" in lower:
        return (
            "Routing lets builders send simple tasks to cheaper models and save frontier models for hard work.",
            "Spend premium tokens only where they matter.",
        )
    if "token" in lower and "compression" in lower:
        return (
            "Compression turns provider sprawl into a cost-control problem builders can measure.",
            "Measure savings before trusting gateway claims.",
        )
    return None


def brief_lines_are_generic(lines: list[str]) -> bool:
    if not lines:
        return True
    generic_markers = (
        "search private docs",
        "keep retrieval close",
        "as a small workflow",
    )
    return all(any(marker in line.lower() for marker in generic_markers) for line in lines)


def brief_body_for_slide(slide: dict[str, Any]) -> str:
    lines = brief_slide_lines(slide)
    return brief_clean_text(" ".join(lines), words=28) if lines else ""


def brief_kinetic_tokens(value: str) -> list[str]:
    tokens = [
        token
        for token in re.findall(r"[A-Za-z0-9+#/-]+", normalize_space(value))
        if token and token.lower() not in BRIEF_KINETIC_DROP_WORDS
    ]
    return tokens[:8]


def brief_kinetic_fly_lines(value: str) -> list[list[dict[str, Any]]]:
    tokens = brief_kinetic_tokens(value)
    if not tokens:
        return []

    groups: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        projected = sum(len(part) for part in current) + len(token) + len(current)
        limit = 13 if not groups else 12
        if current and (projected > limit or len(current) >= 3):
            groups.append(current)
            current = [token]
        else:
            current.append(token)
        if len(groups) == 2 and current:
            continue
    if current:
        groups.append(current)
    if len(groups) > 3:
        groups = groups[:2] + [sum(groups[2:], [])[:3]]

    lines: list[list[dict[str, Any]]] = []
    for line_index, group in enumerate(groups[:3]):
        line: list[dict[str, Any]] = []
        for token_index, token in enumerate(group):
            accent = (line_index == 0 and token_index == 0) or line_index == len(groups[:3]) - 1
            size = kinetic_word_size(token, accent=accent)
            if len(token) > 9:
                size = min(size, 0.54)
            elif len(token) > 6:
                size = min(size, 0.62)
            line.append({
                "text": token,
                "size": size,
                "accent": accent,
            })
        if line:
            lines.append(line)
    return lines


def research_brief_to_render_carousel(
    brief: dict[str, Any],
    *,
    source_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    slides = [slide for slide in brief.get("slides", []) if isinstance(slide, dict)]
    cover_slide = next((slide for slide in slides if slide.get("type") == "cover"), slides[0] if slides else {})
    story_slides = [slide for slide in slides if slide is not cover_slide]
    if not story_slides:
        story_slides = [{
            "type": "hook_detail",
            "headline": string_value(brief.get("workingTitle") or brief.get("hook")),
            "lines": [],
            "altText": f"Story slide for {string_value(brief.get('workingTitle') or brief.get('hook'))}.",
        }]

    evidence_urls = brief.get("evidenceUrls")
    source_records = brief_source_records(evidence_urls)
    hook = (
        string_value(brief.get("hook"))
        or string_value(cover_slide.get("headline"))
        or string_value(brief.get("workingTitle"))
        or "Research story"
    )
    hook = brief_clean_text(hook, words=80)
    working_title = string_value(brief.get("workingTitle"))
    cover_image = brief_image_meta(cover_slide)
    cover_image_kind = string_value(cover_image.get("kind"))
    source_image_queue = brief_source_image_queue(cover_slide, story_slides)
    allocated_source_image_urls: set[str] = set()
    carousel: dict[str, Any] = {
        "id": f"research-{string_value(brief.get('id')) or hashlib.sha1(hook.encode('utf-8')).hexdigest()[:10]}",
        "channel_id": "",
        "render_source": RESEARCH_BRIEF_RENDER_SOURCE,
        "source_brief_id": string_value(brief.get("id")),
        "source_brief_title": working_title,
        "source_brief_score": brief.get("score"),
        "source_brief_confidence": brief.get("confidence"),
        "source_brief_hook_style": brief.get("hookStyle"),
        "source_brief_evidence_urls": [record["url"] for record in source_records],
        "generated_at": string_value((source_payload or {}).get("generatedAt")),
        "instagram_caption": multiline_string_value(brief.get("instagramDescription")),
        "suppress_cta": True,
        "cover_page": {
            "kicker": "RESEARCH LEAD",
            "headline": hook,
            "subheadline": "",
            "kinetic_subline": "",
            "kinetic_fly_lines": brief_kinetic_fly_lines(hook),
            "hook_only_cover": True,
            "image_kind": cover_image_kind,
            "source_image_url": "",
            "source_image_urls": brief_image_urls(cover_image),
            "image_prompt": string_value(cover_image.get("promptBase")),
            "image_alt_text": string_value(cover_image.get("altText")),
            "alt_text": string_value(cover_slide.get("altText")) or f"Cover slide for {hook}.",
        },
    }

    page_order = ["cover_page"]
    for item_index, slide in enumerate(story_slides, start=1):
        key = f"item_{item_index}"
        body = brief_body_for_slide(slide)
        slide_image = brief_image_meta(slide)
        slide_image_kind = string_value(slide_image.get("kind"))
        slide_source_image_urls = brief_image_urls(slide_image)
        slide_source_image_url = next(
            (url for url in slide_source_image_urls if url not in allocated_source_image_urls),
            "",
        )
        if not slide_source_image_url:
            slide_source_image_url = next(
                (url for url in source_image_queue if url not in allocated_source_image_urls),
                "",
            )
        if slide_source_image_url:
            allocated_source_image_urls.add(slide_source_image_url)
        if slide_source_image_url and slide_source_image_url not in slide_source_image_urls:
            slide_source_image_urls = [slide_source_image_url, *slide_source_image_urls]
        page_order.append(key)
        carousel[key] = {
            "item_name": "",
            "headline": brief_clean_text(slide.get("headline"), words=80) or f"Story point {item_index}",
            "body": body,
            "best_for": "",
            "watch_out": "",
            "takeaway": "",
            "proof_points": brief_slide_lines(slide),
            "sources": brief_slide_sources(slide),
            "show_source": False,
            "literal_slide": True,
            "image_kind": slide_image_kind,
            "source_image_url": slide_source_image_url,
            "source_image_urls": slide_source_image_urls,
            "image_prompt": string_value(slide_image.get("promptBase")),
            "image_alt_text": string_value(slide_image.get("altText")),
            "alt_text": string_value(slide.get("altText")) or f"Story slide {item_index} for {hook}.",
        }

    carousel["page_order"] = page_order
    carousel["page_count"] = len(page_order)
    return carousel


def normalize_carousel_for_render(
    carousel: dict[str, Any],
    *,
    source_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if is_render_ready_carousel(carousel):
        return carousel
    if is_research_carousel_brief(carousel):
        return research_brief_to_render_carousel(carousel, source_payload=source_payload)
    return carousel


def image_prompt(base: str, topic: str) -> str:
    base = normalize_space(base)
    topic = normalize_space(topic)
    if not base:
        base = f"Editorial illustration for {topic}"
    return normalize_space(
        f"{base}. 16:9 horizontal editorial artwork for an Instagram carousel. "
        "Cream paper, dark ink, terracotta accent, warm print texture. "
        "No text, no logos, no UI, no charts, no readable marks, no neon, no rainbow colors."
    )


def cover_image_prompt(base: str, topic: str) -> str:
    base = normalize_space(base)
    topic = normalize_space(topic)
    if not base:
        base = f"Editorial cover illustration for {topic}"
    return normalize_space(
        f"{base}. 16:9 horizontal landscape editorial artwork for the top of an Instagram carousel cover. "
        "Create a complete landscape illustration with clear subject detail in the upper portion; "
        "the rendered cover will place animated title text below the focal art. "
        "Keep the lower portion quiet, warm, and uncluttered so it can softly vanish into the brand paper color; no hard edge, no busy details, and no important objects there. "
        "Cream paper, dark ink, terracotta accent, warm print texture. "
        "No text, no logos, no UI, no charts, no readable marks, no neon, no rainbow colors."
    )


def openai_item_image_size() -> str:
    return os.environ.get("OPENAI_ITEM_IMAGE_SIZE") or DEFAULT_IDEA_ITEM_IMAGE_SIZE


def generated_image_path(out_dir: Path, topic: str, prompt: str) -> Path:
    digest = hashlib.sha1(f"{topic}\n{prompt}".encode("utf-8")).hexdigest()[:10]
    return out_dir / "generated_assets" / f"{digest}.png"


def source_image_asset_path(out_dir: Path, source_image_url: str) -> Path:
    parsed = urllib.parse.urlparse(source_image_url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}:
        suffix = ".img"
    digest = hashlib.sha1(source_image_url.encode("utf-8")).hexdigest()[:10]
    return out_dir / "source_assets" / f"{digest}{suffix}"


def maybe_cache_source_image(out_dir: Path, source_image_url: str) -> Path | None:
    source_image_url = string_value(source_image_url)
    if not source_image_url:
        return None
    if not re.match(r"^https?://", source_image_url, flags=re.I):
        path = Path(source_image_url)
        return path if path.exists() else None
    path = source_image_asset_path(out_dir, source_image_url)
    if path.exists() and path.stat().st_size > 0:
        print(f"[asset] using cached source image -> {path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        source_image_url,
        headers={
            "User-Agent": "Mozilla/5.0 carousel-app source-image renderer",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            path.write_bytes(response.read())
    except (OSError, urllib.error.URLError) as exc:
        print(f"[asset] source image download failed -> {source_image_url} ({exc})")
        return None
    print(f"[asset] cached source image -> {path}")
    return path


def cover_image_composition(path: Path | None) -> str:
    return "top_art" if path else ""


def load_reusable_assets(path: Path | None) -> dict[str, Any]:
    if not path:
        return {"cover": None, "cover_composition": "", "items": {}}
    manifest = read_json(path)
    assets: dict[str, Any] = {"cover": None, "cover_composition": "", "items": {}}
    slides = manifest.get("slides")
    if not isinstance(slides, list):
        return assets
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        raw_image_path = string_value(slide.get("image_path"))
        if not raw_image_path:
            continue
        image_path = Path(raw_image_path)
        if not image_path.exists():
            continue
        if slide.get("type") == "title" and assets["cover"] is None:
            assets["cover"] = image_path
            assets["cover_composition"] = (
                string_value(slide.get("image_composition")).replace("spot_illustration", "top_art")
                or cover_image_composition(image_path)
            )
        item_name = string_value(slide.get("item_name"))
        if item_name:
            assets["items"][item_name.lower()] = image_path
    return assets


def maybe_generate_image(
    out_dir: Path,
    *,
    topic: str,
    prompt: str,
    generate_images: bool,
    size: str | None = None,
) -> Path | None:
    if not generate_images:
        return None
    if not openai_api_key():
        print("[openai] OPENAI_API_KEY not set; using local visual fallback")
        return None
    path = generated_image_path(out_dir, topic, prompt)
    if path.exists():
        print(f"[openai] using cached GPT Image asset -> {path}")
        return path
    try:
        generate_openai(
            prompt,
            path,
            model=openai_title_image_model(),
            size=size or openai_title_image_size(),
        )
    except (SystemExit, Exception) as exc:
        print(f"[openai] image generation failed for {topic}; using fallback ({exc})")
        return None
    return path


def asset_uri(path: Path | None) -> str:
    if not path:
        return ""
    return path.resolve().as_uri()


def image_reference_uri(value: str) -> str:
    raw = string_value(value)
    if not raw:
        return ""
    if re.match(r"^(?:https?|data|file):", raw, flags=re.I):
        return raw
    path = Path(raw)
    if path.exists():
        return path.resolve().as_uri()
    return raw


def render_image_uri(image_path: Path | None, source_image_url: str = "") -> str:
    return asset_uri(image_path) if image_path else image_reference_uri(source_image_url)


def normalize_cover_style(value: str | None) -> str:
    style = normalize_space(value).lower().replace("_", "-")
    if not style or style in {"default", "usual", "animated", "text-motion", "text-motion-lines"}:
        return DEFAULT_COVER_STYLE
    if style in {"fly", "fly-cover", "kinetic", "kinetic-fly"}:
        return KINETIC_FLY_COVER_STYLE
    raise SystemExit(f"unknown cover style: {value}")


def cover_template_catalog() -> list[dict[str, Any]]:
    return [dict(template) for template in KINETIC_COVER_TEMPLATE_CATALOG]


def normalize_cover_template(value: str | None) -> str:
    template = normalize_space(string_value(value)).lower().replace("_", "-")
    if not template or template in {"auto", "best", "dynamic", "match"}:
        return DEFAULT_COVER_TEMPLATE
    aliases = {
        "stop": "stop-signal",
        "warning": "stop-signal",
        "abrupt-cut": "stop-signal",
        "pattern": "pattern-break",
        "list": "pattern-break",
        "oddball": "pattern-break",
        "metrics": "metric-snap",
        "metric": "metric-snap",
        "numbers": "metric-snap",
        "split": "split-switch",
        "before-after": "split-switch",
        "switch": "split-switch",
        "loom": "loom-reveal",
        "zoom": "loom-reveal",
        "reveal": "loom-reveal",
    }
    template = aliases.get(template, template)
    valid = {item["id"] for item in KINETIC_COVER_TEMPLATE_CATALOG}
    if template not in valid:
        raise SystemExit(f"unknown cover template: {value}")
    return template


def cover_template_keyword_score(text: str, keywords: tuple[str, ...]) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def select_kinetic_cover_template(
    carousel: dict[str, Any],
    requested_template: str | None = DEFAULT_COVER_TEMPLATE,
) -> str:
    requested = normalize_cover_template(requested_template)
    if requested != DEFAULT_COVER_TEMPLATE:
        return requested

    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
    explicit = normalize_cover_template(cover.get("cover_template") or cover.get("coverTemplate"))
    if explicit != DEFAULT_COVER_TEMPLATE:
        return explicit

    hook = string_value(cover.get("headline"))
    hook_style = string_value(carousel.get("source_brief_hook_style") or cover.get("hook_style") or cover.get("hookStyle"))
    title = string_value(carousel.get("source_brief_title"))
    text = normalize_space(" ".join([hook, hook_style, title])).lower()
    scores = {item["id"]: 0 for item in KINETIC_COVER_TEMPLATE_CATALOG}

    if hook_style.lower() == "list" or re.search(r"^\s*\d+[\).]?\s", hook):
        scores["pattern-break"] += 5
    scores["pattern-break"] += cover_template_keyword_score(
        text,
        ("capabilities", "ways", "tools", "patterns", "roundup", "stacking", "list"),
    )

    if re.search(r"(\d+(\.\d+)?%|\b\d+(\.\d+)?[bkmt]?\b|\$|tokens?|cost|benchmark|performance|latency|throughput)", text):
        scores["metric-snap"] += 4
    scores["metric-snap"] += cover_template_keyword_score(
        text,
        ("compression", "savings", "score", "tracking", "real-time", "scale", "inference"),
    )

    if re.search(r"\b(stop|don't|dont|avoid|risk|risky|wrong|only option|defaulting|assuming)\b", text):
        scores["stop-signal"] += 5
    scores["stop-signal"] += cover_template_keyword_score(
        text,
        ("contrarian", "warning", "trap", "too risky", "before trusting"),
    )

    if re.search(r"\b(vs|versus|instead|before|after|cloud|local|alternative|ecosystem|broader)\b", text):
        scores["split-switch"] += 3
    if re.search(r"\bfrom\b.+\bto\b", text):
        scores["split-switch"] += 3
    if "only option" in text or "default" in text:
        scores["split-switch"] += 2

    if re.search(r"\b(new|launch|reveal|repo|repository|github|via|product|framework|sdk|tool)\b", text):
        scores["loom-reveal"] += 2
    scores["loom-reveal"] += cover_template_keyword_score(
        text,
        ("spotlight", "emerging", "exploring", "converging", "turning to"),
    )

    if not any(scores.values()):
        scores["loom-reveal"] = 1

    order = [item["id"] for item in KINETIC_COVER_TEMPLATE_CATALOG]
    return max(order, key=lambda template_id: (scores[template_id], -order.index(template_id)))


def contains_japanese(value: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value or ""))


def is_katakana_char(value: str) -> bool:
    return bool(value and re.match(r"[\u30a0-\u30ffー]", value))


def is_ascii_word_char(value: str) -> bool:
    return bool(value and re.match(r"[A-Za-z0-9'._+-]", value))


def japanese_phrase_chunks(text: str, max_chars: int = 9) -> list[str]:
    """Create short Japanese chunks without splitting common particles or terms."""
    text = normalize_space(text)
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    soft_suffixes = (
        "から",
        "まで",
        "なら",
        "では",
        "には",
        "にも",
        "ので",
        "ため",
        "こと",
        "です",
        "ます",
        "として",
    )
    soft_particles = set("でにをがはへとものか")
    suffix_starts = {suffix[:2] for suffix in soft_suffixes if len(suffix) >= 2}
    for index, char in enumerate(text):
        current += char
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if char in "、。！？!?":
            chunks.append(current)
            current = ""
            continue
        if (len(current) >= 4 and current.endswith(soft_suffixes)) or (
            len(current) >= 5 and char in soft_particles and f"{char}{next_char}" not in suffix_starts
        ):
            if next_char and next_char not in "、。！？!?":
                chunks.append(current)
                current = ""
                continue
        if len(current) >= max_chars:
            if (is_katakana_char(char) and is_katakana_char(next_char)) or (
                is_ascii_word_char(char) and is_ascii_word_char(next_char)
            ) or (
                next_char in soft_particles
            ) or (
                f"{char}{next_char}" in suffix_starts
            ):
                continue
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return [chunk for chunk in (normalize_space(chunk) for chunk in chunks) if chunk]


def bracket_plain_text(value: str) -> str:
    return normalize_space((value or "").replace("[", "").replace("]", ""))


def kinetic_fly_raw_lines(cover: dict[str, Any]) -> list[list[dict[str, Any]]]:
    raw = cover.get("kinetic_fly_lines") or cover.get("fly_lines")
    if not isinstance(raw, list):
        return []
    lines: list[list[dict[str, Any]]] = []
    for raw_line in raw:
        if not isinstance(raw_line, list):
            continue
        line: list[dict[str, Any]] = []
        for item in raw_line:
            if isinstance(item, str):
                text = item
                size = 0.68
                accent = False
            elif isinstance(item, dict):
                text = string_value(item.get("text") or item.get("t"))
                size = float(item.get("size") or item.get("s") or 0.68)
                accent = bool(item.get("accent"))
            else:
                continue
            if text:
                line.append({"text": text, "size": size, "accent": accent})
        if line:
            lines.append(line)
    return lines[:3]


def kinetic_fly_tokens(headline: str, *, japanese: bool) -> list[str]:
    headline = bracket_plain_text(headline)
    if not headline:
        return []
    if japanese and " " not in headline:
        visible_chars = visible_character_count(headline)
        max_chars = 7 if visible_chars > 18 else 9
        return japanese_phrase_chunks(headline, max_chars=max_chars)
    return [token for token in headline.split() if token.strip()]


def kinetic_word_size(token: str, *, accent: bool) -> float:
    length = len(token)
    if contains_japanese(token):
        if length <= 2:
            return 0.96
        if length <= 4:
            return 0.76
        return 0.58 if accent else 0.54
    if length <= 2:
        return 0.64
    if length <= 5:
        return 0.86 if accent else 0.74
    return 0.82 if accent else 0.58


def kinetic_fly_lines(cover: dict[str, Any], channel_language: str) -> list[list[dict[str, Any]]]:
    explicit = kinetic_fly_raw_lines(cover)
    if explicit:
        return explicit

    headline = string_value(cover.get("headline"))
    japanese = channel_language.lower().startswith("japanese") or contains_japanese(headline)
    tokens = kinetic_fly_tokens(headline, japanese=japanese)
    if not tokens:
        return [[{"text": "AI", "size": 0.86, "accent": True}], [{"text": "Brief", "size": 0.88, "accent": False}]]

    if len(tokens) <= 3:
        grouped = [[token] for token in tokens]
    elif len(tokens) == 4:
        grouped = [tokens[:2], [tokens[2]], [tokens[3]]]
    else:
        grouped = [tokens[:3], tokens[3:4], tokens[4:]]

    lines: list[list[dict[str, Any]]] = []
    for line_index, group in enumerate(grouped[:3]):
        line: list[dict[str, Any]] = []
        for token_index, token in enumerate(group):
            accent = line_index == 0 and token_index == 0 or line_index == len(grouped[:3]) - 1
            line.append({"text": token, "size": kinetic_word_size(token, accent=accent), "accent": accent})
        if line:
            lines.append(line)
    return lines


def kinetic_fly_items(carousel: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in item_keys(carousel):
        page = carousel.get(key)
        if isinstance(page, dict):
            name = string_value(page.get("item_name"))
            if name:
                names.append(name)
    while len(names) < 3:
        names.append("Compare first")
    return names[:3]


def kinetic_fly_subline(cover: dict[str, Any], *, japanese: bool) -> str:
    explicit = string_value(cover.get("kinetic_subline") or cover.get("subline"))
    if explicit:
        return explicit
    subheadline = string_value(cover.get("subheadline"))
    if subheadline and contains_japanese(subheadline):
        return clamp_words(subheadline, 18, ellipsis=False)
    if japanese:
        return "いつもの選択に戻る前に、候補を一度だけ並べて見る。"
    return "Compare the route before defaulting to the obvious choice."


def kinetic_fly_handle(channel: Any) -> str:
    handle = string_value(getattr(channel, "handle", "")).lstrip("@")
    return handle.lower() if handle else string_value(getattr(channel, "account_name", "")).lower()


def kinetic_fly_headline_markup(lines: list[list[dict[str, Any]]]) -> str:
    in_vectors = [
        ("-128vw", "8vh", "-18deg", 1.45),
        ("116vw", "-14vh", "14deg", 0.48),
        ("-96vw", "-42vh", "22deg", 1.75),
        ("64vw", "36vh", "-20deg", 0.58),
        ("108vw", "18vh", "10deg", 1.58),
        ("0vw", "72vh", "-8deg", 2.05),
    ]
    out_vectors = [
        ("78vw", "-34vh", "18deg"),
        ("-92vw", "22vh", "-16deg"),
        ("76vw", "42vh", "24deg"),
        ("-62vw", "-34vh", "-22deg"),
        ("94vw", "-12vh", "12deg"),
        ("0vw", "-74vh", "-10deg"),
    ]
    rows: list[str] = []
    word_index = 0
    for line in lines:
        words: list[str] = []
        for word in line:
            in_x, in_y, in_rotate, in_scale = in_vectors[word_index % len(in_vectors)]
            out_x, out_y, out_rotate = out_vectors[word_index % len(out_vectors)]
            delay = word_index * 78
            classes = "word is-accent" if word.get("accent") else "word"
            style = (
                f"--word-size:{float(word.get('size') or 0.68):.3f};"
                f"--cycle:{KINETIC_FLY_CYCLE_SECONDS:.2f}s;"
                f"--in-x:{in_x};--in-y:{in_y};--in-rotate:{in_rotate};--in-scale:{in_scale};"
                f"--out-x:{out_x};--out-y:{out_y};--out-rotate:{out_rotate};"
            )
            words.append(
                f'<span class="{classes}" data-kinetic data-delay-ms="{delay}" '
                f'style="{style}">{html.escape(string_value(word.get("text")))}</span>'
            )
            word_index += 1
        rows.append(f'<div class="line">{"".join(words)}</div>')
    return "\n".join(rows)


def kinetic_fly_cover_css() -> str:
    return f"""
{shared_css()}
body {{ background: var(--bg); }}
.slide {{
  width: {SLIDE_W}px;
  height: {SLIDE_H}px;
  overflow: hidden;
  isolation: isolate;
  background:
    linear-gradient(180deg, var(--bg-top) 0%, var(--bg) 44%, #efe5d3 100%);
}}
.slide::before,
.slide::after {{
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
}}
.slide::before {{
  z-index: 1;
  background:
    linear-gradient(90deg, rgba(var(--primary-rgb), 0.12) 0 1px, transparent 1px 100px),
    linear-gradient(0deg, rgba(var(--fg-rgb), 0.08) 0 1px, transparent 1px 100px);
  opacity: 0.65;
  animation: gridDrift 10s linear infinite;
}}
.slide::after {{
  z-index: 30;
  border: 18px solid rgba(var(--fg-rgb), 0.08);
  background:
    linear-gradient(180deg, transparent 0%, transparent 58%, rgba(var(--bg-rgb), 0.44) 76%, rgba(var(--bg-rgb), 0.88) 100%),
    radial-gradient(circle at 18% 22%, rgba(var(--primary-rgb), 0.18), transparent 300px);
  mix-blend-mode: multiply;
}}
.source-art {{
  position: absolute;
  inset: -36px;
  z-index: 6;
  background-position: center;
  background-size: cover;
  opacity: 0.2;
  filter: grayscale(1) contrast(1.12) blur(5px);
  transform: scale(1.04);
  mix-blend-mode: multiply;
  animation: sourceArtDrift {KINETIC_FLY_CYCLE_SECONDS:.2f}s ease-in-out infinite;
}}
.source-art::after {{
  content: "";
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle at 74% 42%, rgba(var(--primary-rgb), 0.2), transparent 320px),
    linear-gradient(180deg, rgba(var(--bg-rgb), 0.7), rgba(var(--bg-rgb), 0.92));
}}
.brand-bar,
.head,
.option-row,
.subline,
.fly-footer {{
  position: absolute;
  z-index: 40;
}}
.brand-bar {{
  top: 78px;
  left: 92px;
  right: 92px;
  display: flex;
  align-items: center;
  gap: 22px;
}}
.brand-logo,
.brand-fallback {{
  width: 112px;
  height: 112px;
  display: grid;
  place-items: center;
  border: 3px solid var(--primary);
  border-radius: 50%;
  background: var(--bg);
  object-fit: cover;
  overflow: hidden;
  animation: logoSnap {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.brand-fallback {{
  color: var(--primary);
  font-size: 34px;
  font-weight: 900;
}}
.brand-name,
.brand-handle {{
  display: block;
}}
.brand-name {{
  color: var(--fg);
  font-size: 32px;
  font-weight: 900;
  line-height: 1;
  text-transform: uppercase;
}}
.brand-handle {{
  margin-top: 10px;
  color: var(--muted);
  font-size: 24px;
  font-weight: 800;
  line-height: 1;
  text-transform: none;
}}
.route-map {{
  position: absolute;
  inset: 0;
  z-index: 12;
  pointer-events: none;
}}
.route {{
  position: absolute;
  left: 90px;
  width: 1040px;
  height: 4px;
  transform-origin: 0 50%;
  background: linear-gradient(90deg, transparent, rgba(var(--primary-rgb), 0.8), rgba(var(--fg-rgb), 0.18), transparent);
  clip-path: inset(0 100% 0 0);
  animation: routeDraw {KINETIC_FLY_CYCLE_SECONDS:.2f}s linear infinite;
}}
.route-one {{ top: 404px; transform: rotate(-18deg); }}
.route-two {{ top: 626px; transform: rotate(4deg); }}
.route-three {{ top: 848px; transform: rotate(22deg); }}
.node {{
  position: absolute;
  width: 104px;
  height: 104px;
  border: 3px solid var(--fg);
  background: var(--bg);
  box-shadow: 16px 16px 0 rgba(var(--primary-rgb), 0.18);
  animation: nodePop {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.node-one {{ left: 104px; top: 492px; }}
.node-two {{ right: 156px; top: 376px; border-color: var(--primary); }}
.node-three {{ right: 110px; top: 664px; }}
.node-four {{ right: 192px; top: 920px; border-color: var(--primary); }}
.template-layer {{
  position: absolute;
  inset: 0;
  z-index: 18;
  pointer-events: none;
}}
.cover-template-stop-signal .route-map {{
  opacity: 0.92;
}}
.cut-bars span {{
  position: absolute;
  left: -120px;
  right: -120px;
  height: 14px;
  background: rgba(var(--primary-rgb), 0.66);
  transform-origin: 50% 50%;
  animation: cutBarSnap {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.cut-bars span:nth-child(1) {{ top: 336px; transform: rotate(-17deg); }}
.cut-bars span:nth-child(2) {{ top: 706px; transform: rotate(9deg); animation-delay: .12s; }}
.cut-bars span:nth-child(3) {{ top: 1010px; transform: rotate(-9deg); animation-delay: .24s; }}
.pattern-grid {{
  left: 604px;
  top: 154px;
  width: 386px;
  height: 760px;
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 18px;
  opacity: 0.74;
}}
.pattern-grid span {{
  border: 2px solid rgba(var(--fg-rgb), 0.22);
  background: rgba(var(--bg-rgb), 0.68);
  box-shadow: 9px 9px 0 rgba(var(--primary-rgb), 0.1);
  animation: quietTile {KINETIC_FLY_CYCLE_SECONDS:.2f}s ease-in-out infinite;
}}
.pattern-grid span:nth-child(13) {{
  border-color: var(--primary);
  background: rgba(var(--primary-rgb), 0.18);
  animation: oddTile {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.metric-stack {{
  left: 630px;
  top: 178px;
  width: 330px;
  height: 760px;
  display: flex;
  align-items: flex-end;
  gap: 24px;
  opacity: 0.7;
}}
.metric-stack span {{
  flex: 1;
  min-height: 128px;
  border: 3px solid rgba(var(--fg-rgb), 0.2);
  background: linear-gradient(180deg, rgba(var(--primary-rgb), 0.28), rgba(var(--fg-rgb), 0.06));
  transform-origin: 50% 100%;
  animation: metricSnap {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.metric-stack span:nth-child(1) {{ height: 34%; animation-delay: .02s; }}
.metric-stack span:nth-child(2) {{ height: 54%; animation-delay: .12s; }}
.metric-stack span:nth-child(3) {{ height: 78%; animation-delay: .22s; border-color: rgba(var(--primary-rgb), 0.6); }}
.metric-stack span:nth-child(4) {{ height: 46%; animation-delay: .32s; }}
.metric-dots span {{
  position: absolute;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: rgba(var(--primary-rgb), 0.38);
  animation: metricDot {KINETIC_FLY_CYCLE_SECONDS:.2f}s ease-in-out infinite;
}}
.metric-dots span:nth-child(1) {{ right: 92px; top: 266px; }}
.metric-dots span:nth-child(2) {{ right: 214px; top: 392px; animation-delay: .1s; }}
.metric-dots span:nth-child(3) {{ right: 148px; top: 548px; animation-delay: .2s; }}
.metric-dots span:nth-child(4) {{ right: 286px; top: 724px; animation-delay: .3s; }}
.split-panels {{
  inset: 94px 76px 106px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 26px;
  opacity: 0.62;
}}
.split-panels span {{
  display: block;
  border: 3px solid rgba(var(--fg-rgb), 0.18);
  background:
    radial-gradient(circle at 50% 30%, rgba(var(--primary-rgb), 0.18), transparent 240px),
    rgba(var(--bg-rgb), 0.58);
  transform-origin: 50% 50%;
  animation: splitPanelSwitch {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.split-panels span:nth-child(2) {{
  border-color: rgba(var(--primary-rgb), 0.42);
  animation-delay: .18s;
}}
.split-line {{
  position: absolute;
  left: 50%;
  top: 122px;
  bottom: 146px;
  width: 8px;
  background: rgba(var(--primary-rgb), 0.46);
  transform: translateX(-50%);
  animation: splitLineSnap {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.loom-rings span {{
  position: absolute;
  right: -130px;
  top: 130px;
  width: 540px;
  height: 540px;
  border: 3px solid rgba(var(--primary-rgb), 0.24);
  border-radius: 50%;
  animation: loomRing {KINETIC_FLY_CYCLE_SECONDS:.2f}s ease-in-out infinite;
}}
.loom-rings span:nth-child(2) {{
  right: -34px;
  top: 226px;
  width: 350px;
  height: 350px;
  animation-delay: .16s;
}}
.loom-rings span:nth-child(3) {{
  right: 66px;
  top: 326px;
  width: 150px;
  height: 150px;
  background: rgba(var(--primary-rgb), 0.12);
  animation-delay: .32s;
}}
.cover-template-pattern-break .route-map,
.cover-template-metric-snap .route-map,
.cover-template-split-switch .route-map,
.cover-template-loom-reveal .route-map {{
  opacity: 0.28;
}}
.cover-template-pattern-break .hook-title,
.cover-template-metric-snap .hook-title,
.cover-template-split-switch .hook-title,
.cover-template-loom-reveal .hook-title {{
  max-width: 760px;
}}
.head {{
  inset: 0;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 78px 88px 88px;
}}
.head.is-hook-only {{
  justify-content: center;
  padding: 158px 90px 110px;
}}
.hook-title {{
  max-width: 930px;
  margin: 0;
  color: var(--fg);
  font-size: var(--hook-title-size, 104px);
  font-weight: 900;
  line-height: 0.92;
  letter-spacing: 0;
  text-transform: uppercase;
  text-wrap: balance;
  text-shadow: 0 12px 0 rgba(var(--primary-rgb), 0.13);
  transform-origin: 50% 50%;
  animation: hookTitle {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.hook-title::first-letter {{
  color: var(--primary);
}}
.hook-title .hook-line {{
  display: block;
  white-space: nowrap;
}}
.hook-title .hook-first {{
  color: var(--primary);
}}
.line {{
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.16em;
  min-height: 210px;
}}
.word {{
  display: inline-block;
  color: var(--fg);
  font-size: calc(var(--word-size) * 255px);
  font-weight: 900;
  line-height: 0.88;
  text-transform: uppercase;
  transform-origin: center;
  will-change: transform, filter, opacity;
  animation: flyWord var(--cycle) 0s infinite;
  text-shadow: 0 12px 0 rgba(var(--primary-rgb), 0.12);
  white-space: nowrap;
}}
.word.is-accent {{ color: var(--primary); }}
.option-row {{
  left: 92px;
  right: 92px;
  bottom: 236px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}}
.option-row span {{
  min-height: 66px;
  display: grid;
  place-items: center;
  border: 2px solid var(--fg);
  background: rgba(var(--bg-rgb), 0.72);
  color: var(--fg);
  font-size: 24px;
  font-weight: 800;
  text-align: center;
  text-transform: uppercase;
  animation: chipLift {KINETIC_FLY_CYCLE_SECONDS:.2f}s cubic-bezier(0.16, 1, 0.3, 1) infinite;
}}
.option-row span:nth-child(2) {{ border-color: var(--primary); color: var(--primary); }}
.subline {{
  left: 92px;
  right: 176px;
  bottom: 148px;
  margin: 0;
  color: var(--ink-soft);
  font-size: 32px;
  font-weight: 800;
  line-height: 1.12;
}}
.fly-footer {{
  left: 92px;
  right: 92px;
  bottom: 78px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--muted);
  font-size: 23px;
  font-weight: 900;
  text-transform: uppercase;
}}
.progress {{
  display: flex;
  align-items: center;
  gap: 12px;
}}
.progress i {{
  display: block;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--rule);
}}
.progress i:first-child {{
  width: 78px;
  border-radius: 999px;
  background: var(--primary);
}}
@keyframes flyWord {{
  0% {{
    transform: translate(var(--in-x), var(--in-y)) rotate(var(--in-rotate)) scale(var(--in-scale));
    filter: blur(16px);
    opacity: 0;
    animation-timing-function: cubic-bezier(0.16, 1.38, 0.3, 1);
  }}
  16% {{
    transform: translate(0, 0) rotate(0deg) scale(1);
    filter: blur(0);
    opacity: 1;
    animation-timing-function: linear;
  }}
  64% {{
    transform: translate(0, 0) rotate(0deg) scale(1);
    filter: blur(0);
    opacity: 1;
    animation-timing-function: cubic-bezier(0.7, 0, 0.9, 0.25);
  }}
  80% {{
    transform: translate(var(--out-x), var(--out-y)) rotate(var(--out-rotate)) scale(1.46);
    filter: blur(18px);
    opacity: 0;
  }}
  100% {{ opacity: 0; }}
}}
@keyframes hookTitle {{
  0% {{
    transform: translateY(42px) scale(0.96);
    filter: blur(0);
    opacity: 0;
  }}
  18%, 68% {{
    transform: translateY(0) scale(1);
    filter: blur(0);
    opacity: 1;
  }}
  82%, 100% {{
    transform: translateY(-24px) scale(1.04);
    filter: blur(0);
    opacity: 0;
  }}
}}
@keyframes routeDraw {{
  0%, 18% {{ opacity: 0; clip-path: inset(0 100% 0 0); }}
  30%, 58% {{ opacity: 1; clip-path: inset(0 0 0 0); }}
  78%, 100% {{ opacity: 0; clip-path: inset(0 0 0 100%); }}
}}
@keyframes nodePop {{
  0%, 10%, 88%, 100% {{ transform: translateY(20px) rotate(8deg) scale(0.78); opacity: 0; }}
  24%, 66% {{ transform: translateY(0) rotate(0deg) scale(1); opacity: 0.72; }}
}}
@keyframes chipLift {{
  0%, 14%, 84%, 100% {{ transform: translateY(28px); opacity: 0; }}
  28%, 68% {{ transform: translateY(0); opacity: 1; }}
}}
@keyframes logoSnap {{
  0%, 100% {{ transform: translateY(0) rotate(0deg) scale(1); }}
  18% {{ transform: translateY(-6px) rotate(2deg) scale(1.06); }}
  28%, 70% {{ transform: translateY(0) rotate(0deg) scale(1); }}
}}
@keyframes gridDrift {{
  0% {{ transform: translate(0, 0); }}
  100% {{ transform: translate(100px, 100px); }}
}}
@keyframes sourceArtDrift {{
  0%, 100% {{ transform: translate3d(-10px, 0, 0) scale(1.04); }}
  50% {{ transform: translate3d(14px, -10px, 0) scale(1.08); }}
}}
@keyframes cutBarSnap {{
  0%, 12%, 88%, 100% {{ opacity: 0; clip-path: inset(0 100% 0 0); }}
  22%, 64% {{ opacity: 1; clip-path: inset(0 0 0 0); }}
  76% {{ opacity: 0; clip-path: inset(0 0 0 100%); }}
}}
@keyframes quietTile {{
  0%, 100% {{ transform: translateY(0); opacity: 0.58; }}
  50% {{ transform: translateY(-6px); opacity: 0.74; }}
}}
@keyframes oddTile {{
  0%, 12%, 88%, 100% {{ transform: translate(24px, 24px) scale(0.82); opacity: 0; }}
  24%, 68% {{ transform: translate(0, 0) scale(1.18); opacity: 1; }}
}}
@keyframes metricSnap {{
  0%, 12%, 90%, 100% {{ transform: scaleY(0.2); opacity: 0.08; }}
  28%, 66% {{ transform: scaleY(1); opacity: 1; }}
}}
@keyframes metricDot {{
  0%, 100% {{ transform: scale(0.6); opacity: 0.1; }}
  34%, 66% {{ transform: scale(1.4); opacity: 0.82; }}
}}
@keyframes splitPanelSwitch {{
  0%, 100% {{ transform: scale(0.96); opacity: 0.28; }}
  30%, 62% {{ transform: scale(1.04); opacity: 0.76; }}
}}
@keyframes splitLineSnap {{
  0%, 18%, 84%, 100% {{ transform: translateX(-50%) scaleY(0.1); opacity: 0; }}
  30%, 66% {{ transform: translateX(-50%) scaleY(1); opacity: 1; }}
}}
@keyframes loomRing {{
  0%, 100% {{ transform: scale(0.78); opacity: 0; }}
  24%, 62% {{ transform: scale(1.06); opacity: 0.86; }}
  78% {{ transform: scale(1.28); opacity: 0; }}
}}
"""


def kinetic_fly_progress_script() -> str:
    return f"""
<script>
(() => {{
  const cycleMs = {KINETIC_FLY_CYCLE_SECONDS * 1000:.0f};
  window.__setKineticFlyProgress = (progress) => {{
    const clamped = Math.max(0, Math.min(1, Number(progress) || 0));
    const currentMs = clamped * cycleMs;
    document.querySelectorAll("[data-kinetic]").forEach((element) => {{
      const delayMs = Number(element.dataset.delayMs || 0);
      element.style.animationDelay = `${{(delayMs - currentMs) / 1000}}s`;
      element.style.animationPlayState = "paused";
    }});
  }};
  window.__kineticFlyCoverReady = true;
}})();
</script>
"""


def kinetic_cover_template_markup(template_id: str) -> str:
    if template_id == "stop-signal":
        return '<div class="template-layer cut-bars" aria-hidden="true"><span></span><span></span><span></span></div>'
    if template_id == "pattern-break":
        cells = "".join("<span></span>" for _ in range(25))
        return f'<div class="template-layer pattern-grid" aria-hidden="true">{cells}</div>'
    if template_id == "metric-snap":
        bars = "".join("<span></span>" for _ in range(4))
        dots = "".join("<span></span>" for _ in range(4))
        return (
            f'<div class="template-layer metric-stack" aria-hidden="true">{bars}</div>'
            f'<div class="template-layer metric-dots" aria-hidden="true">{dots}</div>'
        )
    if template_id == "split-switch":
        return (
            '<div class="template-layer split-panels" aria-hidden="true"><span></span><span></span></div>'
            '<div class="template-layer split-line" aria-hidden="true"></div>'
        )
    if template_id == "loom-reveal":
        return '<div class="template-layer loom-rings" aria-hidden="true"><span></span><span></span><span></span></div>'
    return ""


def kinetic_hook_title_size(headline: str) -> int:
    words = [word for word in normalize_space(headline).split() if word]
    length = len(normalize_space(headline))
    if length > 92 or len(words) > 12:
        return 74
    if length > 74 or len(words) > 10:
        return 82
    if length > 58 or len(words) > 8:
        return 92
    return 104


def kinetic_hook_title_markup(headline: str, *, japanese: bool) -> str:
    headline = string_value(headline)
    if not headline:
        return ""
    if not japanese and not contains_japanese(headline):
        return html.escape(headline)
    tokens = kinetic_fly_tokens(headline, japanese=True)
    if not tokens:
        return html.escape(headline)
    rows: list[str] = []
    for index, token in enumerate(tokens):
        escaped = html.escape(token)
        if index == 0 and escaped:
            escaped = f'<span class="hook-first">{escaped[0]}</span>{escaped[1:]}'
        rows.append(f'<span class="hook-line">{escaped}</span>')
    return "".join(rows)


def kinetic_fly_cover_html(
    carousel: dict[str, Any],
    *,
    count: int,
    channel: Any,
    cover_template: str | None = DEFAULT_COVER_TEMPLATE,
) -> str:
    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
    template_id = select_kinetic_cover_template(carousel, cover_template)
    japanese = channel.language_name.lower().startswith("japanese")
    lines = kinetic_fly_lines(cover, channel.language_name)
    headline_text = " ".join(string_value(word.get("text")) for line in lines for word in line)
    items = kinetic_fly_items(carousel)
    hook_only_cover = bool(cover.get("hook_only_cover"))
    if hook_only_cover:
        brand_markup = ""
    else:
        logo_src = asset_uri(getattr(channel, "logo_path", None))
        if logo_src:
            logo_markup = f'<img class="brand-logo" src="{html.escape(logo_src, quote=True)}" alt="{html.escape(channel.brand_name)}" data-kinetic data-delay-ms="0">'
        else:
            fallback = html.escape((string_value(channel.account_name) or "AI")[:2].upper())
            logo_markup = f'<span class="brand-fallback" data-kinetic data-delay-ms="0">{fallback}</span>'
        brand_markup = f"""
  <header class="brand-bar">
    {logo_markup}
    <div>
      <span class="brand-name">{html.escape(string_value(channel.account_name or channel.brand_name))}</span>
      <span class="brand-handle">{html.escape(kinetic_fly_handle(channel))}</span>
    </div>
  </header>"""
    swipe = "スワイプして比較" if japanese else "Swipe for the comparison"
    if hook_only_cover:
        headline_text = string_value(cover.get("headline")) or headline_text
        hook_title_size = kinetic_hook_title_size(headline_text)
        hook_title_markup = kinetic_hook_title_markup(headline_text, japanese=japanese)
        head_markup = (
            '<section class="head is-hook-only" aria-label="'
            f'{html.escape(headline_text, quote=True)}">'
            f'<h1 class="hook-title" data-kinetic data-delay-ms="80" style="--hook-title-size:{hook_title_size}px">'
            f'{hook_title_markup}</h1>'
            '</section>'
        )
        secondary_markup = ""
    else:
        head_markup = (
            f'<section class="head" aria-label="{html.escape(headline_text, quote=True)}">\n'
            f'{kinetic_fly_headline_markup(lines)}\n'
            '</section>'
        )
        secondary_markup = f"""
  <div class="option-row">
    <span data-kinetic data-delay-ms="0">{html.escape(items[0])}</span>
    <span data-kinetic data-delay-ms="80">{html.escape(items[1])}</span>
    <span data-kinetic data-delay-ms="160">{html.escape(items[2])}</span>
  </div>
  <p class="subline">{html.escape(kinetic_fly_subline(cover, japanese=japanese))}</p>
  <footer class="fly-footer">
    <span>{html.escape(swipe)}</span>
    <span class="progress" aria-hidden="true"><i></i><i></i><i></i></span>
  </footer>"""
    source_image = render_image_uri(None, string_value(cover.get("source_image_url")))
    source_art_markup = (
        f'<div class="source-art" style="background-image:url({html.escape(source_image, quote=True)})"></div>'
        if source_image
        else ""
    )
    template_markup = kinetic_cover_template_markup(template_id)
    return f"""<!doctype html>
<html lang="{'ja' if japanese else 'en'}"><head><meta charset="utf-8"><style>
{kinetic_fly_cover_css()}
</style></head>
<body>
<div class="slide cover-template-{html.escape(template_id)}" data-cover-style="kinetic-fly" data-cover-template="{html.escape(template_id)}" aria-label="{html.escape(headline_text, quote=True)}">
  {source_art_markup}
  {template_markup}
  {brand_markup}
  <div class="route-map" aria-hidden="true">
    <span class="route route-one" data-kinetic data-delay-ms="0"></span>
    <span class="route route-two" data-kinetic data-delay-ms="180"></span>
    <span class="route route-three" data-kinetic data-delay-ms="360"></span>
    <span class="node node-one" data-kinetic data-delay-ms="0"></span>
    <span class="node node-two" data-kinetic data-delay-ms="120"></span>
    <span class="node node-three" data-kinetic data-delay-ms="240"></span>
    <span class="node node-four" data-kinetic data-delay-ms="360"></span>
  </div>
  {head_markup}
  {secondary_markup}
</div>
{kinetic_fly_progress_script()}
</body></html>"""


def render_kinetic_fly_cover(
    carousel: dict[str, Any],
    out_path: Path,
    *,
    count: int,
    channel: Any,
    cover_template: str | None = DEFAULT_COVER_TEMPLATE,
    duration_seconds: float = KINETIC_FLY_CYCLE_SECONDS,
    fps: int = KINETIC_FLY_FPS,
) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit("playwright is required to render kinetic fly covers") from exc
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required to render kinetic fly cover MP4s")

    duration_seconds = max(0.1, float(duration_seconds))
    fps = max(1, int(fps))
    frame_count = max(2, int(round(duration_seconds * fps)))
    poster_index = min(frame_count - 1, max(0, int(round(frame_count * 0.36))))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    poster_path = cover_poster_path(out_path)
    html_path = out_path.with_suffix(".html")
    html_path.write_text(
        kinetic_fly_cover_html(carousel, count=count, channel=channel, cover_template=cover_template),
        encoding="utf-8",
    )

    print(f"[cover] rendering kinetic fly cover -> {out_path}")
    try:
        with tempfile.TemporaryDirectory(prefix=f"{out_path.stem}_fly_frames_") as tmp:
            frames_dir = Path(tmp)
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": SLIDE_W, "height": SLIDE_H}, device_scale_factor=1)
                page.goto(html_path.resolve().as_uri())
                page.wait_for_load_state("networkidle")
                page.evaluate(
                    "() => (document.fonts && document.fonts.ready ? "
                    "document.fonts.ready.then(() => true) : true)"
                )
                page.wait_for_function("() => window.__kineticFlyCoverReady === true")
                slide = page.locator(".slide")
                for frame_index in range(frame_count):
                    progress = frame_index / (frame_count - 1)
                    frame_path = frames_dir / f"frame_{frame_index:04d}.png"
                    page.evaluate("(progress) => window.__setKineticFlyProgress(progress)", progress)
                    slide.screenshot(path=str(frame_path))
                browser.close()

            shutil.copyfile(frames_dir / f"frame_{poster_index:04d}.png", poster_path)
            run(
                [
                    ffmpeg,
                    "-y",
                    "-framerate",
                    str(fps),
                    "-start_number",
                    "0",
                    "-i",
                    str(frames_dir / "frame_%04d.png"),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(out_path),
                ]
            )
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(
            "could not render the kinetic fly cover. If this is a fresh setup, run "
            "`uv run python -m playwright install chromium` once."
        ) from exc
    return out_path


def bracket_markup(text: str) -> str:
    escaped = html.escape(text or "")
    return escaped.replace("[", '<span class="accent">').replace("]", "</span>")


def item_title_markup(text: str) -> str:
    text = string_value(text)
    if contains_japanese(text):
        visible_chars = visible_character_count(text)
        max_chars = 10 if visible_chars <= 24 else 9
        chunks = japanese_phrase_chunks(bracket_plain_text(text), max_chars=max_chars)
        if chunks:
            return "".join(f'<span class="jp-phrase">{html.escape(chunk)}</span>' for chunk in chunks)
    return bracket_markup(text)


def concise_body(page: dict[str, Any]) -> str:
    body = normalize_space(page.get("body"))
    if body:
        first_sentence = re.split(r"(?<=[.!?。])\s+", body, maxsplit=1)[0]
        return clamp_words(first_sentence or body, 18, ellipsis=False)
    best_for = normalize_space(page.get("best_for"))
    watch_out = normalize_space(page.get("watch_out"))
    return clamp_words(" ".join(part for part in [best_for, watch_out] if part), 18, ellipsis=False)


def concise_takeaway(page: dict[str, Any]) -> str:
    best_for = normalize_space(page.get("best_for"))
    if best_for:
        return clamp_words(best_for, 14, ellipsis=False)
    takeaway = normalize_space(page.get("takeaway"))
    if takeaway:
        return clamp_words(takeaway, 12, ellipsis=False)
    watch_out = normalize_space(page.get("watch_out"))
    return clamp_words(watch_out, 12, ellipsis=False)


def title_context(
    carousel: dict[str, Any],
    out_dir: Path,
    *,
    generate_images: bool,
    reusable_image: Path | None = None,
    reusable_image_composition: str = "",
) -> tuple[dict[str, Any], dict[str, str], Path | None]:
    channel = load_channel()
    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
    headline = string_value(cover.get("headline"))
    prompt = cover_image_prompt(string_value(cover.get("image_prompt")), headline)
    if reusable_image:
        cover_image = reusable_image
        print(f"[asset] reusing title image -> {cover_image}")
    else:
        cover_image = maybe_generate_image(
            out_dir,
            topic=headline or string_value(carousel.get("id")) or "idea carousel cover",
            prompt=prompt,
            generate_images=generate_images,
            size=openai_title_image_size(),
        )
    cover_copy = {
        "kicker": string_value(cover.get("kicker")) or "CURATION",
        "headline": headline,
        "accent_words": [],
        "swipe_line": "スワイプで続きへ" if channel.language_name.lower().startswith("japanese") else "Swipe for the stack",
    }
    context = {
        "topic": headline,
        "topic_image_path": cover_image,
        "image_provider": "openai" if cover_image else "",
        "image_composition": string_value(reusable_image_composition) or cover_image_composition(cover_image),
        "cover_copy": cover_copy,
        "cover_animation": "text-motion-lines",
        "companies": [],
        "ceos": [],
        "source_people": [],
        "topic_entity": None,
        "post_explanations": [],
        "instagram_caption": multiline_string_value(carousel.get("instagram_caption")),
        "brand_voice_doc": channel.voice_doc_rel,
        "google_enabled": False,
        "provider": string_value(carousel.get("render_source")) or RESEARCH_BRIEF_RENDER_SOURCE,
        "openai_image_model": openai_title_image_model() if openai_api_key() else "",
        "openai_image_size": openai_title_image_size() if openai_api_key() else "",
        "generated_image_prompt": prompt,
    }
    return context, cover_copy, cover_image


def item_slide_css() -> str:
    return f"""
{shared_css()}
body {{ background: #777; }}
.slide {{ width: {SLIDE_W}px; height: {SLIDE_H}px; }}
.handle {{
  position: absolute;
  top: 58px;
  left: 64px;
  font-size: 24px;
  font-weight: 820;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: rgba(20, 18, 14, .72);
  z-index: 5;
}}
.count {{
  position: absolute;
  top: 58px;
  right: 64px;
  font-size: 23px;
  font-weight: 820;
  color: rgba(20, 18, 14, .43);
  z-index: 5;
}}
.item-visual {{
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 565px;
  overflow: hidden;
  background:
    linear-gradient(135deg, rgba(var(--primary-rgb), .72), rgba(22, 20, 15, .94)),
    repeating-linear-gradient(90deg, rgba(var(--bg-rgb), .12) 0 2px, transparent 2px 18px);
  -webkit-mask-image: linear-gradient(180deg, #000 0%, #000 76%, rgba(0, 0, 0, .55) 90%, transparent 100%);
  mask-image: linear-gradient(180deg, #000 0%, #000 76%, rgba(0, 0, 0, .55) 90%, transparent 100%);
}}
.item-visual.has-image {{
  background-position: center;
  background-size: cover;
}}
.item-visual::after {{
  content: '';
  position: absolute;
  inset: 0;
  background:
    linear-gradient(180deg, rgba(var(--bg-rgb), 0) 0%, rgba(var(--bg-rgb), .1) 66%, rgba(var(--bg-rgb), .45) 100%);
}}
.item-cluster {{
  position: absolute;
  left: 56px;
  right: 56px;
  top: 610px;
  bottom: 124px;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
}}
.item-rule {{
  display: flex;
  align-items: center;
  gap: 22px;
  margin-bottom: 28px;
  color: var(--primary);
}}
.item-rule::before,
.item-rule::after {{
  content: '';
  flex: 1;
  height: 2px;
  background: var(--rule);
}}
.item-rule span {{
  font-size: 23px;
  font-weight: 780;
  letter-spacing: .08em;
  line-height: 1;
  text-transform: uppercase;
  color: var(--primary);
}}
.item-title {{
  font-size: 74px;
  line-height: .98;
  letter-spacing: 0;
  font-weight: 900;
  color: var(--fg);
  text-wrap: balance;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.item-title .accent {{
  color: var(--primary);
}}
.item-title.has-japanese {{
  word-break: keep-all;
  overflow-wrap: normal;
}}
.item-title.has-japanese .jp-phrase {{
  display: inline-block;
  white-space: nowrap;
}}
.item-body {{
  margin-top: 34px;
  max-width: 880px;
  font-size: 34px;
  line-height: 1.18;
  font-weight: 650;
  color: var(--ink-soft);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.takeaway {{
  margin-top: 40px;
  max-width: 880px;
  padding: 24px 30px;
  border-left: 8px solid var(--primary);
  background: rgba(255, 255, 255, .36);
  font-size: 31px;
  line-height: 1.12;
  font-weight: 850;
  color: var(--fg);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.source {{
  position: absolute;
  left: 58px;
  right: 58px;
  bottom: 92px;
  color: rgba(20, 18, 14, .48);
  font-size: 21px;
  font-weight: 650;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.dots {{ bottom: 48px; }}
.slide.is-literal .item-cluster {{
  top: 430px;
  bottom: 170px;
  justify-content: center;
}}
.slide.is-literal .item-cluster::before {{
  content: '';
  width: 104px;
  height: 8px;
  margin-bottom: 28px;
  background: var(--primary);
}}
.slide.is-literal .item-visual.has-source-image {{
  left: 40px;
  right: 40px;
  top: 34px;
  height: 650px;
  opacity: 0.96;
  filter: saturate(.94) contrast(1.06);
  transform: none;
  background-position: top center;
  background-repeat: no-repeat;
  background-size: contain;
  background-color: rgba(255, 255, 255, .78);
  border: 1px solid rgba(20, 18, 14, .08);
  box-shadow: 0 18px 42px rgba(20, 18, 14, .08);
  -webkit-mask-image: none;
  mask-image: none;
}}
.slide.is-literal .item-visual.has-source-image::after {{
  background:
    linear-gradient(180deg, rgba(var(--bg-rgb), 0) 0%, rgba(var(--bg-rgb), .03) 78%, rgba(var(--bg-rgb), .42) 100%);
}}
.slide.is-literal .item-visual.has-source-image + .item-cluster {{
  top: 740px;
  bottom: 132px;
  justify-content: flex-start;
}}
.slide.is-literal .item-visual.has-source-image + .item-cluster .item-title {{
  max-width: 940px;
  font-size: 61px;
  line-height: .98;
}}
.slide.is-literal .item-visual.has-generated-image {{
  height: 100%;
  opacity: 0.82;
  filter: saturate(.96) contrast(1.12);
  transform: scale(1.02);
  background-position: center right;
  -webkit-mask-image: none;
  mask-image: none;
}}
.slide.is-literal .item-visual.has-generated-image::after {{
  background:
    linear-gradient(90deg, rgba(var(--bg-rgb), .98) 0%, rgba(var(--bg-rgb), .93) 38%, rgba(var(--bg-rgb), .48) 68%, rgba(var(--bg-rgb), .16) 100%),
    linear-gradient(180deg, rgba(var(--bg-rgb), .06) 0%, rgba(var(--bg-rgb), .32) 100%);
}}
.slide.is-literal .item-visual.has-generated-image + .item-cluster {{
  top: 560px;
  right: 380px;
  bottom: 116px;
  justify-content: center;
}}
.slide.is-literal .item-visual.has-generated-image + .item-cluster .item-title {{
  max-width: 650px;
  font-size: 58px;
  line-height: .98;
}}
.slide.is-literal .item-title {{
  display: block;
  overflow: visible;
  font-size: 56px;
  line-height: 1;
  -webkit-line-clamp: unset;
  -webkit-box-orient: initial;
}}
.slide.is-literal .item-body {{
  max-width: 920px;
  margin-top: 30px;
  font-size: 34px;
  line-height: 1.16;
  display: block;
  overflow: visible;
  -webkit-line-clamp: unset;
  -webkit-box-orient: initial;
}}
"""


def render_item_slide(
    page: dict[str, Any],
    out_path: Path,
    *,
    active: int,
    count: int,
    image_path: Path | None,
) -> None:
    channel = load_channel()
    source_image_url = string_value(page.get("source_image_url"))
    visual_uri = render_image_uri(image_path, source_image_url)
    visual_style = (
        f' style="background-image: url({html.escape(visual_uri, quote=True)})"'
        if visual_uri
        else ""
    )
    visual_class = "item-visual"
    if visual_uri:
        image_source_class = "has-source-image" if source_image_url else "has-generated-image"
        visual_class = f"item-visual has-image {image_source_class}"
    item_name = string_value(page.get("item_name"))
    item_rule_markup = f'<div class="item-rule"><span>{html.escape(item_name)}</span></div>' if item_name else ""
    body_text = concise_body(page)
    body_markup = f'<p class="item-body">{html.escape(body_text)}</p>' if body_text else ""
    takeaway_text = concise_takeaway(page)
    takeaway_markup = f'<div class="takeaway">{html.escape(takeaway_text)}</div>' if takeaway_text else ""
    source_url = first_source_url(page)
    show_source = page.get("show_source", True) is not False
    source_markup = f'<div class="source">Source: {html.escape(source_url)}</div>' if source_url and show_source else ""
    literal_slide = bool(page.get("literal_slide"))
    slide_class = "slide is-literal" if literal_slide else "slide"
    show_chrome = page.get("show_chrome", True) is not False and not literal_slide
    handle_markup = (
        f'<div class="handle">{html.escape(channel.handle.strip() or f"@{channel.account_name}")}</div>'
        if show_chrome
        else ""
    )
    count_markup = f'<div class="count">{active:02d} / {count:02d}</div>' if show_chrome else ""
    dots_markup = f'<div class="dots">{dot_markup(active, count)}</div>' if show_chrome else ""
    headline = string_value(page.get("headline"))
    title_class = "item-title has-japanese" if contains_japanese(headline) else "item-title"
    html_path = out_path.with_suffix(".html")
    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{item_slide_css()}
</style></head>
<body>
<div class="{slide_class}">
  <div class="{visual_class}"{visual_style}></div>
  {handle_markup}
  {count_markup}
  <div class="item-cluster">
    {item_rule_markup}
    <h1 class="{title_class}">{item_title_markup(headline)}</h1>
    {body_markup}
    {takeaway_markup}
  </div>
  {source_markup}
  {dots_markup}
</div>
</body></html>"""
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_text, encoding="utf-8")
    render_html_slide(html_path, out_path)


def render_carousel(
    carousel: dict[str, Any],
    *,
    out_dir: Path,
    generate_images: bool = True,
    channel_id: str | None = None,
    reusable_assets: dict[str, Any] | None = None,
    cover_style: str = DEFAULT_COVER_STYLE,
    cover_template: str | None = DEFAULT_COVER_TEMPLATE,
) -> Path:
    carousel = normalize_carousel_for_render(carousel)
    channel = load_channel(channel_id or string_value(carousel.get("channel_id")) or None)
    os.environ["CAROUSEL_CHANNEL"] = channel.id
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = item_keys(carousel)
    suppress_cta = bool(carousel.get("suppress_cta"))
    total = len(keys) + (1 if suppress_cta else 2)
    reusable_assets = reusable_assets or {"cover": None, "items": {}}
    cover_style = normalize_cover_style(cover_style)
    cover = carousel.get("cover_page")
    cover = cover if isinstance(cover, dict) else {}
    slides: list[dict[str, Any]] = []
    cover_path = out_dir / "slide_01.mp4"
    cover_poster = cover_poster_path(cover_path)
    cover_template_id = ""
    if cover_style == KINETIC_FLY_COVER_STYLE:
        cover_image = None
        image_composition = ""
        cover_template_id = select_kinetic_cover_template(carousel, cover_template)
        render_kinetic_fly_cover(
            carousel,
            cover_path,
            count=total,
            channel=channel,
            cover_template=cover_template_id,
        )
    else:
        context, _cover_copy, cover_image = title_context(
            carousel,
            out_dir,
            generate_images=generate_images,
            reusable_image=reusable_assets.get("cover"),
            reusable_image_composition=string_value(reusable_assets.get("cover_composition")),
        )
        image_composition = string_value(context.get("image_composition"))
        post = {
            "id": string_value(carousel.get("id")),
            "url": f"https://research-idea-generator.local/carousels/{string_value(carousel.get('id'))}",
            "author": channel.brand_name,
            "handle": channel.handle,
            "text": " ".join(
                part
                for part in [string_value(cover.get("headline")), string_value(cover.get("subheadline"))]
                if part
            ),
            "date": string_value(carousel.get("generated_at")),
        }
        render_animated_title_slide(
            post,
            cover_path,
            total,
            None,
            context,
            channel.account_name or DEFAULT_ACCOUNT_NAME,
        )
    slides.append(
        {
            "index": 1,
            "type": "title",
            "path": str(cover_path.resolve()),
            "poster": str(cover_poster.resolve()),
            "image_path": str(cover_image or ""),
            "source_image_url": string_value(cover.get("source_image_url")),
            "source_image_urls": cover.get("source_image_urls") if isinstance(cover.get("source_image_urls"), list) else [],
            "image_composition": image_composition,
            "cover_style": cover_style,
            "cover_template": cover_template_id,
            "alt_text": string_value(cover.get("alt_text")),
        }
    )

    for offset, key in enumerate(keys, start=2):
        page = carousel[key]
        prompt = image_prompt(
            string_value(page.get("image_prompt")),
            string_value(page.get("item_name")),
        )
        reusable_image = reusable_assets.get("items", {}).get(string_value(page.get("item_name")).lower())
        if reusable_image:
            image_path = reusable_image
            print(f"[asset] reusing {page.get('item_name')} image -> {image_path}")
        else:
            source_image_url = string_value(page.get("source_image_url"))
            if source_image_url:
                image_path = maybe_cache_source_image(out_dir, source_image_url)
                print(f"[asset] using source image for {key} -> {source_image_url}")
            else:
                image_path = maybe_generate_image(
                    out_dir,
                    topic=string_value(page.get("item_name")) or key,
                    prompt=prompt,
                    generate_images=generate_images,
                    size=openai_item_image_size(),
                )
        slide_path = out_dir / f"slide_{offset:02d}.png"
        render_item_slide(
            page,
            slide_path,
            active=offset,
            count=total,
            image_path=image_path,
        )
        slides.append(
            {
                "index": offset,
                "type": "item",
                "path": str(slide_path.resolve()),
                "item_name": string_value(page.get("item_name")),
                "image_path": str(image_path or ""),
                "source_image_url": string_value(page.get("source_image_url")),
                "source_image_urls": page.get("source_image_urls") if isinstance(page.get("source_image_urls"), list) else [],
                "source_url": first_source_url(page),
                "alt_text": string_value(page.get("alt_text")),
            }
        )

    if not suppress_cta:
        cta = carousel.get("cta")
        cta = cta if isinstance(cta, dict) else {}
        cta_path = out_dir / f"slide_{total:02d}.png"
        render_cta_slide(
            cta_path,
            total,
            total,
            {
                "kicker": "FOLLOW",
                "headline": string_value(cta.get("headline")) or "Follow for more",
                "body": string_value(cta.get("body")),
                "action": string_value(cta.get("action")) or "Follow + Save",
            },
        )
        slides.append({
            "index": total,
            "type": "cta",
            "path": str(cta_path.resolve()),
            "alt_text": string_value(cta.get("alt_text")),
        })
    manifest = {
        "source": string_value(carousel.get("render_source")) or RESEARCH_BRIEF_RENDER_SOURCE,
        "carousel_id": string_value(carousel.get("id")),
        "channel_id": channel.id,
        "slide_count": total,
        "cover_style": cover_style,
        "cover_template": cover_template_id,
        "cover_image_provider": "reused" if cover_image and reusable_assets.get("cover") else "openai" if cover_image else "",
        "instagram_caption": multiline_string_value(carousel.get("instagram_caption")),
        "suppress_cta": suppress_cta,
        "slides": slides,
    }
    for key in (
        "source_brief_id",
        "source_brief_title",
        "source_brief_score",
        "source_brief_confidence",
        "source_brief_hook_style",
        "source_brief_evidence_urls",
        "localized_channel_id",
        "localized_language",
    ):
        if string_value(carousel.get(key)):
            manifest[key] = carousel.get(key)
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Render one research idea brief or carousel JSON object")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--index", type=int, default=0, help="0-based carousel index")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--channel", help="Render with a different channel id")
    parser.add_argument("--asset-manifest", type=Path, help="Reuse generated images from another render manifest")
    parser.add_argument("--no-generate-images", action="store_true")
    parser.add_argument(
        "--localize-copy",
        action="store_true",
        help="Localize reader-facing research brief copy to the selected channel language before rendering",
    )
    parser.add_argument(
        "--cover-style",
        default=os.environ.get("IDEA_COVER_STYLE", DEFAULT_COVER_STYLE),
        help="Cover renderer: default/usual or kinetic-fly/fly",
    )
    parser.add_argument(
        "--cover-template",
        default=os.environ.get("IDEA_COVER_TEMPLATE", DEFAULT_COVER_TEMPLATE),
        help="Kinetic cover template: auto, stop-signal, pattern-break, metric-snap, split-switch, or loom-reveal",
    )
    args = parser.parse_args()

    payload = read_json(args.input)
    channel = load_channel(args.channel)
    selected = copy.deepcopy(selected_carousel(payload, args.index))
    localization_qa: dict[str, Any] | None = None
    if args.localize_copy:
        selected, localization_qa = localize_research_brief_copy(
            selected,
            channel=channel,
            source_payload=payload,
        )
    carousel = normalize_carousel_for_render(
        selected,
        source_payload=payload,
    )
    if args.localize_copy:
        carousel["localized_channel_id"] = channel.id
        carousel["localized_language"] = channel.language_name
    out_dir = args.out_dir
    if args.out_dir == DEFAULT_OUT:
        out_dir = DEFAULT_OUT / carousel_slug(carousel, args.index)
    if args.localize_copy:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "localized_carousel_brief.json").write_text(
            json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (out_dir / "localization_qa.json").write_text(
            json.dumps(localization_qa or {}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    manifest_path = render_carousel(
        carousel,
        out_dir=out_dir,
        generate_images=not args.no_generate_images,
        channel_id=args.channel,
        reusable_assets=load_reusable_assets(args.asset_manifest),
        cover_style=args.cover_style,
        cover_template=args.cover_template,
    )
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
