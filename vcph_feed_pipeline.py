#!/usr/bin/env python3
"""
Self-contained VCPH feed pipeline for carousel-app.

Loads RSS/sitemap/API sources from a registry JSON, fetches recent stories,
scores, deduplicates, and selects a diverse set of 5 stories: ~2 international,
~2 PH, ~1 workforce, with optional X trending replacement.

Designed to be importable and does NOT depend on ~/.hermes/ paths.
The source registry and SQLite dedupe DB live inside the carousel-app repo
so the whole team can use it.

Usage as module:
    from vcph_feed_pipeline import get_diverse_stories, load_source_registry
    registry = load_source_registry()
    stories = get_diverse_stories(registry_path="vcph_source_registry.json")
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

# ─────────────────────────── Defaults ───────────────────────────

ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY = ROOT / "vcph_source_registry.json"
DEFAULT_DB_DIR = ROOT / "out" / "daily_carousel"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "posted.db"
POSTED_HISTORY_DAYS = 21
DUPLICATE_TITLE_SIMILARITY = 0.86
FIRST_PARTY_HOURS = 168
QWEN_RESEARCH_API = "https://qwen.ai/api/page_config?code=research.research-list"

# ─────────────────────────── Keyword lists ───────────────────────────

AI_KEYWORDS = [
    "ai", "artificial intelligence", "machine learning", "llm", "gpt", "claude",
    "gemini", "qwen", "openai", "anthropic", "google deepmind", "meta ai", "mistral",
    "neural", "chatbot", "generative", "diffusion", "transformer", "robot",
    "automation", "deep learning", "nvidia", "hugging face", "model", "agent",
]

INTL_BIGCO = [
    "openai", "google", "anthropic", "meta", "nvidia", "microsoft", "apple",
    "tesla", "spacex", "amazon", "deepmind", "qwen", "mistral", "hugging face", "x.ai",
    "perplexity", "ibm", "samsung", "huawei", "baidu", "alibaba",
]

PH_KEYWORDS = [
    "philippines", "filipino", "manila", "cebu", "davao", "pagasa", "philsa",
    "dost", "dict", "dti", "dote", "meralco", "pldt", "globe telecom", "smart",
    "converge", "maynilad", "manila water", "napocor", "ngcp", "bsp", "gcash",
    "maya", "ayala", "jollibee", "sm ", "pagcor", "lazada ph", "shopee ph",
    "philippine", "pinoy", "boi", "peza", "ched", "deped",
]

TECH_SIGNAL = [
    "artificial intelligence", "machine learning", "llm", "gpt", "claude",
    "gemini", "openai", "anthropic", "chatbot", "generative ai", "ai model",
    "ai tool", "ai agent", "deep learning", "neural net", "nvidia",
    "startup", "fintech", "cybersecurity", "data center", "software",
    "app launch", "platform", "cloud computing", "saas", "blockchain",
    "e-commerce", "digital transformation", "semiconductor", "chip",
    "telecom", "telco", "5g", "fiber", "broadband", "satellite", "space",
    "power grid", "smart grid", "renewable energy", "solar", "ev ",
    "electric vehicle", "meralco", "pldt", "globe telecom", "converge",
    "smart communications", "maynilad", "manila water", "ngcp", "philsa",
    "dost", "dict", "peza",
]

WORKFORCE_KEYWORDS = [
    "layoff", "layoffs", "job cut", "hiring", "workforce", "labor market",
    "employment", "unemployment", "workers", "jobs", "upskill", "reskill",
    "freelancer", "gig economy", "outsourcing", "bpo", "automation jobs",
    "ai jobs", "tech jobs", "displaced", "restructuring",
]

MODEL_NAMES = [
    "gpt-5", "gpt-4", "gpt-6", "gpt 5", "gpt 4", "o1", "o3", "o4", "o5",
    "chatgpt", "codex", "sora",
    "claude 3", "claude 4", "claude 5", "claude opus", "claude sonnet",
    "claude haiku", "claude mythos",
    "gemini 1", "gemini 2", "gemini 3", "gemini pro", "gemini ultra",
    "gemini flash", "gemini nano", "gemma",
    "llama 3", "llama 4", "llama 5", "llama-3", "llama-4", "llama-5",
    "qwen", "deepseek", "kimi", "yi-", "glm-", "baichuan", "minimax",
    "doubao", "ernie", "hunyuan", "internlm", "mixtral", "mistral",
    "command r", "command-r", "phi-3", "phi-4", "grok",
    "dall-e", "dalle", "gpt image", "gpt-image", "nano banana",
    "flux.1", "flux 1", "flux pro", "flux dev", "flux schnell",
    "stable diffusion", "sd3", "sdxl", "midjourney", "imagen",
    "firefly", "ideogram", "recraft", "seedream",
    "sora 2", "sora-2", "veo 2", "veo 3", "veo-2", "veo-3",
    "kling", "seedance", "runway gen", "gen-3", "gen-4", "luma dream",
    "pika 1", "pika 2", "hailuo", "minimax video", "wan 2", "hunyuan video",
    "cogvideo", "mochi", "ltx video",
    "elevenlabs", "eleven labs", "suno", "udio", "stable audio",
    "audiocraft", "musicgen", "voxtral", "openvoice", "xtts",
    "whisper v", "whisper-v", "parakeet", "cartesia", "sesame",
    "figure 0", "figure 1", "figure 2", "figure 3", "optimus",
    "rt-2", "pi zero", "pi-zero",
    "blackwell", "hopper", "h100", "h200", "b100", "b200", "gb200",
    "mi300", "mi350", "tpu v5", "tpu v6", "trainium",
]

LAUNCH_VERBS = [
    "release", "released", "releases", "releasing",
    "launch", "launched", "launches", "launching",
    "announce", "announced", "announces", "announcing",
    "unveil", "unveiled", "unveils", "unveiling",
    "introduce", "introduced", "introduces", "introducing",
    "debut", "debuts", "debuted",
    "roll out", "rolls out", "rolled out", "rolling out",
    "ships", "shipped", "shipping",
    "open-source", "open sources", "open-sourced", "open sourced",
    "available", "now live", "generally available",
]

FRONTIER_LABS = [
    "openai", "anthropic", "google deepmind", "deepmind", "google",
    "meta ai", "meta", "nvidia", "microsoft", "apple",
    "mistral", "cohere", "xai", "x.ai", "perplexity", "stability ai",
    "hugging face", "huggingface", "runway", "pika", "luma",
    "alibaba", "qwen", "qwen team", "baidu", "tencent", "bytedance", "moonshot",
    "deepseek", "zhipu", "01.ai", "minimax", "kuaishou",
    "elevenlabs", "suno", "udio", "black forest labs", "midjourney",
    "ideogram", "recraft", "adobe",
]

SITEMAP_STORY_KEYWORDS = [
    "agent", "agents", "agentic", "api", "sdk", "mcp", "cli", "code",
    "coding", "developer", "model", "models", "reasoning", "computer use",
    "tool use", "workflow", "workflows", "voice", "speech", "ocr",
    "transcribe", "fine tune", "fine-tune", "open source", "open-weight",
    "opus", "sonnet", "haiku", "claude", "codestral", "devstral",
    "pixtral", "magistral", "voxtral", "le chat", "vibe", "nvidia",
]

LOW_SIGNAL_HF_MODEL_PATTERNS = [
    r"(^|/)SAE[-_]?Res[-_]",
    r"(^|/)SAE[-_]",
    r"-L0_\d+",
    r"-W\d+K-L0_\d+",
]


# ─────────────────────────── Registry loading ───────────────────────────

def load_source_registry(path=None):
    """Load the source registry JSON. Returns list of source entry dicts."""
    if path is None:
        path = DEFAULT_REGISTRY
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        sources = data.get("sources") if isinstance(data, dict) else []
        return sources if isinstance(sources, list) else []
    except Exception as e:
        print(f"  [warn] source registry unavailable ({path}): {e}")
        return []


def _registry_entries(sources, *, input_group=None, kind=None):
    """Filter registry entries by input_group and/or kind."""
    out = []
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        if input_group and entry.get("input_group") != input_group:
            continue
        if kind and entry.get("kind") != kind:
            continue
        out.append(entry)
    return out


def _build_feed_lists(sources):
    """Build categorized feed lists from the registry."""
    intl_feeds = [
        (e["name"], e["url"])
        for e in _registry_entries(sources, input_group="intl", kind="rss")
        if e.get("name") and e.get("url")
    ]
    first_party_feeds = [
        (e["name"], e["url"])
        for e in _registry_entries(sources, input_group="first_party_feed", kind="rss")
        if e.get("name") and e.get("url")
    ]
    first_party_sitemap = [
        (e["name"], e["url"], tuple(e.get("include_prefixes") or ()))
        for e in _registry_entries(sources, input_group="first_party_sitemap", kind="sitemap")
        if e.get("name") and e.get("url")
    ]
    hf_model_orgs = [
        (e["name"], e["author"])
        for e in _registry_entries(sources, input_group="first_party_hf_org", kind="hf_model_org")
        if e.get("name") and e.get("author")
    ]
    ph_feeds = [
        (e["name"], e["url"])
        for e in _registry_entries(sources, input_group="ph", kind="rss")
        if e.get("name") and e.get("url")
    ]
    feed_user_agents = {
        e["name"]: e["user_agent"]
        for e in sources
        if isinstance(e, dict) and e.get("name") and e.get("user_agent")
    }
    workforce_feeds = intl_feeds + ph_feeds

    # Source name sets for classification
    core_first_party = {
        (e.get("name") or "").strip().lower()
        for e in sources
        if isinstance(e, dict) and e.get("name")
        and e.get("official_source") and e.get("tech_signal_bypass")
    }
    first_party_names = {
        (e.get("name") or "").strip().lower()
        for e in sources
        if isinstance(e, dict) and e.get("name")
        and (e.get("official_source") or str(e.get("input_group") or "").startswith("first_party"))
    }

    return {
        "intl_feeds": intl_feeds,
        "first_party_feeds": first_party_feeds,
        "first_party_sitemap": first_party_sitemap,
        "hf_model_orgs": hf_model_orgs,
        "ph_feeds": ph_feeds,
        "workforce_feeds": workforce_feeds,
        "feed_user_agents": feed_user_agents,
        "core_first_party_names": core_first_party,
        "first_party_names": first_party_names,
        "source_registry_by_name": {
            (e.get("name") or "").strip().lower(): e
            for e in sources
            if isinstance(e, dict) and e.get("name")
        },
    }


# ─────────────────────────── Date parsing ───────────────────────────

def parse_date(date_str):
    if not date_str:
        return None
    date_str = str(date_str).strip()
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        iso = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d",):
        try:
            return datetime.strptime(date_str[:10], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


# ─────────────────────────── Keyword matching ───────────────────────────

def matches_any(text, keywords):
    t = text.lower()
    return any(kw in t for kw in keywords)


def is_ph_tech_story(title, desc):
    t = (title + " " + desc).lower()
    has_ph = any(kw in t for kw in PH_KEYWORDS)
    has_tech = any(sig in t for sig in TECH_SIGNAL)
    if not has_tech:
        has_tech = bool(re.search(r"\bai\b", t))
    return has_ph and has_tech


def story_has_tech_signal(story, core_first_party_names=None):
    text = (
        (story.get("title", "") or "") + " "
        + (story.get("desc", "") or "") + " "
        + (story.get("source", "") or "")
    ).lower()
    source = (story.get("source", "") or "").lower()
    explicit_ai_keywords = [kw for kw in AI_KEYWORDS if kw != "ai"]
    return (
        any(sig in text for sig in TECH_SIGNAL)
        or any(kw in text for kw in explicit_ai_keywords)
        or bool(re.search(r"\bai\b", text))
        or any(lab in text for lab in FRONTIER_LABS)
        or (core_first_party_names and source in core_first_party_names)
    )


# ─────────────────────────── Source helpers ───────────────────────────

def source_bucket_key(source_name, source_registry_by_name=None):
    if not source_registry_by_name:
        return "unknown"
    entry = source_registry_by_name.get((source_name or "").strip().lower())
    return entry.get("bucket") if entry and entry.get("bucket") else "unknown"


def source_priority_bonus(story, source_registry_by_name=None):
    if not source_registry_by_name:
        return 0
    entry = source_registry_by_name.get((story.get("source", "") or "").strip().lower())
    try:
        return int(entry.get("priority_bonus", 0)) if entry else 0
    except Exception:
        return 0


# ─────────────────────────── Feed fetching ───────────────────────────

def fetch_feed(name, url, feed_user_agents=None):
    try:
        ua = (feed_user_agents or {}).get(name, "Mozilla/5.0")
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = []

        for item in root.findall(".//item"):
            title = (item.findtext("title", "") or "").strip()
            link = (item.findtext("link", "") or "").strip()
            pub_date = item.findtext("pubDate", "") or ""
            desc = item.findtext("description", "") or ""
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:400]
            items.append((title, link, pub_date, desc, name))

        for entry in root.findall(".//atom:entry", ns):
            title = (entry.findtext("atom:title", "", ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            pub_date = entry.findtext("atom:updated", "", ns) or entry.findtext("atom:published", "", ns)
            desc = entry.findtext("atom:summary", "", ns) or ""
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:400]
            items.append((title, link, pub_date, desc, name))

        return items
    except Exception as e:
        print(f"  [warn] {name}: {e}")
        return []


def fetch_qwen_research_api():
    try:
        req = urllib.request.Request(
            QWEN_RESEARCH_API,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        items = []
        for item in data if isinstance(data, list) else []:
            title = (item.get("title") or "").strip()
            slug = (item.get("id") or "").strip()
            if not title or not slug:
                continue
            desc = (item.get("description") or item.get("introduction") or "").strip()
            desc = re.sub(r"<[^>]+>", "", desc).strip()[:400]
            link = f"https://qwen.ai/blog?id={slug}"
            pub_date = item.get("date") or ""
            items.append((title, link, pub_date, desc, "Qwen Research"))
        return items
    except Exception as e:
        print(f"  [warn] Qwen Research API: {e}")
        return []


def is_allowed_sitemap_loc(loc, sitemap_url, include_prefixes):
    if not loc:
        return False
    try:
        parsed_loc = urllib.parse.urlsplit(loc)
        parsed_sitemap = urllib.parse.urlsplit(sitemap_url)
    except Exception:
        return False
    if parsed_loc.scheme.lower() != "https":
        return False
    if parsed_loc.netloc.lower() != parsed_sitemap.netloc.lower():
        return False
    prefixes: tuple = tuple(include_prefixes) if include_prefixes else ()
    if not prefixes:
        return True
    return any(prefix in parsed_loc.path for prefix in prefixes)


def _title_from_url(url):
    try:
        slug = urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1]
    except Exception:
        slug = url.rsplit("/", 1)[-1]
    title = re.sub(r"[-_]+", " ", slug or "update").strip()
    if not title:
        title = "Update"
    title = title.title()
    replacements = {
        " Ai ": " AI ", " Api ": " API ", " Llm ": " LLM ",
        " Mcp ": " MCP ", " Ocr ": " OCR ", " Tts ": " TTS ", " Vllm ": " vLLM ",
    }
    padded = f" {title} "
    for old, new in replacements.items():
        padded = padded.replace(old, new)
    return padded.strip()


def is_sitemap_story_signal(title, url):
    try:
        path = urllib.parse.urlsplit(url or "").path.lower()
    except Exception:
        path = (url or "").lower()
    text = f"{title or ''} {path}".lower()
    return (
        any(name in text for name in MODEL_NAMES)
        or any(verb in text for verb in LAUNCH_VERBS)
        or any(keyword in text for keyword in SITEMAP_STORY_KEYWORDS)
    )


def fetch_sitemap_source(name, sitemap_url, include_prefixes, hours=168):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    try:
        req = urllib.request.Request(sitemap_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        nsmap = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for url_el in root.findall(".//ns:url", nsmap):
            loc = (url_el.findtext("ns:loc", "", nsmap) or "").strip()
            lastmod = url_el.findtext("ns:lastmod", "", nsmap) or ""
            dt = parse_date(lastmod)
            if not is_allowed_sitemap_loc(loc, sitemap_url, include_prefixes):
                continue
            if dt and dt < cutoff:
                continue
            title = _title_from_url(loc)
            if not is_sitemap_story_signal(title, loc):
                continue
            out.append({
                "title": title,
                "link": loc,
                "source": name,
                "date": dt or datetime.now(timezone.utc),
                "desc": f"From {name} (via sitemap).",
            })
    except Exception as e:
        print(f"  [warn] {name} sitemap: {e}")
    return out


def is_low_signal_hf_model_id(model_id):
    model_id = model_id or ""
    return any(re.search(pat, model_id, flags=re.I) for pat in LOW_SIGNAL_HF_MODEL_PATTERNS)


def _base_hf_model_name(model_id):
    name = (model_id or "").split("/", 1)[-1]
    name = re.sub(r"-(GPTQ|AWQ|GGUF|FP8|BF16|Int4|Int8).*$", "", name, flags=re.I)
    name = re.sub(r"-(Instruct|Base)$", "", name, flags=re.I)
    return name


def fetch_huggingface_model_org_updates(hf_model_orgs, hours=168, limit_per_org=20):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for source, author in hf_model_orgs:
        try:
            url = (
                "https://huggingface.co/api/models?"
                + urllib.parse.urlencode({
                    "author": author,
                    "sort": "lastModified",
                    "direction": "-1",
                    "limit": str(limit_per_org),
                })
            )
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            seen_bases = set()
            for model in data if isinstance(data, list) else []:
                model_id = model.get("modelId") or model.get("id") or ""
                modified = model.get("lastModified") or model.get("createdAt") or ""
                dt = parse_date(modified)
                if not model_id or not dt or dt < cutoff:
                    continue
                if is_low_signal_hf_model_id(model_id):
                    continue
                base = _base_hf_model_name(model_id)
                base_key = base.lower()
                if base_key in seen_bases:
                    continue
                seen_bases.add(base_key)
                tags = model.get("tags") or []
                pipeline = model.get("pipeline_tag") or "model"
                downloads = model.get("downloads") or 0
                title = f"{author} released {base} on Hugging Face"
                desc = (
                    f"Official {author} model repository updated on Hugging Face. "
                    f"Model: {model_id}. Task: {pipeline}. Downloads: {downloads}. "
                    f"Tags: {', '.join(tags[:8])}."
                )
                out.append({
                    "title": title,
                    "link": f"https://huggingface.co/{model_id}",
                    "source": source,
                    "date": dt,
                    "desc": desc[:400],
                })
        except Exception as e:
            print(f"  [warn] {source}: {e}")
    return out


# ─────────────────────────── Collection ───────────────────────────

def collect(feeds, feed_user_agents=None, hours=48):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for name, url in feeds:
        items = fetch_feed(name, url, feed_user_agents)
        for title, link, pub_date, desc, source in items:
            dt = parse_date(pub_date)
            if dt and dt >= cutoff:
                out.append({
                    "title": title, "link": link, "source": source,
                    "date": dt, "desc": desc,
                })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def collect_first_party_model_news(
    feeds=None,
    first_party_feeds=None,
    first_party_sitemap=None,
    hf_model_orgs=None,
    feed_user_agents=None,
    hours=168,
):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for title, link, pub_date, desc, source in fetch_qwen_research_api():
        dt = parse_date(pub_date)
        if dt and dt >= cutoff:
            out.append({"title": title, "link": link, "source": source, "date": dt, "desc": desc})
    if first_party_sitemap:
        for name, sitemap_url, prefixes in first_party_sitemap:
            out.extend(fetch_sitemap_source(name, sitemap_url, prefixes, hours=hours))
    if hf_model_orgs:
        out.extend(fetch_huggingface_model_org_updates(hf_model_orgs, hours=hours))
    if first_party_feeds:
        out.extend(collect(first_party_feeds, feed_user_agents, hours=hours))
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


# ─────────────────────────── Importance scoring ───────────────────────────

def importance_score(story, source_registry_by_name=None):
    text = (story["title"] + " " + story["desc"]).lower()
    score = source_priority_bonus(story, source_registry_by_name)

    if any(name in text for name in MODEL_NAMES):
        score += 60
    if re.search(r"\bqwen[-\s]?3\.6\b", text):
        score += 12

    lab_hit = any(lab in text for lab in FRONTIER_LABS)
    if lab_hit:
        score += 15

    verb_hit = any(v in text for v in LAUNCH_VERBS)
    if verb_hit:
        score += 15

    if lab_hit and verb_hit:
        score += 20

    for kw in ("new model", "new version", "update", "upgraded",
               "benchmark", "state-of-the-art", "sota", "multimodal",
               "reasoning model", "coding model", "video model",
               "image model", "voice model", "audio model",
               "text-to-video", "text-to-image", "text-to-speech",
               "open weights", "open-weight", "open source release"):
        if kw in text:
            score += 8

    for kw in ("acquires", "acquired", "acquisition", "buys", "bought",
               "raises $", "raised $", "funding round", "series a",
               "series b", "series c", "series d", "valuation",
               "ipo", "lawsuit", "sued", "settles", "layoff", "layoffs"):
        if kw in text:
            score -= 20

    for kw in ("doomscroll", "joke", "prank", "gag ", "meme"):
        if kw in text:
            score -= 15

    age_h = (datetime.now(timezone.utc) - story["date"]).total_seconds() / 3600
    if age_h < 6:
        score += 5
    elif age_h < 24:
        score += 3
    elif age_h < 48:
        score += 1

    return score


# ─────────────────────────── Deduplication ───────────────────────────

def dedupe(stories, used_titles):
    out = []
    for s in stories:
        t = s["title"].lower()[:50]
        if not t or any(t in u for u in used_titles):
            continue
        used_titles.add(t)
        out.append(s)
    return out


def _canonical_url(url):
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(k, v) for k, v in query if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
        clean_query = urllib.parse.urlencode(query)
        path = parsed.path.rstrip("/") or "/"
        return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, clean_query, ""))
    except Exception:
        return url.strip().lower()


def _normalize_title(title):
    t = (title or "").lower()
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _story_uid(story):
    basis = _canonical_url(story.get("link")) or _normalize_title(story.get("title", ""))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# ─────────────────────────── SQLite posted-history ───────────────────────────

def _posted_db_path(db_path=None):
    path = Path(db_path or DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _posted_db_conn(db_path=None):
    conn = sqlite3.connect(_posted_db_path(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posted_content (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            title_norm TEXT NOT NULL,
            url TEXT,
            url_norm TEXT,
            source TEXT,
            published_at TEXT,
            posted_at TEXT NOT NULL,
            bucket TEXT DEFAULT 'daily-carousel'
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posted_url_norm ON posted_content(url_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posted_posted_at ON posted_content(posted_at)")
    return conn


def load_posted_history(db_path=None, days=21):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    conn = _posted_db_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT title, title_norm, url_norm, source, posted_at FROM posted_content WHERE posted_at >= ? ORDER BY posted_at DESC",
            (cutoff.isoformat(),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"title": r[0], "title_norm": r[1], "url_norm": r[2] or "", "source": r[3] or "", "posted_at": r[4]}
        for r in rows
    ]


def _duplicate_reason(story, posted_rows):
    title_norm = _normalize_title(story.get("title", ""))
    url_norm = _canonical_url(story.get("link", ""))
    if not title_norm and not url_norm:
        return None
    for row in posted_rows:
        if url_norm and row.get("url_norm") and url_norm == row["url_norm"]:
            return f"same URL already posted: {row['title'][:70]}"
        old = row.get("title_norm") or ""
        if not old or not title_norm:
            continue
        similarity = SequenceMatcher(None, title_norm, old).ratio()
        containment = title_norm in old or old in title_norm
        if similarity >= DUPLICATE_TITLE_SIMILARITY or (containment and min(len(title_norm), len(old)) >= 28):
            return f"similar title already posted ({similarity:.2f}): {row['title'][:70]}"
    return None


def filter_previously_posted(stories, posted_rows, label):
    out = []
    skipped = 0
    for story in stories:
        reason = _duplicate_reason(story, posted_rows)
        if reason:
            skipped += 1
            print(f"  [skip duplicate/{label}] {story['title'][:80]} | {reason}")
            continue
        out.append(story)
    if skipped:
        print(f"  [dedupe/{label}] skipped {skipped} previously posted candidate(s)")
    return out


def record_posted_stories(stories, db_path=None):
    if not stories:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = _posted_db_conn(db_path)
    try:
        for s in stories:
            title = s.get("title", "") or ""
            conn.execute(
                """INSERT OR IGNORE INTO posted_content
                   (id, title, title_norm, url, url_norm, source, published_at, posted_at, bucket)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _story_uid(s), title, _normalize_title(title), s.get("link", ""),
                    _canonical_url(s.get("link", "")), s.get("source", ""),
                    s.get("date").isoformat() if hasattr(s.get("date"), "isoformat") else "",
                    now, "daily-carousel",
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────── Story selection ───────────────────────────

