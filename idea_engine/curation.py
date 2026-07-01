#!/usr/bin/env python3
"""Generate carousel-ready curation JSON.

Each selected idea becomes one carousel:

cover_page -> item_1 -> item_2 -> ... -> cta

This module is intentionally Python because the carousel builders in this repo
are Python. The older TypeScript sidecar can still exist while we migrate, but
the supported CLI path imports this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_article_carousel import clean_article_text
from build_x_carousel import (
    extract_gemini_text,
    gemini_api_key,
    gemini_generate_content,
    gemini_text_model,
    load_env_file,
    parse_json_object,
    string_value,
)
from channel import Channel, load_channel

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "idea_engine" / "data"
CONFIG_DIR = ROOT / "idea_engine" / "config"
PROMPT_DIR = ROOT / "idea_engine" / "prompts"

LENS_CHANNEL: dict[str, str] = {
    "jp_business": "aibrief_jp",
    "ph_builder": "vibecodersph",
}

SOURCE_HANDLE: dict[str, str] = {
    "jp_business": "@aibrief.jp",
    "ph_builder": "@vibecodersph",
}

SOURCE_NAME: dict[str, str] = {
    "jp_business": "AIブリーフ",
    "ph_builder": "VibeCoders PH",
}

KNOWN_ITEM_SOURCES: dict[str, dict[str, str]] = {
    "cursor": {
        "title": "Cursor Security",
        "url": "https://cursor.com/security",
        "claim": "Cursor documents Privacy Mode, team/admin enablement, and training-data protections.",
    },
    "gemini flash": {
        "title": "Gemini API Model Documentation",
        "url": "https://ai.google.dev/gemini-api/docs/models",
        "claim": "Google documents Gemini Flash models for speed-optimized Gemini API use cases.",
    },
    "qwen coder": {
        "title": "Qwen Coder",
        "url": "https://qwenlm.github.io/",
        "claim": "Qwen documents its coder-focused model family and open model releases.",
    },
    "claude sonnet": {
        "title": "Anthropic Model Documentation",
        "url": "https://docs.anthropic.com/en/docs/about-claude/models/overview",
        "claim": "Anthropic documents Claude Sonnet models, capabilities, and model availability.",
    },
    "litellm": {
        "title": "LiteLLM GitHub Repository",
        "url": "https://github.com/BerriAI/litellm",
        "claim": "LiteLLM documents its proxy server and OpenAI-compatible API gateway.",
    },
    "vllm": {
        "title": "vLLM GitHub Repository",
        "url": "https://github.com/vllm-project/vllm",
        "claim": "vLLM documents its high-throughput LLM serving engine.",
    },
    "langgraph": {
        "title": "LangGraph GitHub Repository",
        "url": "https://github.com/langchain-ai/langgraph",
        "claim": "LangGraph documents its framework for building stateful, multi-actor agents.",
    },
    "openrouter": {
        "title": "OpenRouter",
        "url": "https://openrouter.ai/",
        "claim": "OpenRouter documents its model routing API and provider marketplace.",
    },
    "fireworks ai": {
        "title": "Fireworks AI",
        "url": "https://fireworks.ai/",
        "claim": "Fireworks AI documents its inference and model-serving platform.",
    },
    "together ai": {
        "title": "Together AI",
        "url": "https://www.together.ai/",
        "claim": "Together AI documents its model inference platform.",
    },
    "ollama": {
        "title": "Ollama GitHub Repository",
        "url": "https://github.com/ollama/ollama",
        "claim": "Ollama documents local model running and model management.",
    },
    "n8n": {
        "title": "n8n GitHub Repository",
        "url": "https://github.com/n8n-io/n8n",
        "claim": "n8n documents workflow automation with self-hostable and cloud options.",
    },
    "hugging face datasets": {
        "title": "Hugging Face Datasets Documentation",
        "url": "https://huggingface.co/docs/datasets",
        "claim": "Hugging Face documents its Datasets library for loading and sharing datasets.",
    },
    "swe-bench": {
        "title": "SWE-bench",
        "url": "https://www.swebench.com/",
        "claim": "SWE-bench documents benchmark tasks for evaluating software engineering agents.",
    },
    "practical deep learning": {
        "title": "Practical Deep Learning for Coders",
        "url": "https://course.fast.ai/",
        "claim": "fast.ai documents its practical deep learning course for builders.",
    },
    "supabase plus modal": {
        "title": "Supabase Documentation",
        "url": "https://supabase.com/docs",
        "claim": "Supabase documents backend primitives commonly paired with serverless compute stacks.",
    },
}

CATEGORY_LABELS: dict[str, dict[str, str]] = {
    "jp_business": {
        "models": "AIモデル",
        "apis": "AI API",
        "oss": "OSS",
        "frameworks": "AIフレームワーク",
        "tools": "AIツール",
        "datasets": "データセット",
        "papers": "AI論文",
        "techniques": "実務テクニック",
        "people": "注目人物",
        "courses": "学習コース",
        "roles": "AI人材ロール",
        "mistakes": "導入ミス",
        "jargon": "AI用語",
        "stacks": "AI開発スタック",
        "features": "機能",
        "benchmarks": "ベンチマーク",
    },
    "ph_builder": {
        "models": "AI models",
        "apis": "AI APIs",
        "oss": "open-source tools",
        "frameworks": "AI frameworks",
        "tools": "AI tools",
        "datasets": "datasets",
        "papers": "AI papers",
        "techniques": "build techniques",
        "people": "people to follow",
        "courses": "courses",
        "roles": "AI roles",
        "mistakes": "build mistakes",
        "jargon": "AI jargon",
        "stacks": "AI stacks",
        "features": "features",
        "benchmarks": "benchmarks",
    },
}

AXIS_LABELS: dict[str, dict[str, str]] = {
    "jp_business": {
        "cheapest": "コストで選ぶ",
        "fastest": "速さで選ぶ",
        "best_for_task": "用途別に選ぶ",
        "easiest": "導入しやすさで選ぶ",
        "most_overlooked": "見落とされやすさで選ぶ",
        "most_overrated": "過大評価を避けて選ぶ",
        "enterprise_safe": "企業導入の安全度で選ぶ",
        "free_tier": "無料枠で試す",
        "self_hostable": "自社運用しやすさで選ぶ",
        "most_hireable": "採用市場で効く順に選ぶ",
        "best_japanese": "日本語業務で選ぶ",
        "highest_roi": "費用対効果で選ぶ",
        "about_to_blow_up": "次に伸びる順に選ぶ",
    },
    "ph_builder": {
        "cheapest": "ranked by pesos saved",
        "fastest": "ranked by shipping speed",
        "best_for_task": "ranked by actual use case",
        "easiest": "ranked by easiest to ship",
        "most_overlooked": "ranked by underrated value",
        "most_overrated": "ranked para iwas hype",
        "enterprise_safe": "ranked by client-safe choices",
        "free_tier": "ranked by free tier value",
        "self_hostable": "ranked by self-hostable upside",
        "most_hireable": "ranked by remote-work signal",
        "best_japanese": "ranked for Japanese-market work",
        "highest_roi": "ranked by ROI for small teams",
        "about_to_blow_up": "ranked before everyone notices",
    },
}

TWIST_LABELS: dict[str, dict[str, str]] = {
    "jp_business": {
        "own_money_tested": "自腹で試すならこの順",
        "the_one_everyone_ignores": "見落とされがちな本命から",
        "overseas_arbitrage": "海外では定番、日本ではまだ早い順",
        "stop_using_x": "惰性で選ばないための候補",
        "ranked_so_you_dont_have_to": "調べる時間を買うランキング",
        "unusual_criterion": "稟議で説明しやすい順",
    },
    "ph_builder": {
        "own_money_tested": "tested like sariling budget ang gamit",
        "the_one_everyone_ignores": "starting sa underrated picks",
        "overseas_arbitrage": "global builder habits na hindi pa mainstream dito",
        "stop_using_x": "stop defaulting sa obvious choice",
        "ranked_so_you_dont_have_to": "ranked para hindi ka maubusan ng oras",
        "unusual_criterion": "ranked by weird but useful criteria",
    },
}

QUESTION_CATEGORY_HINTS = ["tools", "apis", "stacks", "techniques", "models"]
HOOK_MAX_EN_WORDS = 14
HOOK_MAX_JA_CHARS = 25
ITEM_MAX_LINES = 2

HOOK_AXIS_LABELS: dict[str, dict[str, str]] = {
    "jp_business": {
        "cheapest": "低コスト",
        "fastest": "高速",
        "best_for_task": "用途別",
        "easiest": "導入しやすい",
        "most_overlooked": "見落としがち",
        "most_overrated": "過大評価注意",
        "enterprise_safe": "企業向け",
        "free_tier": "無料枠",
        "self_hostable": "自社運用",
        "most_hireable": "採用で効く",
        "best_japanese": "日本語業務向け",
        "highest_roi": "ROI重視",
        "about_to_blow_up": "次に伸びる",
    },
    "ph_builder": {
        "cheapest": "budget",
        "fastest": "fast",
        "best_for_task": "use-case",
        "easiest": "easy",
        "most_overlooked": "overlooked",
        "most_overrated": "hype-check",
        "enterprise_safe": "client-safe",
        "free_tier": "free-tier",
        "self_hostable": "self-hosted",
        "most_hireable": "hireable",
        "best_japanese": "Japan-ready",
        "highest_roi": "ROI",
        "about_to_blow_up": "next-wave",
    },
}

HOOK_TWIST_LABELS: dict[str, dict[str, str]] = {
    "jp_business": {
        "own_money_tested": "自腹で試す",
        "the_one_everyone_ignores": "見落としがち",
        "overseas_arbitrage": "海外定番",
        "stop_using_x": "惰性回避",
        "ranked_so_you_dont_have_to": "調査済み",
        "unusual_criterion": "稟議向け",
    },
    "ph_builder": {
        "own_money_tested": "worth paying for",
        "the_one_everyone_ignores": "people ignore",
        "overseas_arbitrage": "global builders use",
        "stop_using_x": "before defaulting",
        "ranked_so_you_dont_have_to": "ranked for you",
        "unusual_criterion": "with weird criteria",
    },
}


@dataclass(frozen=True)
class Combination:
    set_category: str
    axis: str
    lens: str
    twist: str
    weight: float = 1.0

    def as_json(self) -> dict[str, object]:
        return {
            "setCategory": self.set_category,
            "axis": self.axis,
            "lens": self.lens,
            "twist": self.twist,
            "weight": self.weight,
        }


@dataclass
class Candidate:
    id: str
    title: str
    angle: str
    items: list[str]
    combination: Combination
    scores: dict[str, object]
    created_at: str
    provider: str
    question: dict[str, object] | None = None
    source_story: dict[str, object] | None = None

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "title": self.title,
            "hook": hook_metadata(self.title, self.combination.lens),
            "angle": self.angle,
            "items": self.items,
            "combination": self.combination.as_json(),
            "scores": self.scores,
            "createdAt": self.created_at,
            "provider": self.provider,
        }
        if self.question:
            payload["question"] = self.question
        if self.source_story:
            payload["source_story"] = self.source_story
        return payload


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def compact_text(value: str, limit: int = 520) -> str:
    value = normalize_space(value)
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0].strip() or value[:limit].strip()


def hook_limit(lens: str) -> int:
    return HOOK_MAX_JA_CHARS if lens == "jp_business" else HOOK_MAX_EN_WORDS


def hook_unit(lens: str) -> str:
    return "characters" if lens == "jp_business" else "words"


def hook_length(value: object, lens: str) -> int:
    text = normalize_space(value)
    if lens == "jp_business":
        return len(re.sub(r"\s+", "", text))
    return len(re.findall(r"\S+", text))


def compact_japanese_hook(text: str, limit: int = HOOK_MAX_JA_CHARS) -> str:
    text = normalize_space(text)
    if hook_length(text, "jp_business") <= limit:
        return text
    for separator in ("：", ":", "。", "、", "｜", "|"):
        head = text.split(separator, 1)[0].strip()
        if head and hook_length(head, "jp_business") <= limit:
            return head
    chars: list[str] = []
    visible_count = 0
    last_was_space = False
    for char in text:
        if char.isspace():
            if chars and not last_was_space:
                chars.append(" ")
                last_was_space = True
            continue
        if visible_count >= limit:
            break
        chars.append(char)
        visible_count += 1
        last_was_space = False
    return "".join(chars).rstrip(" ,;:：、。")


def compact_english_hook(text: str, limit: int = HOOK_MAX_EN_WORDS) -> str:
    text = normalize_space(text)
    words = re.findall(r"\S+", text)
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip(" ,;:：、。")


def compact_hook(text: object, lens: str) -> str:
    text = normalize_space(text)
    if lens == "jp_business":
        return compact_japanese_hook(text)
    return compact_english_hook(text)


def hook_item_count(items: list[str]) -> int:
    return max(1, len([item for item in items if normalize_space(item)]))


def numbered_local_hooks(combination: Combination, names: list[str]) -> list[str]:
    count = hook_item_count(names)
    category = CATEGORY_LABELS[combination.lens][combination.set_category]
    axis = HOOK_AXIS_LABELS[combination.lens].get(combination.axis, combination.axis.replace("_", " "))
    twist = HOOK_TWIST_LABELS[combination.lens].get(combination.twist, combination.twist.replace("_", " "))
    lead_item = names[0] if names else category
    if count == 1:
        if combination.lens == "jp_business":
            return [
                f"{category}の3確認点",
                f"3つの{axis}{category}",
                f"{category}を試す3理由",
            ]
        return [
            f"3 checks before {lead_item}",
            f"3 reasons to test {lead_item}",
            f"3 deal-breakers for {lead_item}",
        ]
    if combination.lens == "jp_business":
        return [
            f"{count}つの{axis}{category}",
            f"{count}つの{twist}{category}",
            f"{count}つの{lead_item}系候補",
        ]
    return [
        f"{count} {category} for {axis}",
        f"{count} {category} {twist}",
        f"{count} {lead_item}-style picks",
    ]


def ensure_numbered_hook(title: object, lens: str, items: list[str]) -> str:
    title_text = normalize_space(title)
    if re.search(r"\d", title_text):
        return compact_hook(title_text, lens)
    count = hook_item_count(items)
    if count == 1:
        if lens == "jp_business":
            return compact_hook(f"{title_text}の3確認点", lens)
        return compact_hook(f"3 checks: {title_text}", lens)
    if lens == "jp_business":
        return compact_hook(f"{count}つの{title_text}", lens)
    return compact_hook(f"{count} {title_text}", lens)


def hook_metadata(title: str, lens: str) -> dict[str, object]:
    return {
        "text": title,
        "length": hook_length(title, lens),
        "max": hook_limit(lens),
        "unit": hook_unit(lens),
    }


def hook_validation_error(value: object, lens: str, field: str) -> str:
    return (
        f"{field} exceeds {hook_limit(lens)} {hook_unit(lens)} "
        f"({hook_length(value, lens)})"
    )


def normalized_hook_key(value: object) -> str:
    return re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u9fff]+", "", normalize_space(value).lower())


def visible_char_count(value: object) -> int:
    return len(re.sub(r"\s+", "", normalize_space(value)))


def is_long_topic(item_name: str) -> bool:
    return visible_char_count(item_name) >= 18 or hook_length(item_name, "ph_builder") >= 3


def item_line_capacity(lens: str, item_name: str, field: str) -> int:
    long_topic = is_long_topic(item_name)
    if lens == "jp_business":
        capacities = {
            "headline": 10 if long_topic else 12,
            "body": 22 if long_topic else 26,
            "takeaway": 18 if long_topic else 22,
            "best_for": 18 if long_topic else 22,
            "watch_out": 18 if long_topic else 22,
        }
        return capacities.get(field, 22)
    capacities = {
        "headline": 4 if long_topic else 5,
        "body": 9 if long_topic else 11,
        "takeaway": 8 if long_topic else 10,
        "best_for": 8 if long_topic else 10,
        "watch_out": 8 if long_topic else 10,
    }
    return capacities.get(field, 10)


def estimated_item_lines(text: object, lens: str, item_name: str, field: str) -> int:
    text = normalize_space(text)
    if not text:
        return 0
    capacity = max(1, item_line_capacity(lens, item_name, field))
    if lens == "jp_business":
        return max(1, (visible_char_count(text) + capacity - 1) // capacity)
    return max(1, (hook_length(text, "ph_builder") + capacity - 1) // capacity)


def compact_japanese_chars(text: str, limit: int) -> str:
    text = normalize_space(text)
    if visible_char_count(text) <= limit:
        return text
    chars: list[str] = []
    visible_count = 0
    last_was_space = False
    for char in text:
        if char.isspace():
            if chars and not last_was_space:
                chars.append(" ")
                last_was_space = True
            continue
        if visible_count >= limit:
            break
        chars.append(char)
        visible_count += 1
        last_was_space = False
    return "".join(chars).rstrip(" ,;:：、。")


def compact_item_lines(text: object, lens: str, item_name: str, field: str) -> str:
    text = normalize_space(text)
    if not text:
        return text
    capacity = item_line_capacity(lens, item_name, field) * ITEM_MAX_LINES
    if lens == "jp_business":
        return compact_japanese_chars(text, capacity)
    return compact_english_hook(text, capacity)


def item_headline(item_name: str, lens: str) -> str:
    if lens == "jp_business":
        return "導入前の要点" if is_long_topic(item_name) else f"{item_name}の要点"
    return "Worth testing?" if is_long_topic(item_name) else f"{item_name}: quick verdict"


def source_display(source: dict[str, str]) -> str:
    title = normalize_space(source.get("title"))
    url = normalize_space(source.get("url"))
    if title and url:
        return f"{title}: {url}"
    return title or url


def compact_caption(text: str, limit: int = 2100) -> str:
    text = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit("\n", 1)[0].strip()
    if len(clipped) < limit * 0.65:
        clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return clipped or text[:limit].strip()


def page_sources_text(page: dict[str, object]) -> list[str]:
    sources = page.get("sources") if isinstance(page.get("sources"), list) else []
    lines: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        line = source_display({
            "title": normalize_space(source.get("title")),
            "url": normalize_space(source.get("url")),
            "claim": normalize_space(source.get("claim")),
        })
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


def item_alt_text(page: dict[str, object], lens: str) -> str:
    item_name = normalize_space(page.get("item_name"))
    headline = normalize_space(page.get("headline"))
    body = normalize_space(page.get("body"))
    takeaway = normalize_space(page.get("takeaway"))
    if lens == "jp_business":
        return compact_text(
            f"{item_name}の解説スライド。見出しは「{headline}」。本文は「{body}」。結論は「{takeaway}」。",
            260,
        )
    body = body.rstrip(". ")
    takeaway = takeaway.rstrip(". ")
    return compact_text(
        f"Item slide about {item_name}. Headline: {headline}. Summary: {body}. Takeaway: {takeaway}.",
        260,
    )


def cover_alt_text(cover: dict[str, object], candidate: Candidate, lens: str) -> str:
    headline = normalize_space(cover.get("headline")) or candidate.title
    item_text = ", ".join(candidate.items[:4])
    if lens == "jp_business":
        return compact_text(f"キュレーション表紙スライド。フックは「{headline}」。対象項目は{item_text}。", 240)
    return compact_text(f"Cover slide for a curation carousel. Hook: {headline}. Items covered: {item_text}.", 240)


def cta_alt_text(cta: dict[str, object], lens: str) -> str:
    headline = normalize_space(cta.get("headline"))
    body = normalize_space(cta.get("body"))
    action = normalize_space(cta.get("action"))
    if lens == "jp_business":
        return compact_text(f"CTAスライド。「{headline}」。{body} アクションは{action}。", 220)
    body = body.rstrip(". ")
    return compact_text(f"CTA slide. Headline: {headline}. Body: {body}. Action: {action}.", 220)


def build_instagram_caption(
    candidate: Candidate,
    lens: str,
    item_pages: list[dict[str, object]],
    provider: str,
    grounding: dict[str, object],
) -> str:
    used_search = bool(grounding.get("used_google_search"))
    if lens == "jp_business":
        lines = [
            f"{candidate.title}",
            "",
            "調査メモ: フックを編集仮説にして、候補アイテムを固定したまま比較しました。",
            (
                "調査方法: Gemini + Google Search groundingで公式情報を優先。"
                if used_search
                else "調査方法: ローカルのシード評価と既知の公式/公開ソースURLを使用。API検索は未使用。"
            ),
        ]
        for page in item_pages:
            name = normalize_space(page.get("item_name"))
            best_for = normalize_space(page.get("best_for"))
            watch_out = normalize_space(page.get("watch_out"))
            proof = normalize_space((page.get("proof_points") or [""])[0] if isinstance(page.get("proof_points"), list) else "")
            lines.append(f"- {name}: {best_for} 注意点: {watch_out} 根拠: {proof}")
        source_lines = [line for page in item_pages for line in page_sources_text(page)]
        if source_lines:
            lines.extend(["", "Sources:", *[f"- {line}" for line in source_lines[:8]]])
        lines.extend(["", "保存して次の比較で見返してください。 #AI #生成AI #AI導入"])
        return compact_caption("\n".join(lines), 2100)

    lines = [
        candidate.title,
        "",
        "Research notes: I treated the hook as the editorial hypothesis, then kept the item list fixed while checking each pick.",
        (
            "Method: Gemini with Google Search grounding, prioritizing official docs and public primary sources."
            if used_search
            else "Method: local seed scoring plus known official/public source URLs. No live search/API call was used for this run."
        ),
    ]
    for page in item_pages:
        name = normalize_space(page.get("item_name"))
        best_for = normalize_space(page.get("best_for"))
        watch_out = normalize_space(page.get("watch_out"))
        proof = normalize_space((page.get("proof_points") or [""])[0] if isinstance(page.get("proof_points"), list) else "")
        lines.append(f"- {name}: {best_for} Watch-out: {watch_out} Proof: {proof}")
    source_lines = [line for page in item_pages for line in page_sources_text(page)]
    if source_lines:
        lines.extend(["", "Sources:", *[f"- {line}" for line in source_lines[:8]]])
    lines.extend(["", "Save this before choosing your stack. #AItools #buildinpublic #vibecoding"])
    return compact_caption("\n".join(lines), 2100)


def stable_id(*parts: object, prefix: str = "idea") -> str:
    digest = hashlib.sha256("\n".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:14]}"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_store() -> dict[str, Any]:
    sets = read_json(DATA_DIR / "sets.json")
    questions = read_json(DATA_DIR / "questions.json")
    return {
        "items": sets.get("items", []),
        "questions": questions.get("questions", []),
    }


def load_config() -> dict[str, Any]:
    return read_json(CONFIG_DIR / "compatibility.json")


def item_index(store: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in store.get("items", []):
        if isinstance(item, dict):
            name = normalize_space(item.get("name"))
            if name:
                out[name.lower()] = item
    return out


def attr_number(item: dict[str, Any], key: str, fallback: int = 3) -> float:
    attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
    value = attrs.get(key)
    return float(value) if isinstance(value, (int, float)) else fallback


def axis_score(item: dict[str, Any], axis: str) -> float:
    attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
    if axis == "cheapest":
        return 6 - attr_number(item, "costRank")
    if axis == "fastest":
        return attr_number(item, "latencyRank")
    if axis == "easiest":
        return attr_number(item, "easiest")
    if axis == "enterprise_safe":
        return attr_number(item, "enterpriseSafe")
    if axis == "free_tier":
        return 5 if attrs.get("freeTier") else 2
    if axis == "self_hostable":
        return 5 if attrs.get("selfHostable") else 2
    if axis == "most_hireable":
        return attr_number(item, "hireSignal")
    if axis == "best_japanese":
        return attr_number(item, "japaneseSupport")
    if axis == "highest_roi":
        return attr_number(item, "roi")
    if axis == "about_to_blow_up":
        return attr_number(item, "trendScore")
    if axis == "most_overlooked":
        return attr_number(item, "overlookedScore")
    if axis == "most_overrated":
        return attr_number(item, "overratedRisk")
    return attr_number(item, "roi")


def resolve_items(combination: Combination, store: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    items = [
        item
        for item in store.get("items", [])
        if isinstance(item, dict) and item.get("category") == combination.set_category
    ]
    return sorted(items, key=lambda item: (-axis_score(item, combination.axis), normalize_space(item.get("name"))))[:limit]


def enumerate_combinations(
    lens: str,
    config: dict[str, Any],
    *,
    set_category: str | None = None,
    axis: str | None = None,
    twist: str | None = None,
) -> list[Combination]:
    axis_by_category = config.get("axisByCategory") if isinstance(config.get("axisByCategory"), dict) else {}
    axis_weights = (config.get("lensAxisWeights") or {}).get(lens, {})
    twist_weights = (config.get("lensTwistWeights") or {}).get(lens, {})
    combinations: list[Combination] = []
    for category in config.get("categories", []):
        if set_category and category != set_category:
            continue
        for axis_name in config.get("axes", []):
            if axis and axis_name != axis:
                continue
            if category not in axis_by_category.get(axis_name, []):
                continue
            for twist_name in config.get("twists", []):
                if twist and twist_name != twist:
                    continue
                weight = float(axis_weights.get(axis_name, 1.0)) * float(twist_weights.get(twist_name, 1.0))
                combinations.append(Combination(category, axis_name, lens, twist_name, weight))
    return sorted(combinations, key=lambda combo: (-combo.weight, combo.set_category, combo.axis, combo.twist))


def infer_question_category(question: str) -> str:
    lower = question.lower()
    if "stack" in lower:
        return "stacks"
    if "api" in lower or "モデル" in question:
        return "apis"
    if "skill" in lower or "tools" in lower or "ツール" in question:
        return "tools"
    if "hire" in lower or "remote" in lower:
        return "roles"
    return QUESTION_CATEGORY_HINTS[0]


def infer_question_axis(question: str, lens: str) -> str:
    lower = question.lower()
    if "cheap" in lower or "budget" in lower or "安" in question:
        return "cheapest"
    if "free" in lower or "無料" in question:
        return "free_tier"
    if "remote" in lower or "hire" in lower or "採用" in question:
        return "most_hireable"
    if "社内" in question or "enterprise" in lower or "risk" in lower:
        return "enterprise_safe"
    return "enterprise_safe" if lens == "jp_business" else "highest_roi"


def question_to_combination(question_text: str, lens: str, store: dict[str, Any]) -> tuple[Combination, dict[str, object]]:
    for seed in store.get("questions", []):
        if not isinstance(seed, dict):
            continue
        if seed.get("lens") == lens and (
            seed.get("id") == question_text or normalize_space(seed.get("question")).lower() == question_text.lower()
        ):
            category = (seed.get("categoryHints") or [infer_question_category(question_text)])[0]
            axis = (seed.get("axisHints") or [infer_question_axis(question_text, lens)])[0]
            return Combination(category, axis, lens, "overseas_arbitrage" if lens == "jp_business" else "own_money_tested", 2.0), seed
    question = {
        "id": stable_id(lens, question_text, prefix="question"),
        "question": question_text,
        "lens": lens,
        "categoryHints": [infer_question_category(question_text)],
        "axisHints": [infer_question_axis(question_text, lens)],
        "tags": ["ad-hoc"],
        "source": "cli",
        "addedAt": datetime.now(timezone.utc).date().isoformat(),
    }
    return (
        Combination(
            str(question["categoryHints"][0]),
            str(question["axisHints"][0]),
            lens,
            "overseas_arbitrage" if lens == "jp_business" else "own_money_tested",
            2.0,
        ),
        question,
    )


def local_angle(combination: Combination, names: list[str], question: str | None = None) -> str:
    category = CATEGORY_LABELS[combination.lens][combination.set_category]
    axis = AXIS_LABELS[combination.lens][combination.axis]
    twist = TWIST_LABELS[combination.lens][combination.twist]
    if combination.lens == "jp_business":
        item_text = "、".join(names[:3]) if names else category
        question_text = f"問い: {question}。" if question else ""
        return normalize_space(f"{question_text}{item_text}を候補に、{axis}。{twist}なので、流行ではなく導入判断に使える。")
    item_text = ", ".join(names[:3]) if names else category
    question_text = f"Question: {question}. " if question else ""
    return normalize_space(f"{question_text}{item_text} ang anchor, then {axis}. May twist na {twist}, so hindi siya generic tool dump.")


def local_drafts(
    combination: Combination,
    items: list[dict[str, Any]],
    count: int,
    question: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    names = [normalize_space(item.get("name")) for item in items if normalize_space(item.get("name"))]
    question_text = normalize_space(question.get("question")) if question else None
    titles = numbered_local_hooks(combination, names)
    return [
        {
            "title": compact_hook(title, combination.lens),
            "angle": local_angle(combination, names, question_text),
            "items": names,
        }
        for title in titles[:count]
    ]


def prompt_text(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def gemini_json(
    prompt: str,
    *,
    use_search: bool = False,
    temperature: float = 0.25,
    timeout: int = 60,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    api_key = gemini_api_key()
    if not api_key:
        return None, {"sources": [], "web_search_queries": [], "used_google_search": False}
    payload: dict[str, object] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
    if use_search:
        payload["tools"] = [{"google_search": {}}]
    response = gemini_generate_content(
        gemini_text_model(),
        api_key,
        payload,
        api_version=os.environ.get("GEMINI_TEXT_API_VERSION") or "v1beta",
        timeout=timeout,
    )
    parsed = parse_json_object(extract_gemini_text(response))
    return parsed, extract_grounding(response, used_google_search=use_search)


def extract_grounding(response: dict[str, object] | None, *, used_google_search: bool) -> dict[str, object]:
    sources: list[dict[str, str]] = []
    queries: list[str] = []
    if isinstance(response, dict):
        candidates = response.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                metadata = candidate.get("groundingMetadata") or candidate.get("grounding_metadata")
                if not isinstance(metadata, dict):
                    continue
                raw_queries = metadata.get("webSearchQueries") or metadata.get("web_search_queries") or []
                if isinstance(raw_queries, list):
                    queries.extend(normalize_space(query) for query in raw_queries if normalize_space(query))
                chunks = metadata.get("groundingChunks") or metadata.get("grounding_chunks") or []
                if isinstance(chunks, list):
                    for chunk in chunks:
                        if not isinstance(chunk, dict):
                            continue
                        web = chunk.get("web") if isinstance(chunk.get("web"), dict) else {}
                        uri = normalize_space(web.get("uri"))
                        title = normalize_space(web.get("title"))
                        if uri:
                            sources.append({"title": title or uri, "url": uri})
    deduped_sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for source in sources:
        if source["url"] in seen_urls:
            continue
        seen_urls.add(source["url"])
        deduped_sources.append(source)
    return {
        "used_google_search": used_google_search,
        "web_search_queries": list(dict.fromkeys(queries)),
        "sources": deduped_sources,
    }


def allowed_items(items: list[dict[str, Any]]) -> dict[str, str]:
    return {
        normalize_space(item.get("name")).lower(): normalize_space(item.get("name"))
        for item in items
        if normalize_space(item.get("name"))
    }


def gemini_drafts(
    combination: Combination,
    items: list[dict[str, Any]],
    count: int,
    question: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    prompt_payload = {
        "combination": combination.as_json(),
        "lens": combination.lens,
        "items": [
            {
                "name": normalize_space(item.get("name")),
                "attrs": item.get("attrs"),
                "source": item.get("source"),
            }
            for item in items
        ],
        "question": question.get("question") if question else None,
        "count": count,
    }
    prompt = f"{prompt_text('titling')}\n\nInput JSON:\n{json.dumps(prompt_payload, ensure_ascii=False, indent=2)}"
    parsed, _grounding = gemini_json(prompt, temperature=0.35, timeout=45)
    candidates = parsed.get("candidates") if isinstance(parsed, dict) else []
    allowed = allowed_items(items)
    drafts: list[dict[str, object]] = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            title = compact_hook(candidate.get("title"), combination.lens)
            angle = compact_text(normalize_space(candidate.get("angle")), 500)
            if not title or not angle or "\u2014" in title:
                continue
            raw_items = candidate.get("items") if isinstance(candidate.get("items"), list) else []
            resolved = [
                allowed[item_name.lower()]
                for raw in raw_items
                if (item_name := normalize_space(raw)) and item_name.lower() in allowed
            ]
            drafts.append({
                "title": title,
                "angle": angle,
                "items": resolved or list(allowed.values()),
            })
            if len(drafts) >= count:
                break
    return drafts or local_drafts(combination, items, count, question)


def clamp_score(value: object) -> int:
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, number))


def score_candidate(candidate: Candidate, min_core: int = 3, min_total: int = 14) -> dict[str, object]:
    title = candidate.title.lower()
    angle = candidate.angle.lower()
    generic = any(
        re.search(pattern, title)
        for pattern in [
            r"\btop\s+\d+\s+ai\s+tools\b",
            r"\bbest\s+ai\s+tools\b",
            r"\bultimate\s+guide\b",
            r"\bwhat\s+you\s+need\s+to\s+know\b",
            r"\bai\s+tools\s+you\s+must\s+try\b",
        ]
    )
    has_items = bool(candidate.items)
    has_question = bool(candidate.question)
    has_twist = (
        candidate.combination.twist.split("_")[0] in angle
        or "twist" in angle
        or "流行ではなく" in candidate.angle
        or "generic" in angle
    )
    saveable = clamp_score(3 + int(has_items) + int(":" in candidate.title) - int(not has_twist))
    authority = clamp_score(3 + int(has_items) + int(has_question) - int(not has_twist))
    decision = clamp_score(
        3
        + int(bool(re.search(r"ranked|compare|比較|判断|導入|roi|budget|pesos|client|remote|無料|費用", f"{candidate.title} {candidate.angle}", re.I)))
        - int(not has_twist)
    )
    compounds = clamp_score(3 + int(bool(re.search(r"ranking|リスト|シリーズ|updates?|候補|毎月|weekly", f"{candidate.title} {candidate.angle}", re.I))))
    total = saveable + authority + decision + compounds
    reasons: list[str] = []
    if generic or len(candidate.angle) < 60:
        reasons.append("generic listicle or weak angle")
    if saveable < min_core or authority < min_core or decision < min_core:
        reasons.append("core filter below threshold")
    if total < min_total:
        reasons.append(f"total {total} below {min_total}")
    killed = bool(reasons)
    return {
        "saveable": saveable,
        "authority": authority,
        "decisionUseful": decision,
        "compounds": compounds,
        "total": total,
        "killed": killed,
        "reason": ", ".join(reasons) if reasons else "",
    }


def make_candidate(
    *,
    combination: Combination,
    draft: dict[str, object],
    provider: str,
    question: dict[str, object] | None = None,
    source_story: dict[str, object] | None = None,
) -> Candidate:
    angle = compact_text(normalize_space(draft.get("angle")), 500)
    items = [normalize_space(item) for item in draft.get("items", []) if normalize_space(item)] if isinstance(draft.get("items"), list) else []
    title = ensure_numbered_hook(draft.get("title"), combination.lens, items)
    candidate = Candidate(
        id=stable_id(combination.lens, combination.set_category, combination.axis, combination.twist, title),
        title=title,
        angle=angle,
        items=items,
        combination=combination,
        scores={
            "saveable": 0,
            "authority": 0,
            "decisionUseful": 0,
            "compounds": 0,
            "total": 0,
            "killed": False,
        },
        created_at=now_iso(),
        provider=provider,
        question=question,
        source_story=source_story,
    )
    candidate.scores = score_candidate(candidate)
    return candidate


def select_diverse_candidates(candidates: list[Candidate], count: int) -> list[Candidate]:
    selected: list[Candidate] = []
    selected_ids: set[str] = set()
    seen_hooks: set[str] = set()
    for candidate in candidates:
        hook_key = normalized_hook_key(candidate.title)
        if hook_key in seen_hooks:
            continue
        selected.append(candidate)
        selected_ids.add(candidate.id)
        seen_hooks.add(hook_key)
        if len(selected) >= count:
            return selected
    for candidate in candidates:
        if candidate.id in selected_ids:
            continue
        selected.append(candidate)
        if len(selected) >= count:
            break
    return selected


def generate_candidates(
    *,
    lens: str,
    count: int,
    provider: str,
    candidate_pool: int | None = None,
    candidates_per_combination: int = 3,
    set_category: str | None = None,
    axis: str | None = None,
    twist: str | None = None,
    from_question: str | None = None,
) -> tuple[list[Candidate], list[Candidate]]:
    store = load_store()
    config = load_config()
    generated: list[Candidate] = []
    if from_question:
        combination, question = question_to_combination(from_question, lens, store)
        items = resolve_items(combination, store, 5)
        drafts = gemini_drafts(combination, items, max(1, candidates_per_combination), question) if provider == "gemini" else local_drafts(combination, items, max(1, candidates_per_combination), question)
        generated.extend(make_candidate(combination=combination, draft=draft, provider=provider, question=question) for draft in drafts)
    else:
        seeded_categories = {
            item.get("category")
            for item in store.get("items", [])
            if isinstance(item, dict) and item.get("category")
        }
        combinations = [
            combo
            for combo in enumerate_combinations(lens, config, set_category=set_category, axis=axis, twist=twist)
            if combo.set_category in seeded_categories
        ][: candidate_pool or max(16, count * 4)]
        for combination in combinations:
            items = resolve_items(combination, store, 5)
            drafts = gemini_drafts(combination, items, candidates_per_combination) if provider == "gemini" else local_drafts(combination, items, candidates_per_combination)
            generated.extend(make_candidate(combination=combination, draft=draft, provider=provider) for draft in drafts)
    live = [candidate for candidate in generated if not candidate.scores.get("killed")]
    killed = [candidate for candidate in generated if candidate.scores.get("killed")]
    live.sort(key=lambda candidate: (-int(candidate.scores.get("total") or 0), candidate.title))
    selected = select_diverse_candidates(live, count)
    selected_ids = {candidate.id for candidate in selected}
    return selected, killed + [candidate for candidate in live if candidate.id not in selected_ids]


def parse_items_line(source_text: str) -> list[str]:
    match = re.search(r"^Items:\s*(.+)$", source_text, flags=re.M)
    if not match:
        return []
    return [normalize_space(part) for part in match.group(1).split(",") if normalize_space(part)]


def parse_formula(source_text: str, lens: str) -> Combination:
    match = re.search(
        r"^Formula:\s*([a-z_]+)\s+ranked by\s+([a-z_]+)\s+for\s+([a-z_]+)\s+with\s+([a-z_]+)",
        source_text,
        flags=re.M,
    )
    if match:
        return Combination(match.group(1), match.group(2), match.group(3), match.group(4), 1.0)
    return Combination("tools", "highest_roi", lens, "ranked_so_you_dont_have_to", 1.0)


def candidates_from_story_file(path: Path, lens: str, provider: str) -> list[Candidate]:
    payload = read_json(path)
    stories = payload.get("stories", payload) if isinstance(payload, dict) else payload
    if not isinstance(stories, list):
        return []
    candidates: list[Candidate] = []
    for story in stories:
        if not isinstance(story, dict):
            continue
        title = normalize_space(story.get("headline") or story.get("title"))
        angle = compact_text(normalize_space(story.get("body") or story.get("summary") or story.get("text")), 500)
        source_text = str(story.get("source_text") or "")
        if not title or not angle:
            continue
        items = story.get("items") if isinstance(story.get("items"), list) else parse_items_line(source_text)
        combination = parse_formula(source_text, lens)
        source_story = {
            "category": story.get("category"),
            "headline": title,
            "body": angle,
            "source_url": story.get("source_url"),
            "source_type": story.get("source_type"),
            "source_text": story.get("source_text"),
        }
        candidates.append(
            make_candidate(
                combination=combination,
                draft={"title": title, "angle": angle, "items": items},
                provider=provider,
                source_story=source_story,
            )
        )
    return candidates


def fallback_sources(item_name: str, item: dict[str, Any] | None = None) -> list[dict[str, str]]:
    known = KNOWN_ITEM_SOURCES.get(item_name.lower())
    if known:
        return [known]
    source = normalize_space((item or {}).get("source"))
    if source and source != "manual seed":
        return [{"title": item_name, "url": source, "claim": "Seed source for item"}]
    return [{"title": item_name, "url": "", "claim": "Manual seed item, pending web source"}]


def brand_image_prompt(prompt: str, channel: Channel) -> str:
    prompt = normalize_space(prompt)
    suffix = (
        f" Match {channel.brand_name} carousel style: cream paper background, dark ink, "
        "terracotta accent, premium editorial composition, no neon, no rainbow colors, "
        "no blue-purple gradients."
    )
    if "terracotta" in prompt.lower() and "cream" in prompt.lower():
        return prompt
    return normalize_space(f"{prompt}.{suffix}" if prompt else suffix.strip())


def attr_bool(item: dict[str, Any], key: str) -> bool:
    attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
    return bool(attrs.get(key))


def best_fit_note(item_name: str, item: dict[str, Any], lens: str) -> str:
    if lens == "jp_business":
        if attr_bool(item, "selfHostable"):
            return "自社運用やデータ管理を重視する検証向き。"
        if attr_bool(item, "freeTier"):
            return "無料枠で小さく試したい初期検証向き。"
        if attr_number(item, "enterpriseSafe", 0) >= 4:
            return "セキュリティ確認が必要な企業導入候補。"
        if attr_number(item, "japaneseSupport", 0) >= 4:
            return "日本語業務の精度を見たいチーム向き。"
        if attr_number(item, "hireSignal", 0) >= 4:
            return "採用や外注選定でスキル証明に使いやすい。"
        if attr_number(item, "roi", 0) >= 5:
            return "小さなチームで費用対効果を見たい用途向き。"
        return f"{item_name}を実務ワークフローで試す初期候補。"
    if attr_bool(item, "selfHostable"):
        return "Best for teams that need more control over hosting or data flow."
    if attr_bool(item, "freeTier"):
        return "Best for builders who want to test without upfront spend."
    if attr_number(item, "enterpriseSafe", 0) >= 4:
        return "Best for client work where security review matters."
    if attr_number(item, "hireSignal", 0) >= 4:
        return "Best for portfolio work that signals practical AI skills."
    if attr_number(item, "latencyRank", 0) >= 4:
        return "Best for prototypes where response speed matters."
    if attr_number(item, "roi", 0) >= 5:
        return "Best for small teams chasing clear payoff per build hour."
    return f"Best for testing {item_name} on one focused workflow."


def watch_out_note(item: dict[str, Any], lens: str) -> str:
    if lens == "jp_business":
        if attr_number(item, "enterpriseSafe", 0) < 4:
            return "本番前に権限、監査、データ保持を確認する。"
        if not attr_bool(item, "selfHostable"):
            return "データ所在とベンダーロックインを先に確認する。"
        if attr_number(item, "costRank", 3) >= 4:
            return "利用量が増えた時の料金を先に試算する。"
        if attr_number(item, "easiest", 5) <= 3:
            return "初回構築に検証時間を確保する。"
        return "価格、制限、既存運用との相性を小さく検証する。"
    if attr_number(item, "enterpriseSafe", 0) < 4:
        return "Do a security and data-retention check before client data."
    if not attr_bool(item, "selfHostable"):
        return "Check data residency and lock-in before making it a default."
    if attr_number(item, "costRank", 3) >= 4:
        return "Model the bill before usage spikes."
    if attr_number(item, "easiest", 5) <= 3:
        return "Expect setup time before the first clean workflow."
    return "Validate pricing, limits, and operational fit on a small pilot."


def proof_points_for_item(item_name: str, item: dict[str, Any], combination: Combination) -> list[str]:
    attrs = item.get("attrs") if isinstance(item.get("attrs"), dict) else {}
    sources = fallback_sources(item_name, item)
    points: list[str] = []
    source_claim = normalize_space(sources[0].get("claim") if sources else "")
    source_title = normalize_space(sources[0].get("title") if sources else "")
    if source_claim:
        if combination.lens == "jp_business":
            points.append(f"公開ソース「{source_title or item_name}」で基本情報を確認。")
        else:
            points.append(source_claim)
    axis_label = AXIS_LABELS[combination.lens][combination.axis]
    hook_axis_label = HOOK_AXIS_LABELS[combination.lens].get(combination.axis, axis_label)
    if combination.lens == "jp_business":
        points.append(f"シード評価では{hook_axis_label}の候補として採用。")
        if attrs.get("freeTier"):
            points.append("シード情報では無料枠で初期検証しやすい。")
        if attrs.get("selfHostable"):
            points.append("シード情報では自社運用しやすい候補。")
        if attr_number(item, "roi", 0) >= 5:
            points.append("シード情報では小規模チームのROIが高い候補。")
    else:
        points.append(f"Seed score puts {item_name} in this list for the {hook_axis_label} test.")
        if attrs.get("freeTier"):
            points.append("Seed metadata marks a free-tier path for initial testing.")
        if attrs.get("selfHostable"):
            points.append("Seed metadata marks it as self-hostable or control-friendly.")
        if attr_number(item, "roi", 0) >= 5:
            points.append("Seed metadata marks high ROI potential for small teams.")
    return [compact_text(point, 160) for point in points[:3] if normalize_space(point)]


def local_research(candidate: Candidate, lens: str, channel: Channel, store: dict[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
    items_by_name = item_index(store)
    item_pages: list[dict[str, object]] = []
    for index, item_name in enumerate(candidate.items, start=1):
        item = items_by_name.get(item_name.lower(), {})
        best_for = compact_item_lines(best_fit_note(item_name, item, lens), lens, item_name, "best_for")
        watch_out = compact_item_lines(watch_out_note(item, lens), lens, item_name, "watch_out")
        proof_points = proof_points_for_item(item_name, item, candidate.combination)
        hook_axis = HOOK_AXIS_LABELS[lens].get(candidate.combination.axis, candidate.combination.axis)
        if lens == "jp_business":
            headline = item_headline(item_name, lens)
            body = f"{item_name}は{hook_axis}の観点で見る候補。{watch_out}"
            takeaway = "まず1つの実務フローで試し、数字と運用負荷を見る。"
        else:
            headline = item_headline(item_name, lens)
            body = f"{item_name} made the cut for the {hook_axis} test. {watch_out}"
            takeaway = "Test it on one real workflow before committing budget."
        item_page = {
            "type": "item",
            "page_key": f"item_{index}",
            "item_name": item_name,
            "headline": compact_item_lines(headline, lens, item_name, "headline"),
            "body": compact_item_lines(body, lens, item_name, "body"),
            "takeaway": compact_item_lines(takeaway, lens, item_name, "takeaway"),
            "proof_points": proof_points,
            "best_for": best_for,
            "watch_out": watch_out,
            "image_search_query": f"{item_name} product logo documentation",
            "image_prompt": brand_image_prompt(
                f"Square editorial carousel illustration for {item_name}",
                channel,
            ),
            "sources": fallback_sources(item_name, item),
        }
        item_page["alt_text"] = item_alt_text(item_page, lens)
        item_pages.append(item_page)
    cover = {
        "type": "cover",
        "kicker": "CURATION",
        "headline": candidate.title,
        "subheadline": candidate.angle,
        "image_search_query": " ".join(candidate.items[:3]) or candidate.title,
        "image_prompt": brand_image_prompt(
            f"Premium square editorial cover art for: {candidate.title}. Use one strong symbolic object",
            channel,
        ),
        "style_notes": [
            channel.brand.get("source_pattern", "channel visual style"),
            "Use the normal channel cover layout when rendered.",
        ],
    }
    cover["alt_text"] = cover_alt_text(cover, candidate, lens)
    cta = {
        "type": "cta",
        "headline": "Save this list" if lens == "ph_builder" else "保存してあとで比較",
        "body": "Send it to the next builder choosing a stack." if lens == "ph_builder" else "次のAI導入比較で見返せるように保存してください。",
        "action": "Follow + Save" if lens == "ph_builder" else "保存 + フォロー",
    }
    cta["alt_text"] = cta_alt_text(cta, lens)
    return {
        "cover_page": cover,
        "items": item_pages,
        "cta": cta,
        "instagram_caption": build_instagram_caption(candidate, lens, item_pages, "local", {"used_google_search": False}),
    }, {"used_google_search": False, "web_search_queries": [], "sources": []}


def research_prompt(candidate: Candidate, lens: str, channel: Channel, item_details: list[dict[str, object]]) -> str:
    language = channel.language_name
    hook_rule = (
        "14 words or fewer" if lens != "jp_business" else "25 visible Japanese characters or fewer"
    )
    return f"""
