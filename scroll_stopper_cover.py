#!/usr/bin/env python3
"""Generate scroll-stopper first-slide cover variants for carousel posts.

This module is intentionally deterministic by default. It can be called from a
future API route, from tests, or from the CLI in this repo:

    uv run python scroll_stopper_cover.py "How to make carousels get more saves"

The returned variants are HTML/CSS compositions with editable text. Optional AI
image assets are planned with safe prompts, and only generated when the CLI is
asked to do so.
"""
from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Literal

from build_video_slide import OUT, SLIDE_H, SLIDE_W
from channel import load_channel
from generate_cover import DEFAULT_OPENAI_IMAGE_MODEL, generate_openai, openai_api_key

CoverTemplateId = Literal[
    "face_reaction_object",
    "mistake_warning",
    "before_after_split",
    "pattern_break_grid",
    "proof_receipt",
    "oversized_type",
    "surreal_scale",
    "kinetic_reveal",
]

TEMPLATE_IDS: tuple[CoverTemplateId, ...] = (
    "face_reaction_object",
    "mistake_warning",
    "before_after_split",
    "pattern_break_grid",
    "proof_receipt",
    "oversized_type",
    "surreal_scale",
    "kinetic_reveal",
)

DEFAULT_FORMAT = {"width": SLIDE_W, "height": SLIDE_H, "platform": "instagram"}
DEFAULT_CONSTRAINTS = {
    "avoidClickbait": True,
    "maxMainHeadlineChars": 42,
    "maxSubheadlineChars": 80,
    "numberOfVariants": 4,
}
DEFAULT_CREATIVE_DIRECTION = {
    "tone": "bold",
    "hookType": "auto",
    "visualStyle": "auto",
    "motionIntensity": "none",
    "allowHumanFace": True,
    "allowGeneratedImages": False,
    "allowSurrealImages": False,
}
WEIGHTS = {
    "focalClarity": 15,
    "mobileReadability": 15,
    "valueContrast": 12,
    "curiosityGap": 12,
    "humanEmotion": 10,
    "anomaly": 10,
    "relevance": 10,
    "motionUsefulness": 8,
    "brandFit": 4,
    "accessibility": 4,
}
MOTION_ACTIONS = {"pop", "slam", "zoom_in", "reveal", "wipe", "shake_once", "parallax", "circle_draw", "arrow_enter"}
DECORATIVE_MOTION_ACTIONS = {"float", "pulse"}
VISUAL_CATEGORY_BY_HOOK = {
    "mistake": "mistake",
    "warning": "warning",
    "secret": "secret",
    "contradiction": "secret",
    "transformation": "transformation",
    "proof": "proof",
    "comparison": "comparison",
    "identity": "identity",
    "story": "story",
}
MOTION_PATTERN_BY_TEMPLATE = {
    "face_reaction_object": {
        "pattern": "face/subject approaches + gaze/highlight cue",
        "why": "Approach and social attention cues create urgency, then the highlight directs the scan path.",
        "bestUse": "Commentary, confession hooks, thumbnails, and carousel covers with emotional stakes.",
    },
    "mistake_warning": {
        "pattern": "abrupt cut + object snap",
        "why": "A sudden warning mark and broken object create an orienting event and predictive violation.",
        "bestUse": "Mistakes, tests, tool warnings, and dramatic educational hooks.",
    },
    "before_after_split": {
        "pattern": "fast before/after switch",
        "why": "The viewer sees the transformation before needing to read the details.",
        "bestUse": "Design, learning, finance, editing, cleaning, and optimization posts.",
    },
    "pattern_break_grid": {
        "pattern": "one element moves while all else is static + pattern break",
        "why": "Oddball detection pulls attention to the single moving anomaly.",
        "bestUse": "Grid posts, comparisons, lists, audits, and animated carousel covers.",
    },
    "proof_receipt": {
        "pattern": "partial reveal + highlight stroke",
        "why": "A receipt slide-in creates proof, while the highlight stroke turns the hidden detail into a curiosity gap.",
        "bestUse": "Receipts, tests, case studies, benchmarks, and proof posts.",
    },
    "oversized_type": {
        "pattern": "abrupt cut + looped micro-motion",
        "why": "Huge type lands as the signal, then a subtle underline/echo prevents attention decay.",
        "bestUse": "Opinion hooks, identity hooks, warnings, and text-led posters.",
    },
    "surreal_scale": {
        "pattern": "zoom toward viewer + scale violation",
        "why": "A looming impossible object creates urgency and surprise.",
        "bestUse": "Abstract concepts, hidden costs, surreal metaphors, and launch hooks.",
    },
    "kinetic_reveal": {
        "pattern": "partial reveal + abrupt moving slabs",
        "why": "Hard wipes reveal the path while the first frame stays readable.",
        "bestUse": "Motion-first covers, launches, and result/action starts.",
    },
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "before",
    "for",
    "from",
    "get",
    "gets",
    "getting",
    "how",
    "in",
    "into",
    "is",
    "it",
    "make",
    "more",
    "of",
    "on",
    "or",
    "post",
    "the",
    "this",
    "to",
    "with",
    "you",
    "your",
}


def string_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", string_value(value)).strip()


def nested_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [normalize_space(item) for item in value if normalize_space(item)]


def sanitize_font_family(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9 ,._-]+", "", normalize_space(value))
    value = re.sub(r"\s*,\s*", ", ", value)
    return value.strip(" ,") or "Archivo"


def clamp_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(parsed, maximum))


def slugify(value: str, fallback: str = "cover") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:72] or fallback


def kebab_template(template_id: str) -> str:
    return template_id.replace("_", "-")


def clip_words(value: str, max_words: int, max_chars: int | None = None) -> str:
    words = [word for word in normalize_space(value).split() if word]
    clipped = " ".join(words[:max_words])
    if max_chars and len(clipped) > max_chars:
        kept: list[str] = []
        for word in words:
            candidate = " ".join([*kept, word])
            if len(candidate) > max_chars:
                break
            kept.append(word)
        clipped = " ".join(kept) or value[:max_chars].rstrip()
    return clipped