def finalize_story_selection(ordered, backup_all, limit=5, max_per_source=1):
    final = []
    seen_final = set()
    source_counts = {}

    def append_final(candidate, enforce_source_cap=True):
        if len(final) >= limit:
            return False
        if not story_has_tech_signal(candidate):
            print(f"  [drop non-tech] {candidate.get('source','?')}: {candidate.get('title','')[:80]}")
            return False
        uid = _story_uid(candidate)
        if uid in seen_final:
            return False
        source = (candidate.get("source") or "unknown").strip() or "unknown"
        source_key = source.lower()
        if enforce_source_cap and max_per_source and source_counts.get(source_key, 0) >= max_per_source:
            print(f"  [source diversity] skipped extra {source}: {candidate.get('title','')[:80]}")
            return False
        seen_final.add(uid)
        source_counts[source_key] = source_counts.get(source_key, 0) + 1
        final.append(candidate)
        return True

    for story in ordered:
        append_final(story, enforce_source_cap=True)

    if len(final) < limit:
        for candidate in backup_all:
            if len(final) >= limit:
                break
            append_final(candidate, enforce_source_cap=True)

    if len(final) < limit:
        for candidate in backup_all:
            if len(final) >= limit:
                break
            append_final(candidate, enforce_source_cap=False)

    return final[:limit]