You are creating carousel-ready JSON for {channel.brand_name}.

Important: the input idea is one whole carousel, not one slide.
The title becomes the cover page. Every item in candidate.items becomes exactly
one item page. If there are two items, produce two item pages.

Use the candidate title as the hook topic and the candidate angle as the
editorial thesis. Use Google Search grounding to research each item. Prefer
official docs, GitHub repos, pricing/docs pages, and primary sources. If you
use a secondary source, mark the claim narrowly.

Return strict JSON only:
{{
  "cover_page": {{
    "type": "cover",
    "kicker": "short section label",
    "headline": "{language} cover headline, faithful to the candidate title",
    "subheadline": "{language} one-line promise for the swipe",
    "alt_text": "{language} accessibility description for the cover slide",
    "image_search_query": "web image search phrase",
    "image_prompt": "gpt-image-2 ready square cover prompt in English",
    "style_notes": ["renderer or brand style hints"]
  }},
  "items": [
    {{
      "type": "item",
      "page_key": "item_1",
      "item_name": "copy exact candidate item name",
      "headline": "{language} item-page headline",
      "body": "{language} body copy, 1 to 2 tight sentences",
      "takeaway": "{language} one decision takeaway",
      "alt_text": "{language} accessibility description for this item slide",
      "proof_points": ["specific sourced point", "specific sourced point"],
      "best_for": "{language} short best-fit note",
      "watch_out": "{language} short caveat",
      "image_search_query": "specific product/docs/logo query",
      "image_prompt": "gpt-image-2 ready square item-page prompt in English",
      "sources": [
        {{"title": "source title", "url": "https://...", "claim": "what this source supports"}}
      ]
    }}
  ],
  "cta": {{
    "type": "cta",
    "headline": "{language} CTA headline",
    "body": "{language} CTA body",
    "action": "{language} short action",
    "alt_text": "{language} accessibility description for the CTA slide"
  }},
  "instagram_caption": "{language} Instagram description with actual research method, item explanations, and source URLs"
}}