def headline_word_count(value: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+|[^\sA-Za-z0-9]", value))


def compact_topic(topic: str, max_words: int = 4) -> str:
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+.#/-]*", topic)
    important = [token for token in tokens if token.lower() not in STOPWORDS]
    selected = important[:max_words] or tokens[:max_words]
    return " ".join(selected) or "This Idea"


def is_hex_color(value: str) -> bool:
    return bool(re.fullmatch(r"#?[0-9a-fA-F]{6}", string_value(value)))


def normalize_hex(value: str, fallback: str) -> str:
    raw = string_value(value)
    if not is_hex_color(raw):
        raw = fallback
    return f"#{raw.lstrip('#').upper()}"


def rgb_from_hex(value: str) -> tuple[float, float, float]:
    value = normalize_hex(value, "#000000").lstrip("#")
    return (
        int(value[0:2], 16) / 255,
        int(value[2:4], 16) / 255,
        int(value[4:6], 16) / 255,
    )


def relative_luminance(value: str) -> float:
    channels = []
    for channel in rgb_from_hex(value):
        if channel <= 0.03928:
            channels.append(channel / 12.92)
        else:
            channels.append(((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast_ratio(foreground: str, background: str) -> float:
    l1 = relative_luminance(foreground)
    l2 = relative_luminance(background)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def css_var(css: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", css)
    return normalize_space(match.group(1)) if match else ""


def channel_brand_tokens(channel_id: str | None) -> dict[str, Any]:
    channel = load_channel(channel_id)
    brand = channel.brand if isinstance(channel.brand, dict) else {}
    colors = nested_dict(brand.get("colors"))
    typography = nested_dict(brand.get("typography"))
    return {
        "name": channel.brand_name,
        "voice": channel.default_cover_voice(),
        "colors": {
            "bg": normalize_hex(colors.get("bg", "#F4F2EC"), "#F4F2EC"),
            "bgTop": normalize_hex(colors.get("bg_top", "#E9E6DF"), "#E9E6DF"),
            "fg": normalize_hex(colors.get("fg", "#16140F"), "#16140F"),
            "accent": normalize_hex(colors.get("primary", "#C0552E"), "#C0552E"),
            "danger": "#FF3B30",
            "paper": "#FFF8EC",
            "dark": "#101014",
        },
        "fonts": [
            sanitize_font_family(typography.get("heading_font") or "Archivo"),
            sanitize_font_family(typography.get("body_font") or "Archivo"),
        ],
        "channelId": channel.id,
        "accountName": channel.account_name,
        "audience": channel.audience,
    }


def normalize_brand(raw_brand: dict[str, Any], channel_tokens: dict[str, Any]) -> dict[str, Any]:
    raw_colors = raw_brand.get("colors")
    colors = dict(channel_tokens["colors"])
    if isinstance(raw_colors, dict):
        if raw_colors.get("bg"):
            colors["bg"] = normalize_hex(raw_colors["bg"], colors["bg"])
        if raw_colors.get("fg"):
            colors["fg"] = normalize_hex(raw_colors["fg"], colors["fg"])
        if raw_colors.get("accent") or raw_colors.get("primary"):
            colors["accent"] = normalize_hex(raw_colors.get("accent") or raw_colors.get("primary"), colors["accent"])
    elif isinstance(raw_colors, list):
        safe_colors = [normalize_hex(color, "") for color in raw_colors if is_hex_color(string_value(color))]
        if safe_colors:
            colors["accent"] = safe_colors[0]
        if len(safe_colors) > 1:
            colors["bg"] = safe_colors[1]
    return {
        "name": normalize_space(raw_brand.get("name")) or channel_tokens["name"],
        "colors": colors,
        "fonts": [sanitize_font_family(font) for font in list_of_strings(raw_brand.get("fonts"))] or channel_tokens["fonts"],
        "voice": normalize_space(raw_brand.get("voice")) or channel_tokens["voice"],
        "logoUrl": normalize_space(raw_brand.get("logoUrl")),
        "forbiddenColors": list_of_strings(raw_brand.get("forbiddenColors")),
        "accountName": channel_tokens["accountName"],
    }


def normalize_request(request: dict[str, Any], *, channel_id: str | None = None) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("request must be a dict")
    topic = normalize_space(request.get("topic"))
    if not topic:
        raise ValueError("topic is required")
    channel_tokens = channel_brand_tokens(channel_id)
    creative = {**DEFAULT_CREATIVE_DIRECTION, **nested_dict(request.get("creativeDirection"))}
    motion_intensity = normalize_space(creative.get("motionIntensity")).lower() or "none"
    if motion_intensity not in {"none", "subtle", "kinetic"}:
        motion_intensity = "none"
    hook_type = normalize_space(creative.get("hookType")).lower() or "auto"
    if hook_type not in {
        "mistake",
        "warning",
        "secret",
        "contradiction",
        "transformation",
        "proof",
        "comparison",
        "identity",
        "story",
        "auto",
    }:
        hook_type = "auto"
    visual_style = normalize_space(creative.get("visualStyle")).lower() or "auto"
    tone = normalize_space(creative.get("tone")).lower() or "bold"
    constraints = {**DEFAULT_CONSTRAINTS, **nested_dict(request.get("constraints"))}
    format_spec = {**DEFAULT_FORMAT, **nested_dict(request.get("format"))}
    width = clamp_int(format_spec.get("width"), SLIDE_W, 320, 4096)
    height = clamp_int(format_spec.get("height"), SLIDE_H, 320, 4096)
    normalized = {
        "topic": topic,
        "audience": normalize_space(request.get("audience")) or channel_tokens["audience"],
        "carouselPromise": normalize_space(request.get("carouselPromise")),
        "slideOutline": list_of_strings(request.get("slideOutline")),
        "brand": normalize_brand(nested_dict(request.get("brand")), channel_tokens),
        "format": {
            "width": width,
            "height": height,
            "platform": normalize_space(format_spec.get("platform")) or "instagram",
        },
        "creativeDirection": {
            "tone": tone,
            "hookType": hook_type,
            "visualStyle": visual_style,
            "motionIntensity": motion_intensity,
            "allowHumanFace": creative.get("allowHumanFace") is not False,
            "allowGeneratedImages": bool(creative.get("allowGeneratedImages")),
            "allowSurrealImages": bool(creative.get("allowSurrealImages")),
        },
        "assets": {
            "existingImageUrls": list_of_strings(nested_dict(request.get("assets")).get("existingImageUrls")),
            "productImageUrls": list_of_strings(nested_dict(request.get("assets")).get("productImageUrls")),
            "userProvidedFaceUrl": normalize_space(nested_dict(request.get("assets")).get("userProvidedFaceUrl")),
        },
        "constraints": {
            "avoidClickbait": constraints.get("avoidClickbait") is not False,
            "maxMainHeadlineChars": clamp_int(constraints.get("maxMainHeadlineChars"), 42, 18, 72),
            "maxSubheadlineChars": clamp_int(constraints.get("maxSubheadlineChars"), 80, 24, 140),
            "numberOfVariants": clamp_int(constraints.get("numberOfVariants"), 4, 3, 6),
        },
    }
    return normalized


def infer_hook_type(request: dict[str, Any]) -> str:
    configured = request["creativeDirection"]["hookType"]
    if configured != "auto":
        return configured
    text = " ".join(
        [
            request["topic"],
            request.get("carouselPromise", ""),
            " ".join(request.get("slideOutline", [])),
        ]
    ).lower()
    if any(word in text for word in ["mistake", "wrong", "fail", "avoid", "trap"]):
        return "mistake"
    if any(word in text for word in ["proof", "tested", "case study", "receipt", "data"]):
        return "proof"
    if any(word in text for word in ["before", "after", "change", "fix", "transform", "redesign"]):
        return "transformation"
    if any(word in text for word in ["vs", "versus", "compare", "which", "wins"]):
        return "comparison"
    if any(word in text for word in ["secret", "hidden", "nobody"]):
        return "secret"
    return "warning"


def fit_headline(value: str, max_chars: int) -> str:
    value = normalize_space(value)
    if len(value) <= max_chars and 2 <= headline_word_count(value) <= 7:
        return value
    return clip_words(value, 7, max_chars)


def fit_subhook(value: str, max_chars: int) -> str:
    return clip_words(value, 12, max_chars)


def generate_hook_candidates(request: dict[str, Any]) -> list[str]:
    topic = request["topic"]
    topic_short = compact_topic(topic, 3)
    topic_one = compact_topic(topic, 1)
    promise = request.get("carouselPromise") or topic
    max_chars = request["constraints"]["maxMainHeadlineChars"]
    text = f"{topic} {promise}".lower()
    hooks: list[str] = []
    if "save" in text and "carousel" in text:
        hooks.extend(
            [
                "Stop Killing Your Saves",
                "Your First Slide Fails",
                "The Save Trap",
                "Before You Post",
                "One Slide Is Leaking",
                "This Hook Gets Swipes",
            ]
        )
    if "slide" in text or "carousel" in text:
        hooks.extend(
            [
                "Your Cover Is Losing",
                "Fix The First Slide",
                "The Swipe Starts Here",
            ]
        )
    if "langgraph" in text and any(word in text for word in ["token", "budget", "wallet", "deploy", "deal-breaker", "deal breaker"]):
        hooks.extend(
            [
                "Before You Deploy LangGraph",
                "The LangGraph Cost Trap",
                "Stop Token Bleeding",
                "LangGraph Can Drain Budget",
                "3 LangGraph Deal-Breakers",
                "Your Agent Is Leaking",
            ]
        )
    if "api" in text and any(word in text for word in ["budget", "gcash", "peso", "pesos", "bill", "credits", "paying", "saved"]):
        hooks.extend(
            [
                "Stop Burning API Credits",
                "Your API Bill Is Leaking",
                "3 APIs Worth Paying For",
                "The GCash API Test",
                "Before You Pick An API",
                "The Cheap API Trap",
            ]
        )
    hooks.extend(
        [
            f"Stop Missing {topic_one}",
            f"The {topic_one} Trap",
            f"Before You {topic_one}",
            f"I Was Wrong About {topic_short}",
            f"Nobody Tells You {topic_one}",
            f"This Tiny Change Works",
            f"Why {topic_short} Fails",
            f"The Hidden {topic_one} Cost",
        ]
    )
    seen: set[str] = set()
    fitted: list[str] = []
    for hook in hooks:
        hook = fit_headline(hook, max_chars)
        key = hook.lower()
        if key in seen or headline_word_count(hook) < 2:
            continue
        seen.add(key)
        fitted.append(hook)
    return fitted[:10]


def subhook_from_request(request: dict[str, Any], fallback: str) -> str:
    promise = normalize_space(request.get("carouselPromise"))
    if promise:
        return fit_subhook(promise, request["constraints"]["maxSubheadlineChars"])
    outline = request.get("slideOutline") or []
    if outline:
        return fit_subhook(outline[0], request["constraints"]["maxSubheadlineChars"])
    return fit_subhook(fallback, request["constraints"]["maxSubheadlineChars"])


def motion_plan(intensity: str, kind: str) -> dict[str, Any] | None:
    if intensity == "none":
        return None
    if kind == "kinetic":
        return {
            "intensity": intensity,
            "firstFrameHook": "Headline and focal shape are visible at frame zero.",
            "timeline": [
                {"target": "headline", "action": "slam", "startMs": 0, "durationMs": 520, "easing": "cubic-bezier(.12,.9,.24,1)"},
                {"target": "hero", "action": "reveal", "startMs": 120, "durationMs": 680, "easing": "cubic-bezier(.18,.8,.18,1)"},
                {"target": "highlight", "action": "circle_draw", "startMs": 620, "durationMs": 700},
            ],
            "loop": [{"target": "highlight", "action": "pulse", "startMs": 1600, "durationMs": 2200}],
        }
    if kind == "warning":
        return {
            "intensity": intensity,
            "firstFrameHook": "Warning word is readable immediately.",
            "timeline": [
                {"target": "headline", "action": "slam", "startMs": 0, "durationMs": 520},
                {"target": "object", "action": "shake_once", "startMs": 450, "durationMs": 360},
            ],
            "loop": [],
        }
    return {
        "intensity": intensity,
        "firstFrameHook": "The final still frame remains readable.",
        "timeline": [
            {"target": "headline", "action": "reveal", "startMs": 0, "durationMs": 620},
            {"target": "accent", "action": "arrow_enter", "startMs": 420, "durationMs": 520},
        ],
        "loop": [],
    }


def make_strategy(
    *,
    main_hook: str,
    sub_hook: str,
    request: dict[str, Any],
    visual_category: str,
    signal_type: str,
    signal_description: str,
    stakes_type: str,
    stakes_description: str,
    curiosity_gap: str,
    focal_description: str,
    focal_position: str,
    eye_path: list[str],
    pattern_interrupt: str = "",
    human_cue: dict[str, Any] | None = None,
    motion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "mainHook": fit_headline(main_hook, request["constraints"]["maxMainHeadlineChars"]),
        "subHook": fit_subhook(sub_hook, request["constraints"]["maxSubheadlineChars"]),
        "contentPromise": request.get("carouselPromise") or f"A useful payoff about {request['topic']}",
        "audienceRelevance": request.get("audience") or "Creators who care about the topic.",
        "visualCategory": visual_category,
        "signal": {"type": signal_type, "description": signal_description},
        "stakes": {"type": stakes_type, "description": stakes_description},
        "curiosityGap": curiosity_gap,
        "focalPoint": {"description": focal_description, "preferredPosition": focal_position},
        "eyePath": eye_path,
        "patternInterrupt": pattern_interrupt,
        "humanCue": human_cue or {"use": False},
        "motionPlan": motion,
    }


def generate_attention_strategies(request: dict[str, Any]) -> list[dict[str, Any]]:
    hook_type = infer_hook_type(request)
    category = VISUAL_CATEGORY_BY_HOOK.get(hook_type, "warning")
    hooks = generate_hook_candidates(request)
    subhook = subhook_from_request(request, "The missing piece is on slide 2")
    motion_intensity = request["creativeDirection"]["motionIntensity"]
    topic_short = compact_topic(request["topic"], 3)
    topic_one = compact_topic(request["topic"], 1)
    audience = request["audience"]
    strategies: list[dict[str, Any]] = []

    if request["creativeDirection"]["allowHumanFace"]:
        strategies.append(
            make_strategy(
                main_hook=hooks[0],
                sub_hook=subhook,
                request=request,
                visual_category=category if category != "proof" else "mistake",
                signal_type="face",
                signal_description=f"One expressive person reacts to the unresolved problem in {topic_short}.",
                stakes_type="loss" if category in {"mistake", "warning"} else "curiosity",
                stakes_description=f"{audience} may be missing a fix that changes the outcome.",
                curiosity_gap=f"What exactly is going wrong with {topic_short}?",
                focal_description="Large emotional face aimed toward the headline and highlighted object.",
                focal_position="rule_of_thirds_right",
                eye_path=["face", "headline", "highlighted object", "subhook"],
                pattern_interrupt="A single marked object breaks the otherwise clean cover.",
                human_cue={"use": True, "cueType": "face", "emotion": "concern", "gazeTarget": "headline"},
                motion=motion_plan(motion_intensity, "warning"),
            )
        )

    strategies.extend(
        [
            make_strategy(
                main_hook=hooks[1 if len(hooks) > 1 else 0],
                sub_hook=subhook,
                request=request,
                visual_category="mistake" if category == "warning" else category,
                signal_type="huge_text",
                signal_description="Huge high-contrast type acts as the first visual hit.",
                stakes_type="loss",
                stakes_description=f"The viewer may be losing time, money, status, or saves because of {topic_short}.",
                curiosity_gap=f"Which part of {topic_short} is failing?",
                focal_description="Oversized headline dominates the upper half with one tight accent.",
                focal_position="center",
                eye_path=["headline", "accent sticker", "subhook"],
                pattern_interrupt="One rough accent slashes through the calm layout.",
                motion=motion_plan(motion_intensity, "warning"),
            ),
            make_strategy(
                main_hook=hooks[2 if len(hooks) > 2 else 0],
                sub_hook=subhook,
                request=request,
                visual_category=category if category in {"warning", "mistake"} else "secret",
                signal_type="pattern_break",
                signal_description="A repeated grid creates instant pattern recognition, then one tile breaks it.",
                stakes_type="curiosity",
                stakes_description=f"{audience} can spot the one decision that changes the outcome.",
                curiosity_gap="Which tile is the hidden problem?",
                focal_description="One enlarged broken tile interrupts a clean repeated grid.",
                focal_position="rule_of_thirds_left",
                eye_path=["grid pattern", "broken tile", "headline", "subhook"],
                pattern_interrupt="One card is tilted, marked, and higher contrast than every other card.",
                motion=motion_plan(motion_intensity, "kinetic"),
            ),
            make_strategy(
                main_hook=hooks[3 if len(hooks) > 3 else 0],
                sub_hook="The clue is visible before the explanation",
                request=request,
                visual_category="proof",
                signal_type="proof_screenshot",
                signal_description="A stylized receipt/proof card gives the hook a concrete object.",
                stakes_type="surprise",
                stakes_description=f"The viewer gets evidence-shaped tension without fake metrics or endorsements.",
                curiosity_gap="What did the proof reveal?",
                focal_description="A proof card sits under a bold headline with one censored clue.",
                focal_position="bottom",
                eye_path=["headline", "proof card", "highlight strip", "subhook"],
                pattern_interrupt="A censored bar hides one useful detail without inventing facts.",
                motion=motion_plan(motion_intensity, "default"),
            ),
            make_strategy(
                main_hook=hooks[4 if len(hooks) > 4 else 0],
                sub_hook="The fix is obvious once you see the split",
                request=request,
                visual_category="transformation",
                signal_type="before_after",
                signal_description="A split frame shows problem and improved state before any reading.",
                stakes_type="gain",
                stakes_description=f"{audience} sees a path from weak result to stronger result.",
                curiosity_gap="What changed between the two sides?",
                focal_description="Diagonal before/after split with the headline crossing the boundary.",
                focal_position="center",
                eye_path=["before side", "split line", "after side", "headline"],
                pattern_interrupt="The fixed side cuts sharply through the flawed side.",
                motion=motion_plan(motion_intensity, "kinetic"),
            ),
        ]
    )

    if motion_intensity != "none":
        strategies.append(
            make_strategy(
                main_hook=hooks[5 if len(hooks) > 5 else 0],
                sub_hook="The first frame already tells the story",
                request=request,
                visual_category=category,
                signal_type="motion",
                signal_description="A motion-first reveal uses a visible headline from frame zero.",
                stakes_type="time",
                stakes_description="The viewer gets the signal instantly, then motion points to the open loop.",
                curiosity_gap=f"What is about to be revealed about {topic_one}?",
                focal_description="Kinetic slabs reveal the hook and point to one bright target.",
                focal_position="center",
                eye_path=["headline", "moving slab", "highlight target"],
                pattern_interrupt="A hard wipe interrupts a static poster composition.",
                motion=motion_plan(motion_intensity, "kinetic"),
            )
        )

    strategies.extend(
        [
            make_strategy(
                main_hook=hooks[6 if len(hooks) > 6 else 0],
                sub_hook="A simple warning before the costly part",
                request=request,
                visual_category="warning",
                signal_type="object",
                signal_description="A single broken object or danger sticker carries the warning.",
                stakes_type="loss",
                stakes_description=f"The cover frames a specific avoidable mistake for {audience}.",
                curiosity_gap=f"What should viewers stop doing with {topic_one}?",
                focal_description="Giant warning word plus one marked object, no competing stickers.",
                focal_position="top",
                eye_path=["warning word", "marked object", "subhook"],
                pattern_interrupt="One red mark interrupts the otherwise restrained brand palette.",
                motion=motion_plan(motion_intensity, "warning"),
            ),
            make_strategy(
                main_hook=hooks[7 if len(hooks) > 7 else 0],
                sub_hook="The metaphor exposes the hidden cost",
                request=request,
                visual_category="surreal",
                signal_type="object",
                signal_description=f"One impossible scale metaphor makes {topic_short} feel concrete.",
                stakes_type="surprise",
                stakes_description=f"The viewer sees the invisible cost of {topic_short}.",
                curiosity_gap="Why is the object so out of scale?",
                focal_description="One giant symbolic object dominates, with empty space for editable text.",
                focal_position="rule_of_thirds_right",
                eye_path=["giant object", "headline", "subhook"],
                pattern_interrupt="An impossible scale relationship breaks the expected poster logic.",
                motion=motion_plan(motion_intensity, "default"),
            ),
        ]
    )
    return strategies[:8]


def choose_template(strategy: dict[str, Any], request: dict[str, Any]) -> CoverTemplateId:
    signal = nested_dict(strategy.get("signal"))
    visual_category = normalize_space(strategy.get("visualCategory"))
    signal_type = normalize_space(signal.get("type"))
    if visual_category == "transformation" or signal_type == "before_after":
        return "before_after_split"
    if visual_category == "proof" or signal_type == "proof_screenshot":
        return "proof_receipt"
    if nested_dict(strategy.get("humanCue")).get("use") and request["creativeDirection"]["allowHumanFace"] is not False:
        return "face_reaction_object"
    if signal_type == "pattern_break":
        return "pattern_break_grid"
    if visual_category == "surreal" or request["creativeDirection"]["allowSurrealImages"]:
        return "surreal_scale"
    if request["creativeDirection"]["motionIntensity"] == "kinetic":
        return "kinetic_reveal"
    if visual_category in {"mistake", "warning"}:
        return "mistake_warning"
    return "oversized_type"


def asset_id(strategy: dict[str, Any], template_id: str, role: str) -> str:
    digest = hashlib.sha1(
        f"{template_id}\n{role}\n{strategy.get('mainHook')}\n{strategy.get('curiosityGap')}".encode("utf-8")
    ).hexdigest()[:10]
    return f"ssc_{role}_{digest}"


def build_image_asset_prompt(strategy: dict[str, Any], template_id: str, request: dict[str, Any]) -> str:
    brand = request["brand"]
    signal = nested_dict(strategy.get("signal"))
    human = nested_dict(strategy.get("humanCue"))
    tone = request["creativeDirection"]["tone"]
    base = [
        "Create a high-impact social media carousel cover asset.",
        "The final headline and labels will be editable HTML text outside this image.",
        f"Topic: {request['topic']}.",
        f"Content promise: {strategy.get('contentPromise')}.",
        f"Brand tone: {tone}; brand colors should inform mood, not create logos.",
    ]
    if template_id == "face_reaction_object" or human.get("use"):
        emotion = normalize_space(human.get("emotion")) or "concerned curiosity"
        gaze = normalize_space(human.get("gazeTarget")) or "headline"
        base.extend(
            [
                f"Subject: one expressive adult person, close-up, emotionally readable expression: {emotion}.",
                f"Pose/gaze: looking toward the {gaze} where the HTML headline or object will be placed.",
                "Composition: subject should occupy the right 55% of a vertical 4:5 frame, with clean negative space on the left.",
                "Lighting/style: bold editorial lighting, high contrast, modern creator-thumbnail energy, crisp details.",
                "Background: simple, non-distracting, easy to crop or mask.",
            ]
        )
    elif template_id == "surreal_scale" or strategy.get("visualCategory") == "surreal":
        base.extend(
            [
                f"Subject: one surreal but clean visual metaphor for {signal.get('description') or request['topic']}.",
                "Composition: one dominant impossible object, centered slightly right, with empty dark space on the left for HTML text.",
                "Style: bold editorial poster, high value contrast, sharp silhouette, minimal clutter.",
            ]
        )
    else:
        base.extend(
            [
                f"Subject: one clean symbolic object representing {signal.get('description') or request['topic']}.",
                "Composition: single dominant subject with enough negative space for HTML headline overlay.",
                "Style: editorial cover asset, high value contrast, simple background, no tiny details.",
            ]
        )
    if brand.get("name"):
        base.append(f"Fit the visual confidence of {brand['name']}, but do not include brand marks.")
    base.append("Important: no text, no letters, no numbers, no logos, no watermark, no UI, no brand marks.")
    base.append("Purpose: this image will sit behind editable HTML/CSS headline text.")
    return " ".join(base)


def uploaded_asset(url: str, role: str, strategy: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": asset_id(strategy, "uploaded", role),
        "kind": "uploaded_image",
        "url": url,
        "alt": f"User-provided {role} image for {strategy.get('mainHook')}",
        "source": "user",
        "placement": {"role": role, "x": 520, "y": 160, "width": 500, "height": 820, "zIndex": 10},
    }


def plan_assets(strategy: dict[str, Any], template_id: CoverTemplateId, request: dict[str, Any]) -> list[dict[str, Any]]:
    assets = request["assets"]
    if template_id == "face_reaction_object" and assets.get("userProvidedFaceUrl"):
        return [uploaded_asset(assets["userProvidedFaceUrl"], "face", strategy)]
    if template_id in {"proof_receipt", "before_after_split"} and assets.get("existingImageUrls"):
        return [uploaded_asset(assets["existingImageUrls"][0], "proof", strategy)]
    if template_id in {"face_reaction_object", "surreal_scale"} and not request["creativeDirection"]["allowGeneratedImages"]:
        return []
    if template_id in {"face_reaction_object", "surreal_scale"}:
        role = "face" if template_id == "face_reaction_object" else "object"
        return [
            {
                "id": asset_id(strategy, template_id, role),
                "kind": "generated_image",
                "alt": f"{role.title()} asset for cover hook: {strategy.get('mainHook')}",
                "prompt": build_image_asset_prompt(strategy, template_id, request),
                "model": os.environ.get("OPENAI_IMAGE_MODEL") or DEFAULT_OPENAI_IMAGE_MODEL,
                "source": "openai",
                "placement": {"role": role, "x": 500, "y": 120, "width": 560, "height": 900, "zIndex": 10},
            }
        ]
    return []


def maybe_generate_assets(
    planned_assets: list[dict[str, Any]],
    *,
    out_dir: Path | None = None,
    generate_images: bool = False,
    image_model: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    if not planned_assets:
        return [], 0
    if not generate_images:
        return [dict(asset) for asset in planned_assets], 0
    if not openai_api_key():
        return [dict(asset, generationError="OPENAI_API_KEY not set") for asset in planned_assets], 0
    generated: list[dict[str, Any]] = []
    latency_ms = 0
    asset_dir = (out_dir or OUT / "scroll_stopper_cover") / "generated_assets"
    for asset in planned_assets:
        item = dict(asset)
        if item.get("kind") != "generated_image" or not item.get("prompt"):
            generated.append(item)
            continue
        path = asset_dir / f"{item['id']}.png"
        start = time.monotonic()
        try:
            if not path.exists():
                generate_openai(
                    string_value(item["prompt"]),
                    path,
                    model=image_model or string_value(item.get("model")) or None,
                    size=os.environ.get("SCROLL_STOPPER_IMAGE_SIZE", "1024x1536"),
                )
            item["url"] = path.resolve().as_uri()
            item["model"] = image_model or string_value(item.get("model")) or os.environ.get("OPENAI_IMAGE_MODEL") or DEFAULT_OPENAI_IMAGE_MODEL
        except Exception as exc:  # pragma: no cover - defensive around network/client failures
            item["generationError"] = str(exc)
        latency_ms += int((time.monotonic() - start) * 1000)
        generated.append(item)
    return generated, latency_ms


def headline_class(strategy: dict[str, Any]) -> str:
    if nested_dict(strategy.get("motionPlan")).get("intensity") in {"subtle", "kinetic"}:
        return "ssc-headline ssc-headline-kinetic"
    return "ssc-headline"


def accent_class(strategy: dict[str, Any]) -> str:
    if nested_dict(strategy.get("motionPlan")).get("intensity") in {"subtle", "kinetic"}:
        return "ssc-accent ssc-motion-pop"
    return "ssc-accent"


def render_asset_image(assets: list[dict[str, Any]], class_name: str) -> str:
    for asset in assets:
        if normalize_space(asset.get("url")):
            src = html_lib.escape(normalize_space(asset["url"]), quote=True)
            alt = html_lib.escape(normalize_space(asset.get("alt")) or "Generated cover asset", quote=True)
            return f'<img class="{class_name}" src="{src}" alt="{alt}" />'
    return ""


def kinetic_text_markup(value: str) -> str:
    words = re.findall(r"\S+", normalize_space(value))
    if not words:
        return ""
    return " ".join(
        f'<span class="ssc-word" style="--i: {index};">{html_lib.escape(word)}</span>'
        for index, word in enumerate(words)
    )


def motion_field_markup(motion_intensity: str) -> str:
    if motion_intensity == "none":
        return ""
    return """
  <div class="ssc-motion-field" aria-hidden="true">
    <span class="ssc-route ssc-route-one"></span>
    <span class="ssc-route ssc-route-two"></span>
    <span class="ssc-route ssc-route-three"></span>
    <span class="ssc-motion-node ssc-motion-node-one"></span>
    <span class="ssc-motion-node ssc-motion-node-two"></span>
    <span class="ssc-motion-node ssc-motion-node-three"></span>
    <span class="ssc-scan-band"></span>
  </div>""".rstrip()


def render_face_template(strategy: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    image_markup = render_asset_image(assets, "ssc-hero ssc-face")
    if not image_markup:
        image_markup = """
  <div class="ssc-hero ssc-face-placeholder" aria-hidden="true">
    <div class="ssc-face-shape"></div>
    <div class="ssc-face-eye ssc-face-eye-left"></div>
    <div class="ssc-face-eye ssc-face-eye-right"></div>
    <div class="ssc-face-mouth"></div>
  </div>""".rstrip()
    return f"""
  {image_markup}
  <div class="{accent_class(strategy)} ssc-highlight-ring" aria-hidden="true"></div>
  <div class="ssc-object-chip" aria-hidden="true">!</div>"""


def render_warning_template(strategy: dict[str, Any]) -> str:
    return f"""
  <div class="ssc-warning-panel" aria-hidden="true">
    <div class="ssc-broken-card"></div>
    <div class="{accent_class(strategy)} ssc-danger-mark">X</div>
  </div>
  <div class="ssc-warning-stamp" aria-hidden="true">WAIT</div>"""


def render_before_after_template(strategy: dict[str, Any]) -> str:
    motion = " ssc-motion-reveal-x" if nested_dict(strategy.get("motionPlan")).get("intensity") != "none" else ""
    return f"""
  <div class="ssc-split" aria-hidden="true">
    <div class="ssc-before"><span>BEFORE</span></div>
    <div class="ssc-after{motion}"><span>AFTER</span></div>
    <div class="{accent_class(strategy)} ssc-split-handle"></div>
  </div>"""


def render_pattern_template(strategy: dict[str, Any]) -> str:
    cells = []
    for index in range(12):
        classes = "ssc-grid-cell"
        if index == 7:
            classes += " ssc-grid-cell-break"
            if nested_dict(strategy.get("motionPlan")).get("intensity") != "none":
                classes += " ssc-motion-pop"
        cells.append(f'<div class="{classes}" style="--i: {index};" aria-hidden="true"></div>')
    return f"""
  <div class="ssc-pattern-grid" aria-hidden="true">
    {''.join(cells)}
  </div>
  <div class="{accent_class(strategy)} ssc-arrow" aria-hidden="true"></div>"""


def render_proof_template(strategy: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    image_markup = render_asset_image(assets, "ssc-proof-image")
    if image_markup:
        return f"""
  <div class="ssc-proof-receipt ssc-has-image">
    {image_markup}
    <div class="{accent_class(strategy)} ssc-proof-highlight" aria-hidden="true"></div>
  </div>"""
    return f"""
  <div class="ssc-proof-receipt" aria-label="Stylized proof card">
    <div class="ssc-proof-row ssc-proof-row-wide" style="--i: 0;"></div>
    <div class="ssc-proof-row" style="--i: 1;"></div>
    <div class="ssc-proof-row ssc-proof-row-short" style="--i: 2;"></div>
    <div class="{accent_class(strategy)} ssc-proof-highlight" aria-hidden="true"></div>
    <div class="ssc-proof-censor" aria-hidden="true"></div>
  </div>"""


def render_oversized_template(strategy: dict[str, Any]) -> str:
    return f"""
  <div class="ssc-type-echo" aria-hidden="true">{html_lib.escape(compact_topic(strategy.get('mainHook', ''), 1))}</div>
  <div class="{accent_class(strategy)} ssc-underline" aria-hidden="true"></div>"""


def render_surreal_template(strategy: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    image_markup = render_asset_image(assets, "ssc-hero ssc-surreal-image")
    if image_markup:
        return f"""
  {image_markup}
  <div class="{accent_class(strategy)} ssc-scale-shadow" aria-hidden="true"></div>"""
    return f"""
  <div class="ssc-surreal-object" aria-hidden="true">
    <div class="ssc-giant-disc"></div>
    <div class="ssc-tiny-card"></div>
  </div>
  <div class="{accent_class(strategy)} ssc-scale-shadow" aria-hidden="true"></div>"""


def render_kinetic_template(strategy: dict[str, Any]) -> str:
    return """
  <div class="ssc-kinetic-stack" aria-hidden="true">
    <div class="ssc-kinetic-slab ssc-kinetic-slab-one ssc-motion-reveal-x"></div>
    <div class="ssc-kinetic-slab ssc-kinetic-slab-two ssc-motion-pop"></div>
    <div class="ssc-kinetic-target ssc-motion-pulse-loop"></div>
  </div>"""


def template_body(template_id: CoverTemplateId, strategy: dict[str, Any], assets: list[dict[str, Any]]) -> str:
    if template_id == "face_reaction_object":
        return render_face_template(strategy, assets)
    if template_id == "mistake_warning":
        return render_warning_template(strategy)
    if template_id == "before_after_split":
        return render_before_after_template(strategy)
    if template_id == "pattern_break_grid":
        return render_pattern_template(strategy)
    if template_id == "proof_receipt":
        return render_proof_template(strategy, assets)
    if template_id == "surreal_scale":
        return render_surreal_template(strategy, assets)
    if template_id == "kinetic_reveal":
        return render_kinetic_template(strategy)
    return render_oversized_template(strategy)


def base_css(request: dict[str, Any], motion_enabled: bool) -> str:
    brand = request["brand"]
    colors = brand["colors"]
    font = ", ".join(brand.get("fonts") or ["Archivo", "ui-sans-serif", "system-ui"])
    width = request["format"]["width"]
    height = request["format"]["height"]
    css = f"""
.ssc-cover,
.ssc-cover *,
.ssc-cover *::before,
.ssc-cover *::after {{
  box-sizing: border-box;
}}
.ssc-cover {{
  --ssc-width: {width}px;
  --ssc-height: {height}px;
  --ssc-bg: {colors['bg']};
  --ssc-bg-top: {colors['bgTop']};
  --ssc-fg: {colors['fg']};
  --ssc-accent: {colors['accent']};
  --ssc-danger: {colors['danger']};
  --ssc-paper: {colors['paper']};
  --ssc-dark: {colors['dark']};
  --ssc-muted: rgba(20, 18, 14, 0.64);
  --ssc-safe: 64px;
  position: relative;
  width: var(--ssc-width);
  height: var(--ssc-height);
  aspect-ratio: {width} / {height};
  overflow: hidden;
  isolation: isolate;
  background: var(--ssc-bg);
  color: var(--ssc-fg);
  font-family: {font}, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
.ssc-bg {{
  position: absolute;
  inset: 0;
  z-index: 0;
  background:
    linear-gradient(180deg, var(--ssc-bg-top) 0%, var(--ssc-bg) 58%, #EEE8DC 100%),
    radial-gradient(circle at 88% 14%, rgba(255, 59, 48, 0.18), transparent 24%);
}}
.ssc-bg::after {{
  content: "";
  position: absolute;
  inset: 0;
  background-image:
    radial-gradient(circle at 20% 30%, rgba(20, 18, 14, 0.13) 0 0.8px, transparent 1px),
    radial-gradient(circle at 70% 60%, rgba(192, 85, 46, 0.12) 0 0.7px, transparent 1px);
  background-size: 7px 7px, 11px 11px;
  opacity: 0.38;
}}
.ssc-headline {{
  position: absolute;
  z-index: 30;
  left: var(--ssc-safe);
  right: var(--ssc-safe);
  top: 118px;
  margin: 0;
  max-width: 880px;
  font-size: var(--ssc-headline-size, 128px);
  line-height: 0.9;
  letter-spacing: 0;
  font-weight: 900;
  text-wrap: balance;
}}
.ssc-subhead {{
  position: absolute;
  z-index: 30;
  left: var(--ssc-safe);
  right: var(--ssc-safe);
  bottom: 108px;
  margin: 0;
  max-width: 740px;
  font-size: 44px;
  line-height: 1.05;
  letter-spacing: 0;
  font-weight: 760;
  color: var(--ssc-fg);
}}
.ssc-kicker {{
  position: absolute;
  z-index: 34;
  left: var(--ssc-safe);
  top: 58px;
  display: inline-flex;
  align-items: center;
  gap: 14px;
  color: var(--ssc-accent);
  font-size: 24px;
  font-weight: 840;
  line-height: 1;
  letter-spacing: 0;
  text-transform: uppercase;
}}
.ssc-kicker::before {{
  content: "";
  width: 38px;
  height: 6px;
  background: currentColor;
}}
.ssc-hero {{
  position: absolute;
  z-index: 12;
  user-select: none;
  pointer-events: none;
  object-fit: cover;
}}
.ssc-accent {{
  position: absolute;
  z-index: 24;
  pointer-events: none;
}}
.ssc-template-face-reaction-object .ssc-headline {{
  max-width: 560px;
  top: 148px;
}}
.ssc-template-face-reaction-object .ssc-subhead {{
  max-width: 540px;
}}
.ssc-face {{
  right: 0;
  bottom: 0;
  width: 600px;
  height: 980px;
  object-position: center bottom;
}}
.ssc-face-placeholder {{
  right: 44px;
  bottom: 108px;
  width: 472px;
  height: 640px;
}}
.ssc-face-shape {{
  position: absolute;
  inset: 0;
  border-radius: 48% 52% 42% 46%;
  background:
    radial-gradient(circle at 36% 34%, var(--ssc-paper) 0 16%, transparent 17%),
    linear-gradient(145deg, #F0C7A7, #8F3A26 78%);
  box-shadow: 0 36px 90px rgba(20, 18, 14, 0.28);
}}
.ssc-face-eye {{
  position: absolute;
  top: 236px;
  width: 42px;
  height: 20px;
  border-radius: 50%;
  background: var(--ssc-dark);
}}
.ssc-face-eye-left {{ left: 142px; }}
.ssc-face-eye-right {{ left: 284px; }}
.ssc-face-mouth {{
  position: absolute;
  left: 182px;
  top: 342px;
  width: 96px;
  height: 52px;
  border: 12px solid var(--ssc-dark);
  border-top: 0;
  border-radius: 0 0 80px 80px;
}}
.ssc-highlight-ring {{
  right: 336px;
  top: 698px;
  width: 190px;
  height: 132px;
  border: 12px solid var(--ssc-accent);
  border-radius: 50%;
  transform: rotate(-12deg);
  box-shadow: 0 0 0 8px rgba(255, 255, 255, 0.55);
}}
.ssc-object-chip {{
  position: absolute;
  right: 400px;
  top: 720px;
  z-index: 18;
  width: 96px;
  height: 96px;
  display: grid;
  place-items: center;
  border-radius: 8px;
  background: var(--ssc-dark);
  color: #FFFFFF;
  font-size: 58px;
  font-weight: 900;
  transform: rotate(8deg);
}}
.ssc-warning-panel {{
  position: absolute;
  z-index: 10;
  right: 72px;
  bottom: 188px;
  width: 412px;
  height: 560px;
  transform: rotate(5deg);
}}
.ssc-broken-card {{
  position: absolute;
  inset: 0;
  border: 8px solid var(--ssc-dark);
  border-radius: 8px;
  background:
    linear-gradient(135deg, rgba(255, 255, 255, 0.74), rgba(255, 255, 255, 0.16)),
    repeating-linear-gradient(0deg, transparent 0 54px, rgba(20, 18, 14, 0.16) 54px 62px);
  box-shadow: 0 32px 80px rgba(20, 18, 14, 0.24);
}}
.ssc-danger-mark {{
  right: 88px;
  top: 174px;
  display: grid;
  place-items: center;
  width: 220px;
  height: 220px;
  border-radius: 50%;
  background: var(--ssc-danger);
  color: #FFFFFF;
  font-size: 148px;
  line-height: 1;
  font-weight: 950;
}}
.ssc-warning-stamp {{
  position: absolute;
  z-index: 26;
  left: 72px;
  top: 650px;
  padding: 18px 24px;
  border: 8px solid var(--ssc-danger);
  border-radius: 8px;
  color: var(--ssc-danger);
  font-size: 56px;
  font-weight: 950;
  transform: rotate(-8deg);
}}
.ssc-split {{
  position: absolute;
  inset: 0;
  z-index: 5;
  display: grid;
  grid-template-columns: 1fr 1fr;
}}
.ssc-before,
.ssc-after {{
  position: relative;
  display: flex;
  align-items: flex-end;
  padding: 76px;
  font-size: 52px;
  font-weight: 920;
}}
.ssc-before {{
  color: rgba(255, 255, 255, 0.84);
  background:
    linear-gradient(160deg, rgba(16, 16, 20, 0.94), rgba(62, 49, 44, 0.94)),
    repeating-linear-gradient(135deg, rgba(255, 255, 255, 0.08) 0 8px, transparent 8px 28px);
}}
.ssc-after {{
  color: var(--ssc-dark);
  background:
    linear-gradient(160deg, #FFF9ED, #F4C987),
    radial-gradient(circle at 60% 25%, rgba(192, 85, 46, 0.24), transparent 28%);
}}
.ssc-split-handle {{
  left: 50%;
  top: 0;
  bottom: 0;
  width: 18px;
  background: var(--ssc-accent);
  box-shadow: 0 0 0 10px rgba(255, 255, 255, 0.56);
}}
.ssc-template-before-after-split .ssc-headline {{
  top: 438px;
  left: 88px;
  right: 88px;
  max-width: 900px;
  color: #FFFFFF;
  text-shadow: 0 5px 24px rgba(16, 16, 20, 0.66);
}}
.ssc-template-before-after-split .ssc-subhead {{
  color: #FFFFFF;
  text-shadow: 0 3px 18px rgba(16, 16, 20, 0.72);
}}
.ssc-pattern-grid {{
  position: absolute;
  z-index: 5;
  right: 64px;
  bottom: 252px;
  width: 540px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 22px;
}}
.ssc-grid-cell {{
  height: 144px;
  border: 5px solid rgba(20, 18, 14, 0.36);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.54);
}}
.ssc-grid-cell-break {{
  border-color: var(--ssc-danger);
  background: var(--ssc-danger);
  transform: rotate(-10deg) scale(1.18);
  box-shadow: 0 22px 54px rgba(20, 18, 14, 0.24);
}}
.ssc-arrow {{
  right: 388px;
  bottom: 492px;
  width: 180px;
  height: 12px;
  background: var(--ssc-accent);
  transform: rotate(-15deg);
}}
.ssc-arrow::after {{
  content: "";
  position: absolute;
  right: -4px;
  top: -18px;
  border-left: 42px solid var(--ssc-accent);
  border-top: 24px solid transparent;
  border-bottom: 24px solid transparent;
}}
.ssc-proof-receipt {{
  position: absolute;
  z-index: 10;
  right: 72px;
  bottom: 168px;
  width: 452px;
  min-height: 612px;
  padding: 46px 38px;
  border: 6px solid var(--ssc-dark);
  border-radius: 8px;
  background: #FFFFFF;
  box-shadow: 0 34px 90px rgba(20, 18, 14, 0.30);
  transform: rotate(4deg);
}}
.ssc-proof-image {{
  display: block;
  width: 100%;
  height: 500px;
  object-fit: cover;
  border-radius: 6px;
}}
.ssc-proof-row {{
  height: 32px;
  margin-bottom: 28px;
  border-radius: 8px;
  background: rgba(20, 18, 14, 0.18);
}}
.ssc-proof-row-wide {{ width: 100%; }}
.ssc-proof-row-short {{ width: 58%; }}
.ssc-proof-highlight {{
  left: 26px;
  right: 26px;
  top: 254px;
  height: 88px;
  border: 8px solid var(--ssc-accent);
  border-radius: 8px;
}}
.ssc-proof-censor {{
  position: absolute;
  left: 72px;
  right: 88px;
  bottom: 102px;
  height: 46px;
  background: var(--ssc-dark);
}}
.ssc-type-echo {{
  position: absolute;
  z-index: 2;
  right: -42px;
  bottom: 196px;
  color: rgba(192, 85, 46, 0.18);
  font-size: 280px;
  line-height: 0.8;
  font-weight: 950;
  text-transform: uppercase;
  transform: rotate(-8deg);
}}
.ssc-underline {{
  left: 76px;
  top: 528px;
  width: 520px;
  height: 24px;
  border-radius: 8px;
  background: var(--ssc-accent);
  transform: rotate(-3deg);
}}
.ssc-surreal-image {{
  right: -24px;
  bottom: 76px;
  width: 630px;
  height: 870px;
  object-fit: contain;
}}
.ssc-surreal-object {{
  position: absolute;
  z-index: 8;
  right: -96px;
  bottom: 170px;
  width: 660px;
  height: 690px;
}}
.ssc-giant-disc {{
  position: absolute;
  inset: 0;
  border-radius: 50%;
  background:
    radial-gradient(circle at 35% 28%, rgba(255, 255, 255, 0.84), transparent 18%),
    linear-gradient(135deg, var(--ssc-dark), var(--ssc-accent));
  box-shadow: 0 42px 120px rgba(20, 18, 14, 0.34);
}}
.ssc-tiny-card {{
  position: absolute;
  left: 66px;
  bottom: 76px;
  width: 128px;
  height: 172px;
  border: 7px solid #FFFFFF;
  border-radius: 8px;
  background: var(--ssc-danger);
}}
.ssc-scale-shadow {{
  right: 52px;
  bottom: 126px;
  width: 560px;
  height: 60px;
  border-radius: 50%;
  background: rgba(20, 18, 14, 0.20);
  filter: blur(10px);
}}
.ssc-kinetic-stack {{
  position: absolute;
  inset: 0;
  z-index: 6;
}}
.ssc-kinetic-slab {{
  position: absolute;
  border-radius: 8px;
  background: var(--ssc-dark);
}}
.ssc-kinetic-slab-one {{
  left: 0;
  top: 330px;
  width: 760px;
  height: 220px;
}}
.ssc-kinetic-slab-two {{
  right: 82px;
  bottom: 252px;
  width: 370px;
  height: 370px;
  background: var(--ssc-accent);
  transform: rotate(10deg);
}}
.ssc-kinetic-target {{
  position: absolute;
  right: 184px;
  bottom: 348px;
  width: 146px;
  height: 146px;
  border-radius: 50%;
  border: 12px solid #FFFFFF;
  background: var(--ssc-danger);
}}
.ssc-template-kinetic-reveal .ssc-headline {{
  top: 148px;
  color: #FFFFFF;
  text-shadow: 0 6px 24px rgba(16, 16, 20, 0.64);
}}
.ssc-template-kinetic-reveal .ssc-bg {{
  background: linear-gradient(160deg, var(--ssc-dark), #382018 62%, var(--ssc-accent));
}}
.ssc-template-kinetic-reveal .ssc-subhead {{
  color: #FFFFFF;
}}
""".strip()
    motion_css = """
.ssc-motion-pop {
  animation: ssc-pop 900ms cubic-bezier(.16, 1.35, .24, 1) both;
}
.ssc-motion-slam {
  animation: ssc-slam 780ms cubic-bezier(.12, .9, .24, 1) both;
}
.ssc-motion-reveal-x {
  animation: ssc-reveal-x 1100ms cubic-bezier(.18, .8, .18, 1) both;
}
.ssc-motion-pulse-loop {
  animation: ssc-pulse 2400ms ease-in-out infinite;
}
.ssc-motion-field {
  position: absolute;
  inset: 0;
  z-index: 1;
  pointer-events: none;
  overflow: hidden;
  opacity: 0.78;
  mix-blend-mode: multiply;
}
.ssc-motion-field::before {
  content: "";
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(192, 85, 46, 0.11) 0 1px, transparent 1px 96px),
    linear-gradient(0deg, rgba(20, 18, 14, 0.07) 0 1px, transparent 1px 96px);
  animation: ssc-grid-drift 7.2s linear infinite;
}
.ssc-route {
  position: absolute;
  left: -8%;
  width: 116%;
  height: 5px;
  background: linear-gradient(90deg, transparent, var(--ssc-accent), rgba(20, 18, 14, 0.16), transparent);
  clip-path: inset(0 100% 0 0);
  transform-origin: 0 50%;
  animation: ssc-route-draw 4.8s linear infinite;
}
.ssc-route-one { top: 402px; transform: rotate(-17deg); animation-delay: 0ms; }
.ssc-route-two { top: 660px; transform: rotate(6deg); animation-delay: 180ms; }
.ssc-route-three { top: 920px; transform: rotate(19deg); animation-delay: 360ms; }
.ssc-motion-node {
  position: absolute;
  width: 92px;
  height: 92px;
  border: 4px solid rgba(20, 18, 14, 0.42);
  background: rgba(244, 242, 236, 0.82);
  box-shadow: 14px 14px 0 rgba(192, 85, 46, 0.13);
  animation: ssc-node-pop 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-motion-node-one { left: 86px; top: 470px; animation-delay: 0ms; }
.ssc-motion-node-two { right: 132px; top: 368px; border-color: var(--ssc-accent); animation-delay: 160ms; }
.ssc-motion-node-three { right: 176px; bottom: 300px; animation-delay: 320ms; }
.ssc-scan-band {
  position: absolute;
  top: -18%;
  left: -26%;
  width: 152%;
  height: 220px;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.32), rgba(192, 85, 46, 0.22), transparent);
  transform: rotate(-18deg) translate3d(-20%, 0, 0);
  filter: blur(10px);
  opacity: 0;
  animation: ssc-scan-sweep 4.8s cubic-bezier(.18, .8, .18, 1) infinite;
}
.ssc-cover[data-motion="kinetic"] .ssc-bg::before,
.ssc-cover[data-motion="subtle"] .ssc-bg::before {
  content: "";
  position: absolute;
  inset: -12%;
  z-index: 0;
  background:
    radial-gradient(circle at 18% 18%, rgba(192, 85, 46, 0.18), transparent 22%),
    radial-gradient(circle at 84% 22%, rgba(20, 18, 14, 0.10), transparent 24%);
  animation: ssc-bg-breathe 5.4s ease-in-out infinite;
}
.ssc-cover[data-motion="kinetic"] .ssc-bg::after,
.ssc-cover[data-motion="subtle"] .ssc-bg::after {
  animation: ssc-grain-drift 4.8s linear infinite;
}
.ssc-headline-kinetic {
  will-change: opacity, transform, filter;
  animation: ssc-headline-enter 920ms cubic-bezier(.16, 1.1, .22, 1) both;
}
.ssc-word {
  display: inline-block;
  transform-origin: left bottom;
  will-change: transform, filter;
  animation: ssc-word-rubber 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
  animation-delay: calc(var(--i, 0) * 92ms);
}
.ssc-subhead {
  will-change: transform, opacity;
}
.ssc-cover[data-motion="kinetic"] .ssc-subhead,
.ssc-cover[data-motion="subtle"] .ssc-subhead {
  animation: ssc-subhead-lift 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"] .ssc-kicker,
.ssc-cover[data-motion="subtle"] .ssc-kicker {
  animation: ssc-kicker-snap 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-face-reaction-object .ssc-face-placeholder,
.ssc-cover[data-motion="kinetic"].ssc-template-face-reaction-object .ssc-face {
  animation: ssc-face-approach 4.8s ease-in-out infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-face-reaction-object .ssc-object-chip {
  animation: ssc-chip-jolt 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-face-reaction-object .ssc-highlight-ring {
  animation: ssc-ring-draw 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-mistake-warning .ssc-warning-panel {
  animation: ssc-card-flip-in 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-mistake-warning .ssc-danger-mark {
  animation: ssc-danger-stomp 4.8s cubic-bezier(.18, 1.45, .24, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-mistake-warning .ssc-warning-stamp {
  animation: ssc-stamp-slam 4.8s cubic-bezier(.18, 1.35, .24, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-before-after-split .ssc-after {
  animation: ssc-split-swipe 4.8s cubic-bezier(.18, .8, .18, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-before-after-split .ssc-split-handle {
  animation: ssc-handle-scan 4.8s cubic-bezier(.18, .8, .18, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-pattern-break-grid .ssc-pattern-grid {
  animation: ssc-grid-tilt 4.8s ease-in-out infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-pattern-break-grid .ssc-grid-cell {
  animation: ssc-cell-sequence 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
  animation-delay: calc(var(--i, 0) * 42ms);
}
.ssc-cover[data-motion="kinetic"].ssc-template-pattern-break-grid .ssc-grid-cell-break {
  animation: ssc-anomaly-break 4.8s cubic-bezier(.16, 1.35, .24, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-pattern-break-grid .ssc-arrow {
  transform-origin: 0 50%;
  animation: ssc-arrow-draw 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-proof-receipt .ssc-proof-receipt {
  animation: ssc-receipt-slide 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-proof-receipt .ssc-proof-row {
  transform-origin: 0 50%;
  animation: ssc-row-wipe 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
  animation-delay: calc(var(--i, 0) * 70ms);
}
.ssc-cover[data-motion="kinetic"].ssc-template-proof-receipt .ssc-proof-highlight {
  animation: ssc-proof-sweep 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-proof-receipt .ssc-proof-censor {
  animation: ssc-censor-snap 4.8s steps(1, end) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-oversized-type .ssc-type-echo {
  animation: ssc-echo-slide 4.8s ease-in-out infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-oversized-type .ssc-underline {
  transform-origin: 0 50%;
  animation: ssc-underline-draw 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-surreal-scale .ssc-surreal-object,
.ssc-cover[data-motion="kinetic"].ssc-template-surreal-scale .ssc-surreal-image {
  animation: ssc-surreal-approach 4.8s ease-in-out infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-surreal-scale .ssc-tiny-card {
  animation: ssc-tiny-card-alert 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-surreal-scale .ssc-scale-shadow {
  animation: ssc-shadow-breathe 4.8s ease-in-out infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-kinetic-reveal .ssc-kinetic-slab-one {
  animation: ssc-slab-one 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-kinetic-reveal .ssc-kinetic-slab-two {
  animation: ssc-slab-two 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
.ssc-cover[data-motion="kinetic"].ssc-template-kinetic-reveal .ssc-kinetic-target {
  animation: ssc-target-lock 4.8s cubic-bezier(.16, 1, .3, 1) infinite;
}
@keyframes ssc-pop {
  0% { transform: translate3d(0, 56px, 0) scale(.72); opacity: 0; }
  62% { transform: translate3d(0, -10px, 0) scale(1.08); opacity: 1; }
  100% { transform: translate3d(0, 0, 0) scale(1); opacity: 1; }
}
@keyframes ssc-slam {
  0% { transform: translate3d(-9%, 0, 0) scale(1.18); opacity: 0; }
  72% { transform: translate3d(1%, 0, 0) scale(.98); opacity: 1; }
  100% { transform: translate3d(0, 0, 0) scale(1); opacity: 1; }
}
@keyframes ssc-reveal-x {
  0% { clip-path: inset(0 100% 0 0); }
  100% { clip-path: inset(0 0 0 0); }
}
@keyframes ssc-pulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.075); }
}
@keyframes ssc-headline-enter {
  0% { opacity: 0.72; transform: translate3d(-24px, 0, 0) scaleX(1.08); filter: blur(1.8px); }
  70%, 100% { opacity: 1; transform: translate3d(0, 0, 0) scaleX(1); filter: blur(0); }
}
@keyframes ssc-word-rubber {
  0%, 100% { transform: translate3d(0, 0, 0) scaleX(1); filter: blur(0); }
  8% { transform: translate3d(34px, 0, 0) scaleX(1.16); filter: blur(0.4px); }
  18% { transform: translate3d(-6px, 0, 0) scaleX(0.94); filter: blur(0); }
  28%, 74% { transform: translate3d(0, 0, 0) scaleX(1); filter: blur(0); }
  84% { transform: translate3d(10px, 0, 0) scaleX(1.04); filter: blur(0.2px); }
}
@keyframes ssc-subhead-lift {
  0%, 100% { transform: translate3d(0, 14px, 0); opacity: 0; }
  18%, 72% { transform: translate3d(0, 0, 0); opacity: 1; }
}
@keyframes ssc-kicker-snap {
  0%, 100% { transform: translate3d(-18px, 0, 0); opacity: 0; }
  12%, 74% { transform: translate3d(0, 0, 0); opacity: 1; }
}
@keyframes ssc-route-draw {
  0%, 16% { opacity: 0; clip-path: inset(0 100% 0 0); }
  32%, 60% { opacity: 0.9; clip-path: inset(0 0 0 0); }
  78%, 100% { opacity: 0; clip-path: inset(0 0 0 100%); }
}
@keyframes ssc-node-pop {
  0%, 12%, 88%, 100% { transform: translate3d(0, 24px, 0) rotate(8deg) scale(0.74); opacity: 0; }
  25%, 64% { transform: translate3d(0, 0, 0) rotate(0) scale(1); opacity: 0.62; }
}
@keyframes ssc-scan-sweep {
  0%, 18%, 100% { opacity: 0; transform: rotate(-18deg) translate3d(-32%, -80px, 0); }
  36% { opacity: 0.68; }
  62% { opacity: 0; transform: rotate(-18deg) translate3d(42%, 980px, 0); }
}
@keyframes ssc-grid-drift {
  0% { transform: translate(0, 0); }
  100% { transform: translate(96px, 96px); }
}
@keyframes ssc-bg-breathe {
  0%, 100% { transform: scale(1) translate3d(0, 0, 0); opacity: 0.72; }
  50% { transform: scale(1.045) translate3d(18px, -16px, 0); opacity: 1; }
}
@keyframes ssc-grain-drift {
  0% { transform: translate3d(0, 0, 0); }
  100% { transform: translate3d(18px, -14px, 0); }
}
@keyframes ssc-face-approach {
  0%, 100% { transform: translate3d(28px, 34px, 0) scale(.94); filter: blur(1.2px); opacity: 0; }
  18%, 70% { transform: translate3d(0, 0, 0) scale(1); filter: blur(0); opacity: 1; }
}
@keyframes ssc-chip-jolt {
  0%, 18%, 100% { transform: translate3d(24px, 18px, 0) rotate(18deg) scale(.7); opacity: 0; }
  30% { transform: translate3d(0, 0, 0) rotate(8deg) scale(1.1); opacity: 1; }
  42%, 74% { transform: translate3d(0, 0, 0) rotate(8deg) scale(1); opacity: 1; }
}
@keyframes ssc-ring-draw {
  0%, 30%, 100% { transform: rotate(-12deg) scale(.72); opacity: 0; clip-path: inset(0 100% 0 0); }
  46%, 74% { transform: rotate(-12deg) scale(1); opacity: 1; clip-path: inset(0 0 0 0); }
}
@keyframes ssc-card-flip-in {
  0%, 100% { transform: translate3d(60px, 80px, 0) rotate(18deg) scale(.82); opacity: 0; }
  22%, 72% { transform: translate3d(0, 0, 0) rotate(5deg) scale(1); opacity: 1; }
}
@keyframes ssc-danger-stomp {
  0%, 20%, 100% { transform: scale(.55); opacity: 0; }
  32% { transform: scale(1.2); opacity: 1; }
  42%, 72% { transform: scale(1); opacity: 1; }
}
@keyframes ssc-stamp-slam {
  0%, 28%, 100% { transform: translate3d(-42px, -36px, 0) rotate(-18deg) scale(1.22); opacity: 0; }
  38% { transform: translate3d(0, 0, 0) rotate(-8deg) scale(.94); opacity: 1; }
  48%, 72% { transform: translate3d(0, 0, 0) rotate(-8deg) scale(1); opacity: 1; }
}
@keyframes ssc-split-swipe {
  0%, 100% { clip-path: inset(0 100% 0 0); filter: brightness(.86); }
  26%, 72% { clip-path: inset(0 0 0 0); filter: brightness(1.05); }
}
@keyframes ssc-handle-scan {
  0%, 100% { transform: translateX(-420px); opacity: 0; }
  22%, 72% { transform: translateX(0); opacity: 1; }
}
@keyframes ssc-grid-tilt {
  0%, 100% { transform: translate3d(36px, 10px, 0) rotate(2deg); }
  18%, 70% { transform: translate3d(0, 0, 0) rotate(0deg); }
}
@keyframes ssc-cell-sequence {
  0%, 100% { transform: translate3d(0, 22px, 0) scale(.92); opacity: 0; }
  18%, 70% { transform: translate3d(0, 0, 0) scale(1); opacity: 1; }
}
@keyframes ssc-anomaly-break {
  0%, 24%, 100% { transform: translate3d(0, 22px, 0) rotate(8deg) scale(.86); opacity: 0; }
  34% { transform: translate3d(0, -10px, 0) rotate(-12deg) scale(1.28); opacity: 1; }
  48%, 72% { transform: translate3d(0, 0, 0) rotate(-10deg) scale(1.18); opacity: 1; }
}
@keyframes ssc-arrow-draw {
  0%, 32%, 100% { transform: rotate(-15deg) scaleX(0); opacity: 0; }
  42%, 70% { transform: rotate(-15deg) scaleX(1); opacity: 1; }
}
@keyframes ssc-receipt-slide {
  0%, 100% { transform: translate3d(80px, 70px, 0) rotate(12deg) scale(.84); opacity: 0; }
  22%, 72% { transform: translate3d(0, 0, 0) rotate(4deg) scale(1); opacity: 1; }
}
@keyframes ssc-row-wipe {
  0%, 22%, 100% { transform: scaleX(0); opacity: 0; }
  36%, 72% { transform: scaleX(1); opacity: 1; }
}
@keyframes ssc-proof-sweep {
  0%, 38%, 100% { transform: translateX(-34px) scaleX(.7); opacity: 0; }
  48%, 72% { transform: translateX(0) scaleX(1); opacity: 1; }
}
@keyframes ssc-censor-snap {
  0%, 44%, 100% { opacity: 0; }
  45%, 72% { opacity: 1; }
}
@keyframes ssc-echo-slide {
  0%, 100% { transform: translate3d(70px, 32px, 0) rotate(-8deg); opacity: 0; }
  24%, 72% { transform: translate3d(0, 0, 0) rotate(-8deg); opacity: 1; }
}
@keyframes ssc-underline-draw {
  0%, 34%, 100% { transform: rotate(-3deg) scaleX(0); opacity: 0; }
  44%, 72% { transform: rotate(-3deg) scaleX(1); opacity: 1; }
}
@keyframes ssc-surreal-approach {
  0%, 100% { transform: translate3d(84px, 36px, 0) scale(.76); filter: blur(3px); opacity: 0; }
  26%, 72% { transform: translate3d(0, 0, 0) scale(1); filter: blur(0); opacity: 1; }
}
@keyframes ssc-tiny-card-alert {
  0%, 36%, 100% { transform: translate3d(-20px, 20px, 0) scale(.7); opacity: 0; }
  48%, 72% { transform: translate3d(0, 0, 0) scale(1); opacity: 1; }
}
@keyframes ssc-shadow-breathe {
  0%, 100% { transform: scaleX(.72); opacity: 0; }
  30%, 72% { transform: scaleX(1); opacity: 1; }
}
@keyframes ssc-slab-one {
  0%, 100% { transform: translateX(-92%) scaleX(.7); opacity: 0; }
  18%, 64% { transform: translateX(0) scaleX(1); opacity: 1; }
  74% { transform: translateX(18%) scaleX(1.08); opacity: .86; }
}
@keyframes ssc-slab-two {
  0%, 14%, 100% { transform: translate3d(160px, 90px, 0) rotate(22deg) scale(.72); opacity: 0; }
  30%, 68% { transform: translate3d(0, 0, 0) rotate(10deg) scale(1); opacity: 1; }
}
@keyframes ssc-target-lock {
  0%, 32%, 100% { transform: scale(.52); opacity: 0; box-shadow: 0 0 0 0 rgba(255,255,255,0); }
  44% { transform: scale(1.12); opacity: 1; box-shadow: 0 0 0 28px rgba(255,255,255,.28); }
  56%, 74% { transform: scale(1); opacity: 1; box-shadow: 0 0 0 10px rgba(255,255,255,.10); }
}
@media (prefers-reduced-motion: reduce) {
  .ssc-cover *,
  .ssc-cover *::before,
  .ssc-cover *::after {
    animation-duration: 1ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 1ms !important;
  }
}
""".strip()
    return f"{css}\n{motion_css if motion_enabled else motion_css}"


def render_cover_html(
    *,
    strategy: dict[str, Any],
    template_id: CoverTemplateId,
    assets: list[dict[str, Any]],
    request: dict[str, Any],
) -> tuple[str, str]:
    hook = normalize_space(strategy.get("mainHook"))
    motion = nested_dict(strategy.get("motionPlan"))
    motion_intensity = normalize_space(motion.get("intensity")) or "none"
    hook_markup = (
        kinetic_text_markup(hook)
        if motion_intensity in {"subtle", "kinetic"}
        else html_lib.escape(hook)
    )
    subhook = html_lib.escape(normalize_space(strategy.get("subHook")))
    kicker = html_lib.escape(normalize_space(strategy.get("visualCategory")) or "cover")
    aria = html_lib.escape(f"Carousel cover: {normalize_space(strategy.get('mainHook'))}", quote=True)
    css = base_css(request, motion_intensity != "none")
    body = template_body(template_id, strategy, assets)
    html_text = f"""<section
  class="ssc-cover ssc-template-{kebab_template(template_id)}"
  data-template="{template_id}"
  data-motion="{html_lib.escape(motion_intensity, quote=True)}"
  aria-label="{aria}"
>
  <div class="ssc-bg" aria-hidden="true"></div>
{motion_field_markup(motion_intensity)}
  <div class="ssc-kicker">{kicker}</div>
{body}
  <h1 class="{headline_class(strategy)}">{hook_markup}</h1>
  <p class="ssc-subhead">{subhook}</p>
</section>"""
    return html_text, css


def score_strategy_focal(strategy: dict[str, Any]) -> int:
    score = WEIGHTS["focalClarity"]
    eye_path = strategy.get("eyePath") if isinstance(strategy.get("eyePath"), list) else []
    description = " ".join(
        [
            normalize_space(nested_dict(strategy.get("focalPoint")).get("description")),
            normalize_space(nested_dict(strategy.get("signal")).get("description")),
            normalize_space(strategy.get("patternInterrupt")),
        ]
    ).lower()
    if not nested_dict(strategy.get("focalPoint")).get("description"):
        score -= 5
    if len(eye_path) > 4:
        score -= 3
    if any(word in description for word in ["multiple focal", "many focal", "compete", "crowded", "everything"]):
        score -= 7
    if normalize_space(nested_dict(strategy.get("signal")).get("type")) in {"face", "huge_text", "before_after", "pattern_break"}:
        score += 1
    return max(0, min(WEIGHTS["focalClarity"], score))


def score_mobile_readability(strategy: dict[str, Any], request: dict[str, Any]) -> int:
    hook = normalize_space(strategy.get("mainHook"))
    score = WEIGHTS["mobileReadability"]
    words = headline_word_count(hook)
    if words < 2:
        score -= 7
    if words > 7:
        score -= min(8, words - 7 + 3)
    if len(hook) > request["constraints"]["maxMainHeadlineChars"]:
        score -= 6
    if len(normalize_space(strategy.get("subHook"))) > request["constraints"]["maxSubheadlineChars"]:
        score -= 3
    if len(hook) <= 32 and 2 <= words <= 6:
        score += 1
    return max(0, min(WEIGHTS["mobileReadability"], score))


def score_value_contrast(css: str, request: dict[str, Any]) -> int:
    fg = css_var(css, "--ssc-fg") or request["brand"]["colors"]["fg"]
    bg = css_var(css, "--ssc-bg") or request["brand"]["colors"]["bg"]
    if not is_hex_color(fg) or not is_hex_color(bg):
        return 8
    ratio = contrast_ratio(fg, bg)
    if ratio >= 8:
        return 12
    if ratio >= 6:
        return 10
    if ratio >= 4.5:
        return 8
    if ratio >= 3:
        return 5
    return 2


def score_curiosity(strategy: dict[str, Any]) -> int:
    gap = normalize_space(strategy.get("curiosityGap"))
    if not gap:
        return 0
    score = 8
    if re.search(r"\b(what|why|which|how|where|when)\b", gap, flags=re.I):
        score += 2
    if 18 <= len(gap) <= 120:
        score += 2
    if any(word in gap.lower() for word in ["everything", "anything", "secret trick"]):
        score -= 3
    return max(0, min(WEIGHTS["curiosityGap"], score))


def score_human(strategy: dict[str, Any]) -> int:
    human = nested_dict(strategy.get("humanCue"))
    if not human.get("use"):
        return 7
    score = 6
    if human.get("emotion"):
        score += 2
    if human.get("gazeTarget"):
        score += 2
    if normalize_space(human.get("emotion")).lower() in {"neutral", "blank"}:
        score -= 4
    return max(0, min(WEIGHTS["humanEmotion"], score))


def score_anomaly(strategy: dict[str, Any], template_id: CoverTemplateId) -> int:
    signal_type = normalize_space(nested_dict(strategy.get("signal")).get("type"))
    category = normalize_space(strategy.get("visualCategory"))
    pattern = normalize_space(strategy.get("patternInterrupt"))
    if signal_type == "pattern_break" or template_id == "pattern_break_grid":
        return 10 if pattern else 8
    if category == "surreal" or template_id == "surreal_scale":
        return 9
    if signal_type in {"before_after", "proof_screenshot", "object"}:
        return 7 if pattern else 6
    if signal_type in {"face", "huge_text", "motion"}:
        return 6
    return 4


def score_relevance(strategy: dict[str, Any], request: dict[str, Any]) -> int:
    text = " ".join(
        [
            normalize_space(strategy.get("mainHook")),
            normalize_space(strategy.get("contentPromise")),
            normalize_space(strategy.get("audienceRelevance")),
            request["topic"],
        ]
    ).lower()
    score = 6
    for token in compact_topic(request["topic"], 3).lower().split():
        if token and token in text:
            score += 1
    if request.get("audience") and request["audience"].lower() in text:
        score += 1
    if any(generic in normalize_space(strategy.get("mainHook")).lower() for generic in ["tips and tricks", "improve your content", "success tips"]):
        score -= 5
    return max(0, min(WEIGHTS["relevance"], score))


def score_motion(strategy: dict[str, Any]) -> int:
    plan = nested_dict(strategy.get("motionPlan"))
    if not plan or normalize_space(plan.get("intensity")) == "none":
        return 6
    actions = []
    for beat in [*list(plan.get("timeline") or []), *list(plan.get("loop") or [])]:
        if isinstance(beat, dict):
            actions.append(normalize_space(beat.get("action")))
    useful = [action for action in actions if action in MOTION_ACTIONS]
    decorative = [action for action in actions if action in DECORATIVE_MOTION_ACTIONS]
    if useful:
        return 8 if len(useful) >= 2 else 7
    if decorative:
        return 2
    return 4


def score_brand_fit(request: dict[str, Any]) -> int:
    colors = request["brand"]["colors"]
    forbidden = {normalize_hex(color, "") for color in request["brand"].get("forbiddenColors", []) if is_hex_color(color)}
    used = {normalize_hex(colors.get("bg", ""), ""), normalize_hex(colors.get("fg", ""), ""), normalize_hex(colors.get("accent", ""), "")}
    if forbidden and used.intersection(forbidden):
        return 1
    return 4 if colors.get("accent") else 3


def score_accessibility(html_text: str, css: str, strategy: dict[str, Any], assets: list[dict[str, Any]]) -> int:
    score = 0
    if "aria-label=" in html_text and "<h1" in html_text:
        score += 1
    if "<script" not in html_text.lower():
        score += 1
    if not any(asset.get("url") for asset in assets) or re.search(r"<img\b[^>]*\balt=", html_text):
        score += 1
    if "prefers-reduced-motion" in css:
        score += 1
    return min(WEIGHTS["accessibility"], score)


def attention_score(
    *,
    html_text: str,
    css: str,
    strategy: dict[str, Any],
    template_id: CoverTemplateId,
    assets: list[dict[str, Any]],
    request: dict[str, Any],
) -> dict[str, Any]:
    dimensions = {
        "focalClarity": score_strategy_focal(strategy),
        "mobileReadability": score_mobile_readability(strategy, request),
        "valueContrast": score_value_contrast(css, request),
        "curiosityGap": score_curiosity(strategy),
        "humanEmotion": score_human(strategy),
        "anomaly": score_anomaly(strategy, template_id),
        "relevance": score_relevance(strategy, request),
        "motionUsefulness": score_motion(strategy),
        "brandFit": score_brand_fit(request),
        "accessibility": score_accessibility(html_text, css, strategy, assets),
    }
    total = int(sum(dimensions.values()))
    reasons = [
        f"Signal: {normalize_space(nested_dict(strategy.get('signal')).get('description'))}",
        f"Stakes: {normalize_space(nested_dict(strategy.get('stakes')).get('description'))}",
        f"Gap: {normalize_space(strategy.get('curiosityGap'))}",
        f"Path: {' -> '.join(str(item) for item in strategy.get('eyePath', []))}",
    ]
    required_fixes: list[str] = []
    if dimensions["mobileReadability"] < 10:
        required_fixes.append("Shorten the main hook or enlarge the headline region.")
    if dimensions["valueContrast"] < 8:
        required_fixes.append("Increase foreground/background value contrast.")
    if dimensions["focalClarity"] < 10:
        required_fixes.append("Remove competing elements and enlarge the dominant signal.")
    if dimensions["curiosityGap"] < 8:
        required_fixes.append("Rewrite the hook around one specific unresolved question.")
    if dimensions["motionUsefulness"] < 5 and nested_dict(strategy.get("motionPlan")).get("intensity") != "none":
        required_fixes.append("Replace decorative motion with reveal, approach, point, interrupt, or transform motion.")
    score = {"total": total, "dimensions": dimensions, "reasons": reasons}
    if required_fixes:
        score["requiredFixes"] = required_fixes
    return score


def export_hints(request: dict[str, Any], strategy: dict[str, Any]) -> dict[str, Any]:
    motion = nested_dict(strategy.get("motionPlan"))
    motion_supported = bool(motion and normalize_space(motion.get("intensity")) != "none")
    return {
        "width": request["format"]["width"],
        "height": request["format"]["height"],
        "staticImageSupported": True,
        "motionSupported": motion_supported,
        "motionExportSupported": False,
        "recommendedExport": "html" if motion_supported else "png",
    }


def motion_pattern_for(template_id: CoverTemplateId) -> dict[str, str]:
    return dict(MOTION_PATTERN_BY_TEMPLATE.get(template_id, {}))


def variant_id(index: int, template_id: str, strategy: dict[str, Any]) -> str:
    digest = hashlib.sha1(f"{index}\n{template_id}\n{strategy.get('mainHook')}".encode("utf-8")).hexdigest()[:8]
    return f"ssc_{index + 1}_{digest}"


def review_status(score: dict[str, Any]) -> str:
    total = int(score.get("total") or 0)
    if total >= 80:
        return "ready"
    if total >= 70:
        return "usable"
    return "needs_revision"


def rank_and_trim(variants: list[dict[str, Any]], requested_count: int) -> list[dict[str, Any]]:
    ranked = sorted(variants, key=lambda item: (-int(item["score"]["total"]), item["title"]))
    viable = [variant for variant in ranked if int(variant["score"]["total"]) >= 70]
    selected = viable[:requested_count]
    if len(selected) < min(3, requested_count):
        for variant in ranked:
            if variant not in selected:
                selected.append(variant)
            if len(selected) >= min(3, requested_count):
                break
    return selected[:requested_count]


def build_debug_payload(strategies: list[dict[str, Any]], variants: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "strategyPrompt": "local deterministic scroll-stopper strategy generator",
        "selectedRecipes": [variant["templateId"] for variant in variants],
        "rejectedReasons": [
            f"{variant['title']}: score {variant['score']['total']}"
            for variant in variants
            if int(variant["score"]["total"]) < 70
        ],
    }


def cover_event(event: str, variant: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    payload = {"event": event}
    if variant:
        payload.update(
            {
                "templateId": variant.get("templateId"),
                "scoreTotal": nested_dict(variant.get("score")).get("total"),
                "scoreDimensions": nested_dict(nested_dict(variant.get("score")).get("dimensions")),
                "motionIntensity": nested_dict(variant.get("strategy")).get("motionPlan", {}).get("intensity")
                if isinstance(nested_dict(variant.get("strategy")).get("motionPlan"), dict)
                else "none",
                "usedGeneratedImage": any(asset.get("kind") == "generated_image" and asset.get("url") for asset in variant.get("assets", [])),
            }
        )
    payload.update(extra)
    return payload


def generate_scroll_stopper_cover(
    request: dict[str, Any],
    *,
    channel_id: str | None = None,
    generate_images: bool = False,
    image_model: str | None = None,
    out_dir: Path | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    start = time.monotonic()
    normalized = normalize_request(request, channel_id=channel_id)
    strategies = generate_attention_strategies(normalized)
    candidates: list[dict[str, Any]] = []
    image_latency_ms = 0
    for index, strategy in enumerate(strategies):
        template_id = choose_template(strategy, normalized)
        planned_assets = plan_assets(strategy, template_id, normalized)
        assets, latency = maybe_generate_assets(
            planned_assets,
            out_dir=out_dir,
            generate_images=generate_images,
            image_model=image_model,
        )
        image_latency_ms += latency
        html_text, css = render_cover_html(
            strategy=strategy,
            template_id=template_id,
            assets=assets,
            request=normalized,
        )
        score = attention_score(
            html_text=html_text,
            css=css,
            strategy=strategy,
            template_id=template_id,
            assets=assets,
            request=normalized,
        )
        candidates.append(
            {
                "id": variant_id(index, template_id, strategy),
                "templateId": template_id,
                "title": normalize_space(strategy.get("mainHook")),
                "html": html_text,
                "css": css,
                "assets": assets,
                "strategy": strategy,
                "motionPattern": motion_pattern_for(template_id),
                "score": score,
                "reviewStatus": review_status(score),
                "exportHints": export_hints(normalized, strategy),
            }
        )
    variants = rank_and_trim(candidates, normalized["constraints"]["numberOfVariants"])
    if variants and all(int(variant["score"]["total"]) < 80 for variant in variants):
        for variant in variants:
            variant["reviewStatus"] = "needs_revision"
            variant["score"].setdefault("requiredFixes", ["Revise the cover until at least one variant scores 80+."])
    latency_ms = int((time.monotonic() - start) * 1000)
    response: dict[str, Any] = {
        "variants": variants,
        "recommendedVariantId": variants[0]["id"] if variants else "",
        "telemetry": [
            cover_event(
                "cover_generate_completed",
                variants[0] if variants else None,
                generationLatencyMs=latency_ms,
                imageGenerationLatencyMs=image_latency_ms,
            )
        ],
    }
    if debug:
        response["debug"] = build_debug_payload(strategies, variants)
    return response


def preview_document(variant: dict[str, Any]) -> str:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_lib.escape(normalize_space(variant.get('title')) or 'Scroll Stopper Cover')}</title>
  <style>
html, body {{
  margin: 0;
  min-height: 100%;
  background: #555;
}}
.ssc-preview-root {{
  min-height: 100vh;
  display: grid;
  place-items: center;
}}
{variant["css"]}
  </style>
</head>
<body>
  <main class="ssc-preview-root">
{variant["html"]}
  </main>
</body>
</html>"""


def write_response_artifacts(response: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "scroll_stopper_covers.json"
    manifest_path.write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for index, variant in enumerate(response.get("variants", []), start=1):
        html_path = out_dir / f"variant_{index:02d}_{slugify(variant.get('title', 'cover'))}.html"
        html_path.write_text(preview_document(variant), encoding="utf-8")
        variant.setdefault("previewPath", str(html_path))
    manifest_path.write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def render_variant_png(html_path: Path, out_path: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit("playwright is required for --export-png") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": SLIDE_W, "height": SLIDE_H}, device_scale_factor=1)
        page.goto(html_path.resolve().as_uri())
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1600)
        page.locator(".ssc-cover").screenshot(path=str(out_path))
        browser.close()


def export_pngs(response: dict[str, Any], out_dir: Path) -> None:
    for index, variant in enumerate(response.get("variants", []), start=1):
        html_path = Path(string_value(variant.get("previewPath")))
        if not html_path.exists():
            html_path = out_dir / f"variant_{index:02d}_{slugify(variant.get('title', 'cover'))}.html"
        render_variant_png(html_path, html_path.with_suffix(".png"))


def request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    slide_outline = [line.strip() for line in args.slide_outline.split("|") if line.strip()] if args.slide_outline else []
    return {
        "topic": args.topic,
        "audience": args.audience,
        "carouselPromise": args.promise,
        "slideOutline": slide_outline,
        "creativeDirection": {
            "tone": args.tone,
            "hookType": args.hook_type,
            "visualStyle": args.visual_style,
            "motionIntensity": args.motion,
            "allowHumanFace": not args.no_human_face,
            "allowGeneratedImages": args.allow_generated_images or args.generate_images,
            "allowSurrealImages": args.allow_surreal_images,
        },
        "constraints": {"numberOfVariants": args.variants},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate scroll-stopper carousel cover variants.")
    parser.add_argument("topic", help="Carousel topic or brief.")
    parser.add_argument("--audience", help="Audience the carousel is for.")
    parser.add_argument("--promise", help="Content payoff or carousel promise.")
    parser.add_argument("--slide-outline", help="Pipe-separated slide outline, e.g. 'Mistake|Fix|Example'.")
    parser.add_argument("--tone", default="bold", choices=["bold", "premium", "chaotic", "editorial", "minimal", "playful", "dark", "high-trust"])
    parser.add_argument("--hook-type", default="auto", choices=["mistake", "warning", "secret", "contradiction", "transformation", "proof", "comparison", "identity", "story", "auto"])
    parser.add_argument("--visual-style", default="auto", choices=["thumbnail", "editorial-poster", "scrapbook", "brutalist", "clean-framework", "surreal", "auto"])
    parser.add_argument("--motion", default="none", choices=["none", "subtle", "kinetic"])
    parser.add_argument("--no-human-face", action="store_true", help="Do not generate face-led strategies.")
    parser.add_argument("--allow-generated-images", action="store_true", help="Include generated image asset plans/prompts.")
    parser.add_argument("--allow-surreal-images", action="store_true", help="Allow surreal image strategy/template selection.")
    parser.add_argument("--generate-images", action="store_true", help="Actually call OpenAI image generation for planned assets.")
    parser.add_argument("--image-model", help="OpenAI image model override.")
    parser.add_argument("--variants", type=int, default=4, help="Number of final variants, clamped to 3-6.")
    parser.add_argument("--channel", default=os.environ.get("CAROUSEL_CHANNEL"), help="Channel id for brand tokens.")
    parser.add_argument("--out-dir", type=Path, help="Output directory for JSON and preview HTML.")
    parser.add_argument("--export-png", action="store_true", help="Render each preview HTML to PNG with Playwright.")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    request = request_from_args(args)
    out_dir = args.out_dir or OUT / "scroll_stopper_cover" / slugify(args.topic)
    response = generate_scroll_stopper_cover(
        request,
        channel_id=args.channel,
        generate_images=args.generate_images,
        image_model=args.image_model,
        out_dir=out_dir,
        debug=args.debug,
    )
    manifest_path = write_response_artifacts(response, out_dir)
    if args.export_png:
        export_pngs(response, out_dir)
    print(f"Wrote {manifest_path}")
    print(f"Recommended variant: {response.get('recommendedVariantId')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