# ─────────────────────────── X trending ───────────────────────────

def _load_env_value(*names):
    """Load a value from process env or the repo-local .env file."""
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


def _resolve_xai_credentials():
    """Resolve XAI_API_KEY from env or the repo-local .env file."""
    return _load_env_value("XAI_API_KEY")


def fetch_x_trending_news():
    """Fetch trending AI/tech news from X via xAI's x_search API. Returns story dict or None."""
    api_key = _resolve_xai_credentials()
    if not api_key:
        print("  [x_search] no xAI credentials found (skip X trending)")
        return None

    payload = {
        "model": "grok-4.20-reasoning",
        "input": [{"role": "user", "content": "What is the single most important AI or technology news story trending on X right now? Give me the post URL, author, and what it says."}],
        "tools": [{"type": "x_search"}],
        "store": False,
    }

    try:
        req = urllib.request.Request(
            "https://api.x.ai/v1/responses",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))

        if data.get("error"):
            print(f"  [x_search] API error: {data['error']}")
            return None

        # Extract answer from response
        output_items = data.get("output", [])
        answer = ""
        for item in output_items:
            if item.get("type") == "message" and item.get("role") == "assistant":
                for content_item in item.get("content", []):
                    if content_item.get("type") == "output_text":
                        answer += content_item.get("text", "")
                        break
        if not answer:
            answer = data.get("output_text", "")

        # Try to extract URL, handle, and title from the answer
        link = ""
        source = "X / trending"
        title = answer[:120].strip()
        if not title:
            for item in output_items:
                if item.get("type") == "x_search_result":
                    title = item.get("text", "")[:120]
                    link = item.get("url", "")
                    author = item.get("author", {})
                    if isinstance(author, dict):
                        source = f"X / @{author.get('userName', 'trending')}"
                    break

        if not title:
            return None

        return {
            "title": title.strip(),
            "link": link or "",
            "source": source,
            "desc": answer[:400] if answer else title,
            "date": datetime.now(timezone.utc),
        }
    except Exception as e:
        print(f"  [x_search] fetch failed: {e}")
        return None


