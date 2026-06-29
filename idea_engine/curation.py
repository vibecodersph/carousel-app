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
    category = CATEGORY_LABELS[combination.lens][combination.set_category]
    axis = AXIS_LABELS[combination.lens][combination.axis]
    twist = TWIST_LABELS[combination.lens][combination.twist]
    question_text = normalize_space(question.get("question")) if question else None
    if combination.lens == "jp_business":
        titles = [
            f"日本企業が次に比べるべき{category}: {twist}",
            f"{category}を{axis}: 保存しておきたい判断リスト",
            f"{names[0] if names else category}から見る{category}: {twist}",
        ]
    else:
        titles = [
            f"{category} na sulit for PH builders: {axis}",
            f"Stop guessing, {category} na sulit for remote-ready builds",
            f"{names[0] if names else category} at iba pa: {twist}",
        ]
    return [
        {"title": title, "angle": local_angle(combination, names, question_text), "items": names}
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
            title = normalize_space(candidate.get("title"))
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
    title = normalize_space(draft.get("title"))
    angle = compact_text(normalize_space(draft.get("angle")), 500)
    items = [normalize_space(item) for item in draft.get("items", []) if normalize_space(item)] if isinstance(draft.get("items"), list) else []
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


def generate_candidates(
    *,
    lens: str,
    count: int,
    provider: str,
    candidate_pool: int | None = None,
    candidates_per_combination: int = 2,
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
    return live[:count], killed + live[count:]


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


def local_research(candidate: Candidate, lens: str, channel: Channel, store: dict[str, Any]) -> tuple[dict[str, object], dict[str, object]]:
    items_by_name = item_index(store)
    item_pages: list[dict[str, object]] = []
    for index, item_name in enumerate(candidate.items, start=1):
        item = items_by_name.get(item_name.lower(), {})
        if lens == "jp_business":
            headline = f"{item_name}: 導入判断の比較ポイント"
            body = f"{item_name}を候補に入れる理由を、コスト、運用負荷、日本語業務との相性で確認するページ。"
            takeaway = "まず小さく試し、既存ワークフローに合うかを見る。"
        else:
            headline = f"{item_name}: sulit ba talaga?"
            body = f"Quick builder read on where {item_name} helps, what it saves, and what to watch before spending compute."
            takeaway = "Test it on one real workflow before committing budget."
        item_pages.append({
            "type": "item",
            "page_key": f"item_{index}",
            "item_name": item_name,
            "headline": headline,
            "body": body,
            "takeaway": takeaway,
            "proof_points": [],
            "best_for": "",
            "watch_out": "",
            "image_search_query": f"{item_name} product logo documentation",
            "image_prompt": brand_image_prompt(
                f"Square editorial carousel illustration for {item_name}",
                channel,
            ),
            "sources": fallback_sources(item_name, item),
        })
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
    cta = {
        "type": "cta",
        "headline": "Save this list" if lens == "ph_builder" else "保存してあとで比較",
        "body": "Send it to the next builder choosing a stack." if lens == "ph_builder" else "次のAI導入比較で見返せるように保存してください。",
        "action": "Follow + Save" if lens == "ph_builder" else "保存 + フォロー",
    }
    return {
        "cover_page": cover,
        "items": item_pages,
        "cta": cta,
        "instagram_caption": "",
    }, {"used_google_search": False, "web_search_queries": [], "sources": []}


def research_prompt(candidate: Candidate, lens: str, channel: Channel, item_details: list[dict[str, object]]) -> str:
    language = channel.language_name
    return f"""
You are creating carousel-ready JSON for {channel.brand_name}.

Important: the input idea is one whole carousel, not one slide.
The title becomes the cover page. Every item in candidate.items becomes exactly
one item page. If there are two items, produce two item pages.

Use Google Search grounding to research each item. Prefer official docs,
GitHub repos, pricing/docs pages, and primary sources. If you use a secondary
source, mark the claim narrowly.

Return strict JSON only:
{{
  "cover_page": {{
    "type": "cover",
    "kicker": "short section label",
    "headline": "{language} cover headline, faithful to the candidate title",
    "subheadline": "{language} one-line promise for the swipe",
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
    "action": "{language} short action"
  }},
  "instagram_caption": "{language} caption under 900 chars with one CTA and clean hashtags"
}}

Rules:
- items must have exactly {len(candidate.items)} entries, same order as candidate.items.
- Do not invent products outside candidate.items.
- Sources must be public URLs. Use at least one source per item when search supports it.
- Do not include markdown, comments, citations outside the JSON, or extra top-level keys.
- Do not use em dashes.
- Cover image prompts should describe a symbolic editorial visual, not UI screenshots.
- Item image prompts can request a product-logo-inspired or documentation-inspired visual,
  but must avoid copying proprietary marks exactly unless a later renderer uses web images.
- Keep body copy ready for a carousel slide, not a blog post.

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
        pages.append({
            "type": "item",
            "page_key": f"item_{index}",
            "item_name": item_name,
            "headline": compact_text(normalize_space(raw.get("headline")) or str(fallback.get("headline", "")), 120),
            "body": compact_text(clean_article_text(normalize_space(raw.get("body")) or str(fallback.get("body", ""))), 320),
            "takeaway": compact_text(normalize_space(raw.get("takeaway")) or str(fallback.get("takeaway", "")), 180),
            "proof_points": [
                compact_text(normalize_space(point), 160)
                for point in raw.get("proof_points", [])
                if normalize_space(point)
            ] if isinstance(raw.get("proof_points"), list) else [],
            "best_for": compact_text(normalize_space(raw.get("best_for")) or str(fallback.get("best_for", "")), 160),
            "watch_out": compact_text(normalize_space(raw.get("watch_out")) or str(fallback.get("watch_out", "")), 160),
            "image_search_query": normalize_space(raw.get("image_search_query")) or str(fallback.get("image_search_query", "")),
            "image_prompt": brand_image_prompt(
                normalize_space(raw.get("image_prompt")) or str(fallback.get("image_prompt", "")),
                channel,
            ),
            "sources": clean_sources,
        })
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
        "headline": compact_text(normalize_space(cover_raw.get("headline")) or candidate.title, 180),
        "subheadline": compact_text(normalize_space(cover_raw.get("subheadline")) or candidate.angle, 220),
        "image_search_query": normalize_space(cover_raw.get("image_search_query")) or str(fallback_cover.get("image_search_query", "")),
        "image_prompt": brand_image_prompt(
            normalize_space(cover_raw.get("image_prompt")) or str(fallback_cover.get("image_prompt", "")),
            channel,
        ),
        "style_notes": cover_raw.get("style_notes") if isinstance(cover_raw.get("style_notes"), list) else fallback_cover.get("style_notes", []),
    }
    cta_raw = parsed.get("cta") if isinstance(parsed.get("cta"), dict) else {}
    fallback_cta = fallback_payload["cta"] if isinstance(fallback_payload.get("cta"), dict) else {}
    cta = {
        "type": "cta",
        "headline": normalize_space(cta_raw.get("headline")) or str(fallback_cta.get("headline", "")),
        "body": compact_text(normalize_space(cta_raw.get("body")) or str(fallback_cta.get("body", "")), 260),
        "action": normalize_space(cta_raw.get("action")) or str(fallback_cta.get("action", "")),
    }
    return {
        "cover_page": cover,
        "items": normalize_item_pages(parsed.get("items"), candidate, lens, channel, store),
        "cta": cta,
        "instagram_caption": compact_text(normalize_space(parsed.get("instagram_caption")), 900),
    }, grounding


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
    if len(item_keys) != len(candidate_items):
        errors.append(f"item page count {len(item_keys)} does not match candidate items {len(candidate_items)}")
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
    candidates_per_combination: int = 2,
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
        "carousel_count": len(carousels),
        "killed_count": len(killed),
        "carousels": carousels,
        "validation_errors": validation_errors,
    }
    output = out_path or default_output_path(lens)
    write_json(output, payload)
    payload["carousel_json_path"] = str(output)
    return payload