Rules:
- items must have exactly {len(candidate.items)} entries, same order as candidate.items.
- Do not invent products outside candidate.items.
- cover_page.headline is the hook. Keep it faithful to candidate.title and {hook_rule}.
- Preserve the number in candidate.title when writing cover_page.headline.
- Sources must be public URLs. Use at least one source per item when search supports it.
- Add alt_text for every slide: cover_page, each item, and cta.
- instagram_caption must explain how research was done, what each item means,
  and include appropriate source URLs. Do not leave it generic.
- Do not include markdown, comments, citations outside the JSON, or extra top-level keys.
- Do not use em dashes.
- Cover image prompts should describe a symbolic editorial visual, not UI screenshots.
- Item image prompts can request a product-logo-inspired or documentation-inspired visual,
  but must avoid copying proprietary marks exactly unless a later renderer uses web images.
- Keep body copy ready for a carousel slide, not a blog post.
- Keep each visible item-page field to at most two display lines. Use shorter
  headline/body/takeaway/best_for/watch_out copy when the item name or topic is long.

Brand voice:
{channel.voice_prompt or channel.default_cover_voice()}

Candidate JSON:
{json.dumps(candidate.as_json(), ensure_ascii=False, indent=2)}

Known seed item details:
{json.dumps(item_details, ensure_ascii=False, indent=2)}
""".strip()


def normalize_item_pages(raw_items: object, candidate: Candidate, lens: str, channel: Channel, store: dict[str, Any]) -> list[dict[str, object]]:
    fallback_payload, _ = local_research(candidate, lens, channel, store)
    fallback_items = fallback_payload["items"] if isinstance(fallback_payload.get("items"), list) else []
    raw_list = raw_items if isinstance(raw_items, list) else []
    pages: list[dict[str, object]] = []
    for index, item_name in enumerate(candidate.items, start=1):
        raw = raw_list[index - 1] if index - 1 < len(raw_list) and isinstance(raw_list[index - 1], dict) else {}
        fallback = fallback_items[index - 1] if index - 1 < len(fallback_items) and isinstance(fallback_items[index - 1], dict) else {}
        sources = raw.get("sources") if isinstance(raw.get("sources"), list) else fallback.get("sources", [])
        clean_sources: list[dict[str, str]] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            title = normalize_space(source.get("title"))
            url = normalize_space(source.get("url"))
            claim = normalize_space(source.get("claim"))
            if title or url or claim:
                clean_sources.append({"title": title or url or item_name, "url": url, "claim": claim})
        page = {
            "type": "item",
            "page_key": f"item_{index}",
            "item_name": item_name,
            "headline": compact_item_lines(
                normalize_space(raw.get("headline")) or str(fallback.get("headline", "")),
                lens,
                item_name,
                "headline",
            ),
            "body": compact_item_lines(
                clean_article_text(normalize_space(raw.get("body")) or str(fallback.get("body", ""))),
                lens,
                item_name,
                "body",
            ),
            "takeaway": compact_item_lines(
                normalize_space(raw.get("takeaway")) or str(fallback.get("takeaway", "")),
                lens,
                item_name,
                "takeaway",
            ),
            "proof_points": [
                compact_text(normalize_space(point), 160)
                for point in raw.get("proof_points", [])
                if normalize_space(point)
            ] if isinstance(raw.get("proof_points"), list) else [],
            "best_for": compact_item_lines(
                normalize_space(raw.get("best_for")) or str(fallback.get("best_for", "")),
                lens,
                item_name,
                "best_for",
            ),
            "watch_out": compact_item_lines(
                normalize_space(raw.get("watch_out")) or str(fallback.get("watch_out", "")),
                lens,
                item_name,
                "watch_out",
            ),
            "alt_text": compact_text(
                normalize_space(raw.get("alt_text")) or str(fallback.get("alt_text", "")),
                260,
            ),
            "image_search_query": normalize_space(raw.get("image_search_query")) or str(fallback.get("image_search_query", "")),
            "image_prompt": brand_image_prompt(
                normalize_space(raw.get("image_prompt")) or str(fallback.get("image_prompt", "")),
                channel,
            ),
            "sources": clean_sources,
        }
        if not normalize_space(page.get("alt_text")):
            page["alt_text"] = item_alt_text(page, lens)
        pages.append(page)
    return pages


def research_carousel(candidate: Candidate, lens: str, provider: str, channel: Channel, store: dict[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
    items_by_name = item_index(store)
    item_details = [
        {
            "name": item_name,
            "seed": items_by_name.get(item_name.lower(), {}),
        }
        for item_name in candidate.items
    ]
    if provider != "gemini":
        return local_research(candidate, lens, channel, store)
    parsed, grounding = gemini_json(
        research_prompt(candidate, lens, channel, item_details),
        use_search=True,
        temperature=0.2,
        timeout=75,
    )
    if not parsed:
        payload, fallback_grounding = local_research(candidate, lens, channel, store)
        payload["research_warnings"] = ["Gemini research unavailable; used local seed fallback."]
        return payload, fallback_grounding
    fallback_payload, _ = local_research(candidate, lens, channel, store)
    cover_raw = parsed.get("cover_page") if isinstance(parsed.get("cover_page"), dict) else {}
    fallback_cover = fallback_payload["cover_page"] if isinstance(fallback_payload.get("cover_page"), dict) else {}
    cover = {
        "type": "cover",
        "kicker": normalize_space(cover_raw.get("kicker")) or str(fallback_cover.get("kicker", "")),
        "headline": compact_hook(normalize_space(cover_raw.get("headline")) or candidate.title, lens),
        "subheadline": compact_text(normalize_space(cover_raw.get("subheadline")) or candidate.angle, 220),
        "alt_text": compact_text(
            normalize_space(cover_raw.get("alt_text")) or str(fallback_cover.get("alt_text", "")),
            260,
        ),
        "image_search_query": normalize_space(cover_raw.get("image_search_query")) or str(fallback_cover.get("image_search_query", "")),
        "image_prompt": brand_image_prompt(
            normalize_space(cover_raw.get("image_prompt")) or str(fallback_cover.get("image_prompt", "")),
            channel,
        ),
        "style_notes": cover_raw.get("style_notes") if isinstance(cover_raw.get("style_notes"), list) else fallback_cover.get("style_notes", []),
    }
    if not normalize_space(cover.get("alt_text")):
        cover["alt_text"] = cover_alt_text(cover, candidate, lens)
    cta_raw = parsed.get("cta") if isinstance(parsed.get("cta"), dict) else {}
    fallback_cta = fallback_payload["cta"] if isinstance(fallback_payload.get("cta"), dict) else {}
    cta = {
        "type": "cta",
        "headline": normalize_space(cta_raw.get("headline")) or str(fallback_cta.get("headline", "")),
        "body": compact_text(normalize_space(cta_raw.get("body")) or str(fallback_cta.get("body", "")), 260),
        "action": normalize_space(cta_raw.get("action")) or str(fallback_cta.get("action", "")),
        "alt_text": compact_text(normalize_space(cta_raw.get("alt_text")) or str(fallback_cta.get("alt_text", "")), 220),
    }
    if not normalize_space(cta.get("alt_text")):
        cta["alt_text"] = cta_alt_text(cta, lens)
    normalized_items = normalize_item_pages(parsed.get("items"), candidate, lens, channel, store)
    return {
        "cover_page": cover,
        "items": normalized_items,
        "cta": cta,
        "instagram_caption": build_instagram_caption(candidate, lens, normalized_items, "gemini", grounding),
    }, grounding


def research_method(candidate: Candidate, provider: str, grounding: dict[str, object]) -> dict[str, object]:
    uses_search = bool(grounding.get("used_google_search"))
    steps = [
        "Treat source_candidate.title as the hook topic and source_candidate.angle as the editorial thesis.",
        "Keep source_candidate.items fixed as the research checklist; do not add unrelated products.",
        "Resolve each item against idea_engine/data/sets.json seed metadata before drafting slide copy.",
    ]
    if provider == "gemini":
        steps.append(
            "Run one grounded Gemini JSON prompt with Google Search enabled for the accepted hook topic."
        )
        steps.append(
            "Prefer official docs, GitHub repositories, pricing/docs pages, and other primary sources."
        )
    else:
        steps.append(
            "Use local seed metadata and known source URLs; mark missing web proof as pending."
        )
    steps.append(
        "Normalize the response into cover_page, item_N pages, CTA, per-item sources, and grounding metadata."
    )
    return {
        "hook_topic": candidate.title,
        "provider": provider,
        "uses_google_search": uses_search,
        "input_items": candidate.items,
        "steps": steps,
        "source_policy": (
            "The hook sets the angle, not the evidence. Claims should be tied to item-level "
            "sources, with secondary sources scoped narrowly."
        ),
        "web_search_queries": grounding.get("web_search_queries", []),
    }


def build_carousel_json(candidate: Candidate, lens: str, provider: str, store: dict[str, Any] | None = None) -> dict[str, object]:
    store = store or load_store()
    channel_id = LENS_CHANNEL[lens]
    channel = load_channel(channel_id)
    researched, grounding = research_carousel(candidate, lens, provider, channel, store)
    item_pages = researched.get("items") if isinstance(researched.get("items"), list) else []
    page_order = ["cover_page"] + [f"item_{index}" for index in range(1, len(item_pages) + 1)] + ["cta"]
    carousel: dict[str, object] = {
        "id": stable_id(candidate.id, "carousel", prefix="carousel"),
        "schema_version": 1,
        "type": "idea_carousel",
        "lens": lens,
        "channel_id": channel_id,
        "brand_name": channel.brand_name,
        "language": channel.language_name,
        "generated_at": now_iso(),
        "research_provider": provider,
        "source_candidate": candidate.as_json(),
        "research_method": research_method(candidate, provider, grounding),
        "page_order": page_order,
        "cover_page": researched.get("cover_page", {}),
        "cta": researched.get("cta", {}),
        "instagram_caption": researched.get("instagram_caption", ""),
        "grounding": grounding,
    }
    for index, page in enumerate(item_pages, start=1):
        carousel[f"item_{index}"] = page
    carousel["page_count"] = len(page_order)
    if researched.get("research_warnings"):
        carousel["research_warnings"] = researched["research_warnings"]
    return carousel


def validate_carousel(carousel: dict[str, object]) -> list[str]:
    errors: list[str] = []
    lens = normalize_space(carousel.get("lens")) or "ph_builder"
    page_order = carousel.get("page_order")
    if not isinstance(page_order, list) or not page_order:
        errors.append("page_order missing")
        return errors
    for key in page_order:
        if not isinstance(key, str):
            errors.append("page_order contains non-string key")
            continue
        if key not in carousel:
            errors.append(f"{key} missing")
    if "cover_page" not in page_order or "cta" not in page_order:
        errors.append("cover_page and cta must be in page_order")
    item_keys = [key for key in page_order if isinstance(key, str) and key.startswith("item_")]
    candidate = carousel.get("source_candidate") if isinstance(carousel.get("source_candidate"), dict) else {}
    candidate_items = candidate.get("items") if isinstance(candidate.get("items"), list) else []
    candidate_title = normalize_space(candidate.get("title"))
    if candidate_title and hook_length(candidate_title, lens) > hook_limit(lens):
        errors.append(hook_validation_error(candidate_title, lens, "source_candidate.title"))
    cover = carousel.get("cover_page") if isinstance(carousel.get("cover_page"), dict) else {}
    cover_headline = normalize_space(cover.get("headline"))
    if cover_headline and hook_length(cover_headline, lens) > hook_limit(lens):
        errors.append(hook_validation_error(cover_headline, lens, "cover_page.headline"))
    if not normalize_space(cover.get("alt_text")):
        errors.append("cover_page.alt_text missing")
    caption = normalize_space(carousel.get("instagram_caption"))
    if not caption:
        errors.append("instagram_caption missing")
    elif not re.search(r"research|method|調査", caption, re.I):
        errors.append("instagram_caption missing research method")
    if len(item_keys) != len(candidate_items):
        errors.append(f"item page count {len(item_keys)} does not match candidate items {len(candidate_items)}")
    source_urls: list[str] = []
    for key in item_keys:
        page = carousel.get(key)
        if not isinstance(page, dict):
            errors.append(f"{key} is not an object")
            continue
        if not normalize_space(page.get("item_name")):
            errors.append(f"{key}.item_name missing")
        if not normalize_space(page.get("headline")):
            errors.append(f"{key}.headline missing")
        if not normalize_space(page.get("body")):
            errors.append(f"{key}.body missing")
        if not normalize_space(page.get("takeaway")):
            errors.append(f"{key}.takeaway missing")
        if not normalize_space(page.get("alt_text")):
            errors.append(f"{key}.alt_text missing")
        item_name = normalize_space(page.get("item_name"))
        for field in ("headline", "body", "takeaway", "best_for", "watch_out"):
            value = normalize_space(page.get(field))
            if value and estimated_item_lines(value, lens, item_name, field) > ITEM_MAX_LINES:
                errors.append(f"{key}.{field} exceeds {ITEM_MAX_LINES} estimated lines")
        proof_points = page.get("proof_points") if isinstance(page.get("proof_points"), list) else []
        if not any(normalize_space(point) for point in proof_points):
            errors.append(f"{key}.proof_points missing")
        if not normalize_space(page.get("best_for")):
            errors.append(f"{key}.best_for missing")
        if not normalize_space(page.get("watch_out")):
            errors.append(f"{key}.watch_out missing")
        sources = page.get("sources") if isinstance(page.get("sources"), list) else []
        for source in sources:
            if isinstance(source, dict) and normalize_space(source.get("url")):
                source_urls.append(normalize_space(source.get("url")))
        if not any(
            isinstance(source, dict)
            and (
                normalize_space(source.get("title"))
                or normalize_space(source.get("url"))
                or normalize_space(source.get("claim"))
            )
            for source in sources
        ):
            errors.append(f"{key}.sources missing")
    if caption:
        missing_caption_urls = [
            url for url in dict.fromkeys(source_urls)
            if url and url not in caption
        ]
        if missing_caption_urls:
            errors.append("instagram_caption missing source links")
    cta = carousel.get("cta") if isinstance(carousel.get("cta"), dict) else {}
    if not normalize_space(cta.get("alt_text")):
        errors.append("cta.alt_text missing")
    return errors


def default_output_path(lens: str) -> Path:
    return ROOT / "out" / "idea-engine" / f"{lens}_carousels.json"


def run_idea_engine(
    *,
    lens: str,
    count: int = 10,
    provider: str = "local",
    out_path: Path | None = None,
    from_stories: Path | None = None,
    from_question: str | None = None,
    candidate_pool: int | None = None,
    candidates_per_combination: int = 3,
    set_category: str | None = None,
    axis: str | None = None,
    twist: str | None = None,
) -> dict[str, object]:
    load_env_file(ROOT / ".env")
    if lens not in LENS_CHANNEL:
        raise ValueError("--lens must be jp_business or ph_builder")
    if provider not in {"local", "gemini"}:
        raise ValueError("--llm-provider must be local or gemini")
    if provider == "gemini" and not gemini_api_key():
        raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY is required for --llm-provider gemini")
    store = load_store()
    if from_stories:
        selected = candidates_from_story_file(from_stories, lens, provider)[:count]
        killed: list[Candidate] = []
    else:
        selected, killed = generate_candidates(
            lens=lens,
            count=count,
            provider=provider,
            candidate_pool=candidate_pool,
            candidates_per_combination=max(1, min(3, candidates_per_combination)),
            set_category=set_category,
            axis=axis,
            twist=twist,
            from_question=from_question,
        )
    carousels = [build_carousel_json(candidate, lens, provider, store) for candidate in selected]
    validation_errors: dict[str, list[str]] = {}
    for carousel in carousels:
        errors = validate_carousel(carousel)
        if errors:
            validation_errors[str(carousel.get("id"))] = errors
    payload = {
        "schema_version": 1,
        "type": "idea_carousel_batch",
        "lens": lens,
        "channel_id": LENS_CHANNEL[lens],
        "provider": provider,
        "generated_at": now_iso(),
        "hook_constraints": {
            "ph_builder": {"max": HOOK_MAX_EN_WORDS, "unit": "words"},
            "jp_business": {"max": HOOK_MAX_JA_CHARS, "unit": "characters"},
        },
        "carousel_count": len(carousels),
        "killed_count": len(killed),
        "carousels": carousels,
        "validation_errors": validation_errors,
    }
    output = out_path or default_output_path(lens)
    write_json(output, payload)
    payload["carousel_json_path"] = str(output)
    return payload