# ─────────────────────────── Main entry: get_diverse_stories ───────────────────────────

def get_diverse_stories(
    registry_path=None,
    db_path=None,
    max_stories=5,
    include_x_trending=True,
    verbose=True,
):
    """Return a diverse set of AI/tech stories from the source registry.

    Args:
        registry_path: Path to vcph_source_registry.json (default: in repo).
        db_path: Path to SQLite dedupe DB (default: out/daily_carousel/posted.db).
        max_stories: Number of stories to return (default 5).
        include_x_trending: Whether to try X trending for the 3rd slot.
        verbose: Print scoring/debug info.

    Returns:
        List of story dicts, each with: title, link, source, date, desc.
    """
    sources = load_source_registry(registry_path)
    if not sources:
        print("  [warn] No sources in registry.")
        return []

    feeds = _build_feed_lists(sources)

    posted_rows = load_posted_history(db_path, days=POSTED_HISTORY_DAYS)
    if verbose and posted_rows:
        print(f"  Loaded {len(posted_rows)} posted-story fingerprints from the last {POSTED_HISTORY_DAYS} days")

    used = set()

    # International AI/tech
    first_party_pool = collect_first_party_model_news(
        first_party_feeds=feeds["first_party_feeds"],
        first_party_sitemap=feeds["first_party_sitemap"],
        hf_model_orgs=feeds["hf_model_orgs"],
        feed_user_agents=feeds["feed_user_agents"],
        hours=FIRST_PARTY_HOURS,
    )
    if verbose and first_party_pool:
        print(f"  Loaded {len(first_party_pool)} first-party model/lab announcement(s)")

    intl_pool = collect(feeds["intl_feeds"], feeds["feed_user_agents"]) + first_party_pool
    intl_filtered = [
        s for s in intl_pool
        if matches_any(s["title"] + " " + s["desc"], AI_KEYWORDS + INTL_BIGCO)
        and story_has_tech_signal(s, feeds["core_first_party_names"])
    ]
    intl_filtered = filter_previously_posted(intl_filtered, posted_rows, "intl")
    intl_filtered.sort(key=lambda s: (importance_score(s, feeds["source_registry_by_name"]), s["date"]), reverse=True)
    if verbose:
        for s in intl_filtered[:5]:
            print(f"  [intl score={importance_score(s, feeds['source_registry_by_name']):>3}] {s['title'][:80]}")
    intl = dedupe(intl_filtered, used)[:2]

    # PH
    ph_pool = filter_previously_posted(collect(feeds["ph_feeds"], feeds["feed_user_agents"]), posted_rows, "ph")
    ph_filtered = [s for s in ph_pool if is_ph_tech_story(s["title"], s["desc"])]
    ph_filtered.sort(key=lambda s: (importance_score(s, feeds["source_registry_by_name"]), s["date"]), reverse=True)
    ph = dedupe(ph_filtered, used)[:2]

    # Workforce
    wf_pool = filter_previously_posted(collect(feeds["workforce_feeds"], feeds["feed_user_agents"]), posted_rows, "workforce")
    wf_filtered = [
        s for s in wf_pool
        if matches_any(s["title"] + " " + s["desc"], WORKFORCE_KEYWORDS)
    ]
    wf_filtered.sort(key=lambda s: (importance_score(s, feeds["source_registry_by_name"]), s["date"]), reverse=True)
    wf = dedupe(wf_filtered, used)[:1]

    if verbose:
        print(
            f"  Candidate pool: intl {len(intl_pool)} fetched / {len(intl_filtered)} passed, "
            f"ph {len(ph_pool)} fetched / {len(ph_filtered)} passed, "
            f"workforce {len(wf_pool)} fetched / {len(wf_filtered)} passed"
        )

    ordered = []
    if intl: ordered.append(intl[0])
    if ph:   ordered.append(ph[0])

    # X trending replaces 2nd international slot
    x_story = None
    if include_x_trending and intl and len(intl) > 1:
        x_story = fetch_x_trending_news()
    if x_story:
        title_key = x_story["title"].lower()[:50]
        if title_key and title_key not in used:
            used.add(title_key)
            ordered.append(x_story)
            if verbose:
                print(f"  [x_trending] -> slot 3: {x_story['title'][:80]}")
        else:
            x_story = None
    if not x_story and intl and len(intl) > 1:
        ordered.append(intl[1])

    if ph and len(ph) > 1:     ordered.append(ph[1])
    if wf:   ordered.append(wf[0])

    backup_all = [
        s for s in (intl_filtered + ph_filtered)
        if s not in ordered and story_has_tech_signal(s, feeds["core_first_party_names"])
    ]
    backup = list(backup_all)
    while len(ordered) < max_stories and backup:
        cand = backup.pop(0)
        t = cand["title"].lower()[:50]
        if t and not any(t in u for u in used):
            used.add(t)
            ordered.append(cand)

    final = finalize_story_selection(ordered, backup_all, limit=max_stories, max_per_source=1)
    return final


# ─────────────────────────── CLI test mode ───────────────────────────

if __name__ == "__main__":
    import sys

    registry = sys.argv[1] if len(sys.argv) > 1 else None
    stories = get_diverse_stories(registry_path=registry, include_x_trending=False)

    print(f"\n=== Selected {len(stories)} stories ===\n")
    for i, s in enumerate(stories, 1):
        print(f"{i}. [{s['source']}] {s['title']}")
        print(f"   {s['link']}")
        print(f"   {s['desc'][:120]}")
        print()
