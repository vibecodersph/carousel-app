#!/usr/bin/env python3
"""Find high-signal X posts and articles, queue them, and run approved builds.

This is the human-in-the-loop front door for the carousel renderer:

    uv run python story_scout.py scan --config story_sources.json --notify
    uv run python story_scout.py list
    uv run python story_scout.py approve x_abcd1234
    uv run python story_scout.py telegram-poll --watch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from fetch_tweet_data import (
    extract_json_value,
    load_env_file,
    normalize_post,
    resolve_xai_token,
    xai_responses_text,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "story_sources.json"
DEFAULT_QUEUE = ROOT / "out" / "automation" / "candidates.json"
DEFAULT_TELEGRAM_STATE = ROOT / "out" / "automation" / "telegram_state.json"
DEFAULT_BUILDS_DIR = ROOT / "out" / "automation" / "builds"

QUEUE_VERSION = 1
SCOUT_USER_AGENT = "carousel-app/1.0 story-scout"
ARTICLE_FEED_MAX_BYTES = 3_000_000
ARTICLE_SIGNAL_TERMS = {
    "agent",
    "agentic",
    "ai",
    "alignment",
    "benchmark",
    "benchmarks",
    "coding",
    "context",
    "eval",
    "evaluation",
    "frontier",
    "github",
    "inference",
    "launch",
    "launched",
    "license",
    "model",
    "open source",
    "open-source",
    "paper",
    "policy",
    "pricing",
    "reasoning",
    "release",
    "released",
    "research",
    "safety",
    "score",
    "swe-bench",
    "tokens",
    "tool",
    "training",
}
ARTICLE_STRONG_TERMS = {
    "benchmark",
    "benchmarks",
    "eval",
    "github",
    "license",
    "open source",
    "open-source",
    "paper",
    "pricing",
    "released",
    "research",
    "score",
    "swe-bench",
}
# Formats that dilute an AI/dev feed: roundups, listicles, deals, sponsored posts.
# Articles have no engagement metadata, so we lean on content-format judgment the
# way score_post() leans on likes/replies.
LOW_SIGNAL_ARTICLE_PATTERNS = [
    r"\btop \d+\b",
    r"\b\d+ best\b",
    r"\bbest \w+ (?:tools|apps|gadgets|phones|laptops|deals)\b",
    r"\bround[\s-]?up\b",
    r"\bweekly (?:recap|digest|roundup|wrap)\b",
    r"\bsponsored\b",
    r"\badvertorial\b",
    r"\b(?:deal|deals|discount|coupon|promo code)\b",
    r"\bgiveaway\b",
    r"\bhoroscope\b",
]
# A named frontier-model family followed by a version number is a concrete launch
# signal for the builder audience (e.g. "GPT-5", "Claude 4", "Qwen3", "Gemini 2.5").
MODEL_RELEASE_PATTERN = (
    r"\b(?:gpt|claude|gemini|llama|qwen|mistral|grok|deepseek|phi|command|nova|"
    r"o\d|gemma|kimi|glm)[\s-]?\d"
)
ARTICLE_SOURCE_TYPES = {
    "rss",
    "atom",
    "feed",
    "sitemap",
    "json_api",
    "huggingface_models",
    "x_search",
    "workforce",
}
WORKFORCE_TERMS = {
    "automation",
    "bpo",
    "career",
    "careers",
    "contractor",
    "developers",
    "employment",
    "engineer",
    "engineers",
    "freelance",
    "future of work",
    "hiring",
    "job",
    "jobs",
    "labor",
    "labour",
    "layoff",
    "layoffs",
    "productivity",
    "recruiting",
    "reskill",
    "reskilling",
    "skills",
    "talent",
    "upskill",
    "upskilling",
    "work",
    "worker",
    "workers",
    "workforce",
}
DEFAULT_HUGGINGFACE_MODELS_API = (
    "https://huggingface.co/api/models"
    "?author={org}&sort=lastModified&direction=-1&limit={limit}"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slug_handle(value: str) -> str:
    return value.strip().lstrip("@")


def normalize_handle(value: str) -> str:
    handle = slug_handle(value)
    return f"@{handle}" if handle else ""


def candidate_id(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return f"x_{digest}"


def article_candidate_id(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return f"article_{digest}"


def compact_text(value: str, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def load_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse JSON in {path}: {exc}") from exc


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        example = ROOT / "story_sources.example.json"
        raise SystemExit(
            f"No story source config found at {path}. "
            f"Copy {example.name} to {path.name} and edit the account list."
        )
    config = load_json_file(path, {})
    accounts = config.get("accounts") or []
    if not isinstance(accounts, list):
        raise SystemExit(f"{path} accounts must be an array when present")
    config["accounts"] = [slug_handle(str(account)) for account in accounts if slug_handle(str(account))]
    article_sources = normalize_article_sources(config.get("article_sources") or [])
    config["article_sources"] = article_sources
    if not config["accounts"] and not article_sources:
        raise SystemExit(
            f"{path} must contain at least one X account or article source"
        )
    config["lookback_hours"] = int(config.get("lookback_hours") or 24)
    config["article_lookback_hours"] = int(config.get("article_lookback_hours") or 72)
    config["max_posts_per_account"] = int(config.get("max_posts_per_account") or 5)
    config["max_articles_per_source"] = int(config.get("max_articles_per_source") or 5)
    config["min_score"] = int(config.get("min_score") or 55)
    config["article_min_score"] = int(config.get("article_min_score") or 45)
    config["include_keywords"] = [
        str(item).lower()
        for item in config.get("include_keywords", [])
        if str(item).strip()
    ]
    config["exclude_keywords"] = [
        str(item).lower()
        for item in config.get("exclude_keywords", [])
        if str(item).strip()
    ]
    return config


def normalize_article_sources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SystemExit("article_sources must be an array when present")
    sources: list[dict[str, Any]] = []
    for raw in value:
        if isinstance(raw, str):
            source = {"name": raw, "feed_url": raw}
        elif isinstance(raw, dict):
            source = dict(raw)
        else:
            continue

        feed_urls = normalize_string_list(source.get("feed_urls"))
        feed_url = str(source.get("feed_url") or "").strip()
        if feed_url and feed_url not in feed_urls:
            feed_urls.insert(0, feed_url)
        urls = normalize_string_list(source.get("urls"))
        sitemap_url = str(source.get("sitemap_url") or "").strip()
        json_url = str(source.get("json_url") or "").strip()
        api_url = str(source.get("api_url") or "").strip()
        huggingface_org = str(source.get("huggingface_org") or "").strip()
        x_search_query = str(source.get("x_search_query") or source.get("query") or "").strip()
        source_type = infer_article_source_type(source, feed_urls)

        if (
            source_type not in {"workforce", "x_search"}
            and not feed_urls
            and not sitemap_url
            and not json_url
            and not api_url
            and not huggingface_org
            and not urls
        ):
            continue
        if source_type == "x_search" and not x_search_query:
            continue

        source["source_type"] = source_type
        source["feed_urls"] = feed_urls
        source["feed_url"] = feed_urls[0] if feed_urls else feed_url
        source["urls"] = urls
        source["sitemap_url"] = sitemap_url
        source["json_url"] = json_url
        source["api_url"] = api_url
        source["huggingface_org"] = huggingface_org
        source["x_search_query"] = x_search_query
        source["name"] = str(
            source.get("name")
            or feed_url
            or sitemap_url
            or json_url
            or api_url
            or huggingface_org
            or "Article source"
        ).strip()
        source["include_keywords"] = normalize_keyword_list(source.get("include_keywords"))
        source["exclude_keywords"] = normalize_keyword_list(source.get("exclude_keywords"))
        source["include_paths"] = normalize_path_filters(source.get("include_paths"))
        source["exclude_paths"] = normalize_path_filters(source.get("exclude_paths"))
        if source_type == "workforce":
            workforce_keywords = source.get("workforce_keywords") or sorted(WORKFORCE_TERMS)
        else:
            workforce_keywords = source.get("workforce_keywords")
        source["workforce_keywords"] = normalize_keyword_list(workforce_keywords)
        sources.append(source)
    return sources


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
    return items


def normalize_keyword_list(value: Any) -> list[str]:
    return [item.lower() for item in normalize_string_list(value)]


def normalize_path_filters(value: Any) -> list[str]:
    filters: list[str] = []
    for item in normalize_string_list(value):
        filters.append(item.lower())
    return filters


def infer_article_source_type(source: dict[str, Any], feed_urls: list[str]) -> str:
    raw_type = str(source.get("source_type") or source.get("type") or "").strip().lower()
    raw_type = raw_type.replace("-", "_")
    aliases = {
        "rss_feed": "rss",
        "rss/atom": "rss",
        "atom_feed": "atom",
        "json": "json_api",
        "api": "json_api",
        "hf_models": "huggingface_models",
        "huggingface": "huggingface_models",
        "x": "x_search",
        "grok": "x_search",
        "derived_workforce": "workforce",
    }
    source_type = aliases.get(raw_type, raw_type)
    if source_type in ARTICLE_SOURCE_TYPES:
        return source_type
    if source.get("sitemap_url"):
        return "sitemap"
    if source.get("json_url"):
        return "json_api"
    if source.get("huggingface_org") or source.get("api_url"):
        return "huggingface_models"
    if source.get("x_search_query") or source.get("query"):
        return "x_search"
    if source.get("workforce_keywords") or source.get("derived_from") == "article_sources":
        return "workforce"
    if feed_urls:
        return "rss"
    return "rss"


def load_queue(path: Path) -> dict[str, Any]:
    queue = load_json_file(
        path,
        {
            "version": QUEUE_VERSION,
            "updated_at": utc_now(),
            "candidates": [],
        },
    )
    if "candidates" not in queue or not isinstance(queue["candidates"], list):
        queue["candidates"] = []
    queue["version"] = QUEUE_VERSION
    return queue


def save_queue(path: Path, queue: dict[str, Any]) -> None:
    queue["updated_at"] = utc_now()
    write_json_file(path, queue)


def parse_count(value: Any) -> int:
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if not isinstance(value, str):
        return 0
    raw = value.strip().replace(",", "")
    multiplier = 1
    if raw.lower().endswith("k"):
        multiplier = 1_000
        raw = raw[:-1]
    elif raw.lower().endswith("m"):
        multiplier = 1_000_000
        raw = raw[:-1]
    try:
        return max(0, int(float(raw) * multiplier))
    except ValueError:
        return 0


def weighted_log(value: int, scale: int, cap: int) -> int:
    if value <= 0:
        return 0
    score = int(round(math.log10(value + 1) * scale))
    return min(cap, score)


def score_post(post: dict[str, Any], config: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    text = str(post.get("text") or "").lower()
    likes = parse_count(post.get("likes"))
    retweets = parse_count(post.get("retweets"))
    replies = parse_count(post.get("replies"))
    views = parse_count(post.get("views"))

    score = 0
    view_score = weighted_log(views, 6, 28)
    like_score = weighted_log(likes, 7, 24)
    repost_score = weighted_log(retweets, 8, 20)
    reply_score = weighted_log(replies, 5, 10)
    score += view_score + like_score + repost_score + reply_score

    if view_score:
        reasons.append(f"{views:,} views")
    if like_score:
        reasons.append(f"{likes:,} likes")
    if repost_score:
        reasons.append(f"{retweets:,} reposts")
    if reply_score:
        reasons.append(f"{replies:,} replies")

    matched_keywords = matched_terms(text, config.get("include_keywords", []), limit=5)
    if matched_keywords:
        keyword_score = min(18, 4 * len(matched_keywords))
        score += keyword_score
        reasons.append("keywords: " + ", ".join(matched_keywords))

    excluded = matched_terms(text, config.get("exclude_keywords", []))
    if excluded:
        score -= 25
        reasons.append("excluded keyword: " + ", ".join(excluded[:3]))

    if bool(post.get("has_video")):
        score += 6
        reasons.append("has video")

    if re.search(r"\bthread\b|(?:^|\s)1/\d+|(?:^|\s)1/", text):
        score += 5
        reasons.append("thread/story format")

    if "?" in text and replies >= 50:
        score += 4
        reasons.append("discussion momentum")

    score = max(0, min(100, score))
    if not reasons:
        reasons.append("low public engagement metadata")
    return score, reasons


def build_scout_prompt(config: dict[str, Any], limit: int) -> str:
    handles = ", ".join(f"@{handle}" for handle in config["accounts"])
    include = ", ".join(config.get("include_keywords", [])) or "AI, technology, product news"
    exclude = ", ".join(config.get("exclude_keywords", [])) or "low-signal promotion"
    lookback = config["lookback_hours"]
    max_posts = config["max_posts_per_account"]
    return f"""
You are a strict story scout for an Instagram carousel workflow.

Search X/Twitter for recent original posts from these accounts:
{handles}

Find up to {limit} posts from the last {lookback} hours, with no more than {max_posts}
posts per account. Prefer posts that would make strong vibecodersph carousel source material:
AI product launches, notable model/research releases, safety or policy shifts, strong
technical claims, visible company/person announcements, controversies, benchmarks,
major customer/adoption signals, and posts with unusually high engagement.

Prefer these themes when relevant: {include}.
Avoid posts about: {exclude}.

Return ONLY a raw JSON array. No markdown, no code fences. Each object must have:
"id": "tweet id string",
"full_text": "complete post text",
"author_name": "display name",
"handle": "@username",
"date": "Mon D, YYYY if available",
"likes": number,
"retweets": number,
"replies": number,
"views": number,
"has_video": boolean,
"url": "https://x.com/username/status/id",
"why": "one short reason this is carousel-worthy"
""".strip()


def normalize_scout_post(item: dict[str, Any]) -> dict[str, Any] | None:
    post = normalize_post(item, fallback_id=str(item.get("id") or ""))
    url = str(item.get("url") or post.get("url") or "").strip()
    if not post["id"] or not post["text"] or not url:
        return None
    if "x.com/" not in url and "twitter.com/" not in url:
        return None
    post["url"] = re.sub(r"\?.*$", "", url.replace("twitter.com", "x.com"))
    post["why"] = str(item.get("why") or "").strip()
    return post


def fetch_scout_posts(config: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    token = resolve_xai_token(required=True)
    prompt = build_scout_prompt(config, limit)
    text = xai_responses_text(prompt, token, timeout=120)
    data = extract_json_value(text, "[", "]")
    if not isinstance(data, list):
        raise SystemExit("xAI returned JSON that is not an array")

    posts: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    allowed_handles = {normalize_handle(account).lower() for account in config["accounts"]}
    per_handle: dict[str, int] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        post = normalize_scout_post(item)
        if not post:
            continue
        handle = str(post.get("handle") or "").lower()
        if allowed_handles and handle not in allowed_handles:
            continue
        if per_handle.get(handle, 0) >= config["max_posts_per_account"]:
            continue
        url = str(post["url"])
        if url in seen_urls:
            continue
        seen_urls.add(url)
        per_handle[handle] = per_handle.get(handle, 0) + 1
        posts.append(post)
    return posts


def xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def find_child_text(parent: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in list(parent):
        if xml_local_name(child.tag) in wanted:
            text = "".join(child.itertext())
            return compact_whitespace(text)
    return ""


def find_atom_link(entry: ET.Element) -> str:
    fallback = ""
    for child in list(entry):
        if xml_local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if not href:
            continue
        rel = str(child.attrib.get("rel") or "alternate").lower()
        if rel == "alternate":
            return href
        fallback = fallback or href
    return fallback


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|svg)\b.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return compact_whitespace(value)


def compact_whitespace(value: str) -> str:
    import html as html_module

    value = html_module.unescape(value or "")
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def text_has_term(text: str, term: str) -> bool:
    term = str(term or "").strip().lower()
    if not term:
        return False
    escaped = re.escape(term)
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text, re.I) is not None


def matched_terms(text: str, terms: Any, *, limit: int | None = None) -> list[str]:
    if not isinstance(terms, (list, set, tuple)):
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean_term = str(term).strip().lower()
        if not clean_term or clean_term in seen:
            continue
        if text_has_term(text, clean_term):
            seen.add(clean_term)
            hits.append(clean_term)
            if limit is not None and len(hits) >= limit:
                break
    return hits


def parse_feed_datetime(value: str) -> datetime | None:
    value = compact_whitespace(value)
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def feed_item_datetime(item: dict[str, Any]) -> datetime | None:
    return parse_feed_datetime(str(item.get("published_at") or ""))


def fetch_url_text(url: str, *, timeout: int = 25, max_bytes: int = ARTICLE_FEED_MAX_BYTES) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": SCOUT_USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json, text/xml, text/html;q=0.8, */*;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise RuntimeError(f"response exceeded {max_bytes:,} bytes")
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        print(f"[article] feed HTTP {exc.code}: {url}", file=sys.stderr)
        return ""
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        print(f"[article] feed fetch failed for {url}: {exc}", file=sys.stderr)
        return ""
    return raw.decode(charset, errors="replace")


def parse_feed_entries(feed_xml: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    if not feed_xml.strip():
        return []
    try:
        root = ET.fromstring(feed_xml)
    except ET.ParseError as exc:
        print(f"[article] could not parse feed {source.get('feed_url')}: {exc}", file=sys.stderr)
        return []

    source_name = str(source.get("name") or "Article source")
    entries: list[dict[str, Any]] = []
    items = root.findall(".//item")
    if not items:
        items = [
            element
            for element in root.iter()
            if xml_local_name(element.tag) == "entry"
        ]

    for item in items:
        is_atom = xml_local_name(item.tag) == "entry"
        title = find_child_text(item, "title")
        if is_atom:
            url = find_atom_link(item)
            summary = find_child_text(item, "summary", "content")
            published = find_child_text(item, "published", "updated")
        else:
            url = find_child_text(item, "link") or find_child_text(item, "guid")
            summary = find_child_text(item, "description", "summary", "encoded")
            published = find_child_text(item, "pubDate", "published", "updated", "date")
        url = url.strip()
        if not title or not url.startswith(("http://", "https://")):
            continue
        entries.append(
            {
                "url": re.sub(r"#.*$", "", url),
                "title": compact_whitespace(title),
                "summary": strip_html(summary),
                "source_name": source_name,
                "feed_url": source.get("feed_url", ""),
                "published_at": compact_whitespace(published),
            }
        )
    return entries


def source_primary_url(source: dict[str, Any]) -> str:
    return (
        str(source.get("feed_url") or "")
        or str(source.get("sitemap_url") or "")
        or str(source.get("json_url") or "")
        or str(source.get("api_url") or "")
    )


def url_matches_source_paths(url: str, source: dict[str, Any]) -> bool:
    include_paths = source.get("include_paths") or []
    exclude_paths = source.get("exclude_paths") or []
    if not include_paths and not exclude_paths:
        return True
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    full = urllib.parse.urlunparse(("", "", parsed.path, "", parsed.query, "")).lower()
    if include_paths and not any(path_filter in path or path_filter in full for path_filter in include_paths):
        return False
    if exclude_paths and any(path_filter in path or path_filter in full for path_filter in exclude_paths):
        return False
    return True


def smart_title_word(word: str) -> str:
    lowered = word.lower()
    acronyms = {
        "ai",
        "api",
        "asti",
        "dost",
        "gma",
        "gpu",
        "gpt",
        "hf",
        "ieee",
        "llm",
        "mit",
        "nasa",
        "nvidia",
        "ph",
        "philsa",
        "qwen",
        "rss",
        "swe",
        "vl",
    }
    if lowered in acronyms:
        return lowered.upper()
    if re.fullmatch(r"[a-z]+\d+(?:\.\d+)?", lowered):
        letters = re.match(r"[a-z]+", lowered)
        if letters and letters.group(0) in acronyms:
            return letters.group(0).upper() + lowered[len(letters.group(0)) :]
    return word[:1].upper() + word[1:]


def title_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path or "")
    slug = next((part for part in reversed(path.split("/")) if part), "")
    if not slug:
        return parsed.netloc.removeprefix("www.") or url
    slug = re.sub(r"\.(?:html?|php|aspx?)$", "", slug, flags=re.I)
    slug = re.sub(r"^\d{4}[-_/]\d{2}[-_/]\d{2}[-_/]?", "", slug)
    words = [word for word in re.split(r"[-_\s]+", slug) if word]
    if not words:
        return parsed.netloc.removeprefix("www.") or url
    return compact_whitespace(" ".join(smart_title_word(word) for word in words))


def parse_sitemap_entries(sitemap_xml: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    if not sitemap_xml.strip():
        return []
    try:
        root = ET.fromstring(sitemap_xml)
    except ET.ParseError as exc:
        print(f"[article] could not parse sitemap {source.get('sitemap_url')}: {exc}", file=sys.stderr)
        return []

    source_name = str(source.get("name") or "Article source")
    sitemap_url = str(source.get("sitemap_url") or source.get("feed_url") or "")
    entries: list[dict[str, Any]] = []
    for element in list(root):
        kind = xml_local_name(element.tag)
        if kind == "sitemap":
            loc = find_child_text(element, "loc")
            if loc.startswith(("http://", "https://")):
                entries.append({"_sitemap_url": loc})
            continue
        if kind != "url":
            continue
        loc = find_child_text(element, "loc")
        if not loc.startswith(("http://", "https://")):
            continue
        if not url_matches_source_paths(loc, source):
            continue
        lastmod = find_child_text(element, "lastmod")
        title = title_from_url(loc)
        entries.append(
            {
                "url": re.sub(r"#.*$", "", loc),
                "title": title,
                "summary": f"{title} from {source_name}",
                "source_name": source_name,
                "feed_url": sitemap_url,
                "published_at": compact_whitespace(lastmod),
            }
        )
    return entries


def fetch_sitemap_entries(source: dict[str, Any], *, max_sitemaps: int = 12) -> list[dict[str, Any]]:
    root_url = str(source.get("sitemap_url") or source.get("feed_url") or "").strip()
    if not root_url:
        return []
    pending = [root_url]
    seen_sitemaps: set[str] = set()
    entries: list[dict[str, Any]] = []
    while pending and len(seen_sitemaps) < max_sitemaps:
        sitemap_url = pending.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        scan_source = {**source, "sitemap_url": sitemap_url}
        parsed = parse_sitemap_entries(fetch_url_text(sitemap_url), scan_source)
        for item in parsed:
            nested_url = str(item.get("_sitemap_url") or "")
            if nested_url:
                pending.append(nested_url)
                continue
            entries.append(item)
    return entries


def json_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return compact_whitespace(str(value))
    if isinstance(value, list):
        parts = [json_text_value(item) for item in value]
        return compact_whitespace(" ".join(part for part in parts if part))
    if isinstance(value, dict):
        for key in (
            "text",
            "title",
            "name",
            "headline",
            "description",
            "summary",
            "abstract",
        ):
            text = json_text_value(value.get(key))
            if text:
                return text
    return ""


def json_first_text(item: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, str]:
    lowered = {str(key).lower(): key for key in item.keys()}
    for key in keys:
        raw_key = lowered.get(key.lower())
        if raw_key is None:
            continue
        text = json_text_value(item.get(raw_key))
        if text:
            return text, raw_key
    return "", ""


def iter_json_dicts(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, dict):
        items.append(value)
        for child in value.values():
            items.extend(iter_json_dicts(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(iter_json_dicts(child))
    return items


def format_item_url(template: str, raw_value: str, raw_key: str) -> str:
    if not template:
        return ""
    slug = raw_value.strip().strip("/")
    replacements = {
        "slug": urllib.parse.quote(slug),
        "id": urllib.parse.quote(slug),
        "path": urllib.parse.quote(slug),
        "value": urllib.parse.quote(slug),
    }
    if raw_key:
        replacements[raw_key] = urllib.parse.quote(slug)
    try:
        return template.format(**replacements)
    except KeyError:
        return ""


def json_object_to_article_item(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any] | None:
    title, _ = json_first_text(
        item,
        (
            "title",
            "headline",
            "name",
            "articleTitle",
            "paperTitle",
            "modelName",
            "modelId",
        ),
    )
    url_value, url_key = json_first_text(
        item,
        ("url", "link", "href", "permalink", "path", "slug"),
    )
    if not url_value:
        url_value, url_key = json_first_text(item, ("id",))
    if not title or not url_value:
        return None

    item_url = ""
    if url_value.startswith(("http://", "https://")):
        item_url = url_value
    elif (
        str(source.get("item_url_template") or "")
        and url_key.lower() in {"slug", "id"}
        and not url_value.startswith("/")
    ):
        item_url = format_item_url(str(source.get("item_url_template") or ""), url_value, url_key)
    if not item_url:
        base_url = str(source.get("site_url") or source.get("json_url") or "")
        item_url = urllib.parse.urljoin(base_url, url_value)
    if not item_url.startswith(("http://", "https://")):
        return None
    if not url_matches_source_paths(item_url, source):
        return None

    summary, _ = json_first_text(
        item,
        (
            "summary",
            "description",
            "abstract",
            "excerpt",
            "intro",
            "content",
            "body",
        ),
    )
    published, _ = json_first_text(
        item,
        (
            "published_at",
            "publishedAt",
            "publishTime",
            "releaseDate",
            "date",
            "createdAt",
            "updatedAt",
            "lastModified",
        ),
    )
    return {
        "url": re.sub(r"#.*$", "", item_url),
        "title": compact_whitespace(title),
        "summary": strip_html(summary),
        "source_name": str(source.get("name") or "Article source"),
        "feed_url": str(source.get("json_url") or source.get("api_url") or ""),
        "published_at": compact_whitespace(published),
    }


def parse_json_api_entries(json_text: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    if not json_text.strip():
        return []
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        print(f"[article] could not parse JSON source {source.get('json_url')}: {exc}", file=sys.stderr)
        return []

    entries: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for obj in iter_json_dicts(data):
        item = json_object_to_article_item(obj, source)
        if not item:
            continue
        url = str(item.get("url") or "")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        entries.append(item)
    return entries


def huggingface_models_api_url(source: dict[str, Any], *, limit: int) -> str:
    api_url = str(source.get("api_url") or "").strip()
    if api_url:
        return api_url
    org = str(source.get("huggingface_org") or "").strip()
    if not org:
        return ""
    return DEFAULT_HUGGINGFACE_MODELS_API.format(
        org=urllib.parse.quote(org),
        limit=max(1, limit),
    )


def parse_huggingface_model_entries(json_text: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    if not json_text.strip():
        return []
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        print(f"[article] could not parse Hugging Face source {source.get('api_url')}: {exc}", file=sys.stderr)
        return []
    if isinstance(data, dict):
        raw_items = data.get("models") or data.get("items") or data.get("data") or []
    else:
        raw_items = data
    if not isinstance(raw_items, list):
        return []

    entries: list[dict[str, Any]] = []
    source_name = str(source.get("name") or "Hugging Face models")
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("modelId") or raw.get("id") or "").strip()
        if not model_id or "/" not in model_id:
            continue
        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        tag_text = ", ".join(str(tag) for tag in tags[:8] if str(tag).strip())
        downloads = parse_count(raw.get("downloads"))
        likes = parse_count(raw.get("likes"))
        summary_bits = [
            f"{model_id} model listing",
            str(raw.get("pipeline_tag") or raw.get("library_name") or "").strip(),
            f"{downloads:,} downloads" if downloads else "",
            f"{likes:,} likes" if likes else "",
            tag_text,
        ]
        entries.append(
            {
                "url": f"https://huggingface.co/{model_id}",
                "title": model_id,
                "summary": compact_whitespace(". ".join(bit for bit in summary_bits if bit)),
                "source_name": source_name,
                "feed_url": huggingface_models_api_url(source, limit=1),
                "published_at": compact_whitespace(
                    str(raw.get("lastModified") or raw.get("createdAt") or raw.get("updatedAt") or "")
                ),
            }
        )
    return entries


def fetch_huggingface_model_entries(source: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    api_url = huggingface_models_api_url(source, limit=limit)
    if not api_url:
        return []
    print(f"[article] scanning {source.get('name')}: {api_url}")
    return parse_huggingface_model_entries(fetch_url_text(api_url), {**source, "api_url": api_url})


def build_article_x_search_prompt(source: dict[str, Any], config: dict[str, Any], limit: int) -> str:
    include = ", ".join(source.get("include_keywords") or config.get("include_keywords") or [])
    exclude = ", ".join(source.get("exclude_keywords") or config.get("exclude_keywords") or [])
    query = str(source.get("x_search_query") or source.get("query") or "trending AI technology news")
    lookback = int(source.get("lookback_hours") or config.get("article_lookback_hours") or 72)
    return f"""
You are finding source links for a daily AI/tech Instagram carousel.

Search X/Grok for: {query}

Return up to {limit} recent, article-like source links from the last {lookback} hours.
Prefer links with concrete AI product launches, model releases, benchmarks, research,
developer tooling, Philippine tech/business impact, or workforce impact.
Prefer these themes when relevant: {include or "AI, technology, model releases"}.
Avoid: {exclude or "low-signal promotions"}.

Return ONLY a raw JSON array. No markdown. Each object must have:
"title": "article or source title",
"summary": "one sentence explaining the signal",
"source_name": "publisher or account",
"published_at": "ISO date or readable date if available",
"url": "https://..."
""".strip()


def fetch_x_search_article_items(
    source: dict[str, Any],
    config: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    token = resolve_xai_token(required=False)
    if not token:
        print(f"[article] x_search unavailable for {source.get('name')}: no xAI token", file=sys.stderr)
        return []
    prompt = build_article_x_search_prompt(source, config, limit)
    try:
        text = xai_responses_text(prompt, token, timeout=120)
        data = extract_json_value(text, "[", "]")
    except SystemExit as exc:
        print(f"[article] x_search failed for {source.get('name')}: {exc}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        entries.append(
            {
                "url": re.sub(r"#.*$", "", url),
                "title": compact_whitespace(str(raw.get("title") or title_from_url(url))),
                "summary": strip_html(str(raw.get("summary") or "")),
                "source_name": compact_whitespace(str(raw.get("source_name") or source.get("name") or "X Trending")),
                "feed_url": str(source.get("x_search_query") or ""),
                "published_at": compact_whitespace(str(raw.get("published_at") or "")),
            }
        )
    return entries


def article_keyword_lists(
    source: dict[str, Any],
    config: dict[str, Any],
) -> tuple[list[str], list[str]]:
    include = list(config.get("include_keywords") or [])
    include.extend(source.get("include_keywords") or [])
    exclude = [] if source.get("ignore_global_exclude") else list(config.get("exclude_keywords") or [])
    exclude.extend(source.get("exclude_keywords") or [])
    return dedupe_lower(include), dedupe_lower(exclude)


def dedupe_lower(values: list[str]) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = str(value).strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        items.append(value)
    return items


def score_article_item(
    item: dict[str, Any],
    source: dict[str, Any],
    config: dict[str, Any],
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    title_text = str(item.get("title", "")).lower()
    text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    include_keywords, exclude_keywords = article_keyword_lists(source, config)

    score = int(source.get("base_score") or 20)
    matched = matched_terms(text, include_keywords, limit=6)
    if matched:
        score += min(30, 5 * len(matched))
        reasons.append("keywords: " + ", ".join(matched))

    signal_hits = matched_terms(text, ARTICLE_SIGNAL_TERMS)
    if signal_hits:
        score += min(18, 2 * len(signal_hits))
        reasons.append("signal terms")

    strong_hits = matched_terms(text, ARTICLE_STRONG_TERMS)
    if strong_hits:
        score += min(18, 3 * len(strong_hits))
        reasons.append("strong terms")

    # A hard signal in the headline outweighs the same word buried in the summary,
    # so reward strong terms that land in the title itself.
    title_strong = matched_terms(title_text, ARTICLE_STRONG_TERMS, limit=3)
    if title_strong:
        score += min(9, 3 * len(title_strong))
        reasons.append("strong signal in title")

    if re.search(r"\b\d+(?:\.\d+)?\s?(?:%(?!\w)|x\b|k\b|m\b|b\b|tokens?\b|parameters?\b|steps?\b|tasks?\b|calls?\b)", text, re.I):
        score += 10
        reasons.append("numbers")

    if re.search(r"\b(?:beats?|versus|vs\.?|outperform|surpass|compare|leaderboard)\b", text):
        score += 8
        reasons.append("comparison")

    if re.search(MODEL_RELEASE_PATTERN, text, re.I):
        score += 6
        reasons.append("named model release")

    if any(re.search(pattern, text, re.I) for pattern in LOW_SIGNAL_ARTICLE_PATTERNS):
        score -= 12
        reasons.append("low-signal format")

    excluded = matched_terms(text, exclude_keywords)
    if excluded:
        score -= 35
        reasons.append("excluded keyword: " + ", ".join(excluded[:3]))

    published = feed_item_datetime(item)
    if published:
        age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
        if age_hours <= 24:
            score += 6
            reasons.append("fresh")
        elif age_hours <= 72:
            score += 3

    score = max(0, min(100, score))
    if not reasons:
        reasons.append("article source match")
    return score, reasons


def normalize_article_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": str(item.get("url") or "").strip(),
        "title": compact_whitespace(str(item.get("title") or "")),
        "summary": compact_text(strip_html(str(item.get("summary") or "")), 700),
        "source_name": compact_whitespace(str(item.get("source_name") or "")),
        "published_at": compact_whitespace(str(item.get("published_at") or "")),
        "feed_url": str(item.get("feed_url") or "").strip(),
    }


def direct_url_article_items(source: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    feed_url = source_primary_url(source)
    for url in source.get("urls") or []:
        items.append(
            {
                "url": url,
                "title": title_from_url(url),
                "summary": "",
                "source_name": source.get("name") or "Article source",
                "feed_url": feed_url,
                "published_at": "",
            }
        )
    return items


def fetch_article_source_items(
    source: dict[str, Any],
    config: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    source_type = str(source.get("source_type") or "rss")
    source_items: list[dict[str, Any]] = []

    if source_type in {"rss", "atom", "feed"}:
        for feed_url in source.get("feed_urls") or [source.get("feed_url")]:
            feed_url = str(feed_url or "").strip()
            if not feed_url:
                continue
            print(f"[article] scanning {source.get('name')}: {feed_url}")
            feed_source = {**source, "feed_url": feed_url}
            source_items.extend(parse_feed_entries(fetch_url_text(feed_url), feed_source))
    elif source_type == "sitemap":
        print(f"[article] scanning {source.get('name')}: {source.get('sitemap_url')}")
        source_items.extend(fetch_sitemap_entries(source))
    elif source_type == "json_api":
        json_url = str(source.get("json_url") or "").strip()
        if json_url:
            print(f"[article] scanning {source.get('name')}: {json_url}")
            source_items.extend(parse_json_api_entries(fetch_url_text(json_url), source))
    elif source_type == "huggingface_models":
        source_items.extend(fetch_huggingface_model_entries(source, limit=limit))
    elif source_type == "x_search":
        source_items.extend(fetch_x_search_article_items(source, config, limit=limit))

    source_items.extend(direct_url_article_items(source))
    return source_items


def article_matches_keywords(item: dict[str, Any], keywords: list[str]) -> bool:
    text = f"{item.get('title', '')} {item.get('summary', '')} {item.get('url', '')}".lower()
    return bool(matched_terms(text, keywords))


def workforce_source_items(
    base_items: list[dict[str, Any]],
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    keywords = source.get("workforce_keywords") or sorted(WORKFORCE_TERMS)
    items: list[dict[str, Any]] = []
    for item in base_items:
        if not article_matches_keywords(item, keywords):
            continue
        items.append(
            {
                **item,
                "source_name": source.get("name") or "Workforce",
                "feed_url": str(item.get("feed_url") or ""),
            }
        )
    return items


def fetch_article_items(config: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    sources = config.get("article_sources") or []
    if not sources:
        return []

    items: list[dict[str, Any]] = []
    base_items: list[dict[str, Any]] = []
    workforce_sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    now = datetime.now(timezone.utc)

    def keep_source_items(source: dict[str, Any], source_items: list[dict[str, Any]]) -> None:
        nonlocal items, base_items
        source_type = str(source.get("source_type") or "rss")

        lookback_hours = int(source.get("lookback_hours") or config.get("article_lookback_hours") or 72)
        cutoff = now - timedelta(hours=lookback_hours)
        per_source_limit = int(source.get("max_items") or config.get("max_articles_per_source") or 5)
        kept_for_source = 0
        for raw_item in source_items:
            item = normalize_article_item(raw_item)
            url = item["url"]
            if not url:
                continue
            if url in seen_urls and source_type != "workforce":
                continue
            published = feed_item_datetime(item)
            if published and published < cutoff:
                continue
            if source_type != "workforce":
                seen_urls.add(url)
            item["_source_config"] = source
            items.append(item)
            if source_type != "workforce":
                base_items.append(item)
            kept_for_source += 1
            if kept_for_source >= per_source_limit:
                break

    for source in sources:
        source_type = str(source.get("source_type") or "rss")
        if source_type == "workforce":
            workforce_sources.append(source)
            continue
        keep_source_items(
            source,
            fetch_article_source_items(source, config, limit=limit),
        )

    for source in workforce_sources:
        keep_source_items(source, workforce_source_items(base_items, source))
    return items[:limit]


def merge_candidates(
    queue: dict[str, Any],
    posts: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    min_score: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_by_id = {
        str(candidate.get("id")): candidate for candidate in queue.get("candidates", [])
    }
    now = utc_now()
    discovered: list[dict[str, Any]] = []
    for post in posts:
        score, reasons = score_post(post, config)
        if score < min_score:
            continue
        cid = candidate_id(str(post["url"]))
        previous = existing_by_id.get(cid, {})
        candidate = {
            **previous,
            "id": cid,
            "source_type": previous.get("source_type") or "x_post",
            "status": previous.get("status") or "candidate",
            "score": score,
            "score_reasons": reasons,
            "source_account": post.get("handle", ""),
            "post": post,
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
        }
        existing_by_id[cid] = candidate
        discovered.append(candidate)

    queue["candidates"] = sorted(
        existing_by_id.values(),
        key=lambda item: (
            str(item.get("status") or ""),
            -int(item.get("score") or 0),
            str(item.get("created_at") or ""),
        ),
    )
    return discovered, queue["candidates"]


def merge_article_candidates(
    queue: dict[str, Any],
    articles: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    min_score: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_by_id = {
        str(candidate.get("id")): candidate for candidate in queue.get("candidates", [])
    }
    now = utc_now()
    discovered: list[dict[str, Any]] = []
    for article in articles:
        source_config = article.pop("_source_config", {}) if isinstance(article.get("_source_config"), dict) else {}
        score, reasons = score_article_item(article, source_config, config)
        if score < min_score:
            continue
        cid = article_candidate_id(str(article["url"]))
        previous = existing_by_id.get(cid, {})
        candidate = {
            **previous,
            "id": cid,
            "source_type": "article",
            "status": previous.get("status") or "candidate",
            "score": score,
            "score_reasons": reasons,
            "source_account": article.get("source_name", ""),
            "article": article,
            "created_at": previous.get("created_at") or now,
            "updated_at": now,
        }
        existing_by_id[cid] = candidate
        discovered.append(candidate)

    queue["candidates"] = sorted(
        existing_by_id.values(),
        key=lambda item: (
            str(item.get("status") or ""),
            -int(item.get("score") or 0),
            str(item.get("created_at") or ""),
        ),
    )
    return discovered, queue["candidates"]


def candidate_source_type(candidate: dict[str, Any]) -> str:
    source_type = str(candidate.get("source_type") or "")
    if source_type:
        return source_type
    if isinstance(candidate.get("article"), dict):
        return "article"
    return "x_post"


def format_candidate(candidate: dict[str, Any]) -> str:
    if candidate_source_type(candidate) == "article":
        article = candidate.get("article") or {}
        reasons = "; ".join(candidate.get("score_reasons") or [])
        source_name = article.get("source_name") or candidate.get("source_account") or "article"
        return (
            f"{candidate.get('id')} [{candidate.get('status')}] "
            f"score={candidate.get('score')} ARTICLE {source_name}\n"
            f"{compact_text(str(article.get('title') or ''), 240)}\n"
            f"{article.get('url', '')}\n"
            f"Why: {reasons}"
        ).strip()

    post = candidate.get("post") or {}
    reasons = "; ".join(candidate.get("score_reasons") or [])
    return (
        f"{candidate.get('id')} [{candidate.get('status')}] "
        f"score={candidate.get('score')} {post.get('handle', '')}\n"
        f"{compact_text(str(post.get('text') or ''), 240)}\n"
        f"{post.get('url', '')}\n"
        f"Why: {post.get('why') or reasons}"
    ).strip()


def filtered_candidates(
    queue: dict[str, Any],
    *,
    status: str | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    candidates = list(queue.get("candidates", []))
    if status:
        candidates = [item for item in candidates if item.get("status") == status]
    candidates.sort(
        key=lambda item: (-int(item.get("score") or 0), str(item.get("created_at") or "")),
    )
    if limit:
        candidates = candidates[:limit]
    return candidates


def find_candidate(queue: dict[str, Any], cid: str) -> dict[str, Any]:
    for candidate in queue.get("candidates", []):
        if candidate.get("id") == cid:
            return candidate
    raise SystemExit(f"No candidate found with id {cid}")


def extract_status_url(value: str) -> str:
    match = re.search(
        r"https://(?:www\.)?(?:x|twitter)\.com/[^\s<>()]+/status/\d+",
        value,
    )
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}")


def post_from_status_url(url: str, *, fallback_handle: str = "", fallback_text: str = "") -> dict[str, Any]:
    match = re.search(r"https://(?:www\.)?(?:x|twitter)\.com/([^/]+)/status/(\d+)", url)
    handle = normalize_handle(match.group(1)) if match else normalize_handle(fallback_handle)
    tweet_id = match.group(2) if match else ""
    return {
        "id": tweet_id,
        "text": fallback_text,
        "author": "",
        "handle": handle,
        "date": "",
        "likes": 0,
        "retweets": 0,
        "replies": 0,
        "views": 0,
        "has_video": False,
        "url": url.replace("twitter.com", "x.com"),
        "likes_fmt": "",
        "retweets_fmt": "",
        "replies_fmt": "",
        "views_fmt": "",
    }


def recover_candidate_from_callback(
    queue: dict[str, Any],
    cid: str,
    callback: dict[str, Any],
) -> dict[str, Any] | None:
    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    text = str(message.get("text") or message.get("caption") or "")
    url = extract_status_url(text)
    if not url:
        return None

    for candidate in queue.get("candidates", []):
        post = candidate.get("post") if isinstance(candidate.get("post"), dict) else {}
        if str(post.get("url") or "").replace("twitter.com", "x.com") == url.replace("twitter.com", "x.com"):
            print(
                f"[telegram] recovered missing callback id {cid} by matching URL to {candidate.get('id')}",
                flush=True,
            )
            return candidate

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    score = 0
    handle = ""
    body_lines: list[str] = []
    why = ""
    for line in lines:
        score_match = re.search(r"(\d+)\s+score\s+-\s+(@[A-Za-z0-9_]+)", line)
        if score_match:
            score = int(score_match.group(1))
            handle = score_match.group(2)
            continue
        if line.startswith(("Carousel candidate", "Why:")):
            if line.startswith("Why:"):
                why = line.removeprefix("Why:").strip()
            continue
        if extract_status_url(line):
            continue
        body_lines.append(line)

    post = post_from_status_url(
        url,
        fallback_handle=handle,
        fallback_text=" ".join(body_lines).strip(),
    )
    if why:
        post["why"] = why

    now = utc_now()
    candidate = {
        "id": cid,
        "status": "candidate",
        "score": score,
        "score_reasons": ["recovered from Telegram callback"],
        "source_account": post.get("handle", ""),
        "post": post,
        "created_at": now,
        "updated_at": now,
        "recovered_from_telegram": True,
        "telegram_message": {
            "chat_id": (message.get("chat") or {}).get("id") if isinstance(message.get("chat"), dict) else None,
            "message_id": message.get("message_id"),
        },
    }
    queue.setdefault("candidates", []).append(candidate)
    print(f"[telegram] recovered missing candidate {cid} from Telegram message URL {url}", flush=True)
    return candidate


def telegram_api(method: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not configured")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": SCOUT_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Telegram API error {exc.code}: {body[:400]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Telegram network error: {exc.reason}") from exc
    if not result.get("ok"):
        raise SystemExit(f"Telegram API returned an error: {result}")
    return result


def notify_telegram(candidate: dict[str, Any]) -> bool:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        print("[telegram] TELEGRAM_CHAT_ID is not configured; skipping notification")
        return False
    if candidate.get("telegram_notified_at"):
        return False

    reasons = "; ".join(candidate.get("score_reasons") or [])
    if candidate_source_type(candidate) == "article":
        article = candidate.get("article") or {}
        headline = f"{candidate.get('score')} score - ARTICLE {article.get('source_name', '')}".strip()
        body = compact_text(str(article.get("summary") or article.get("title") or ""), 700)
        url = str(article.get("url") or "")
        why = reasons
    else:
        post = candidate.get("post") or {}
        headline = f"{candidate.get('score')} score - {post.get('handle', '')}"
        body = compact_text(str(post.get("text") or ""), 700)
        url = str(post.get("url") or "")
        why = str(post.get("why") or reasons)
    text = "\n".join(
        [
            "Carousel candidate",
            headline,
            body,
            url,
            f"Why: {why}",
        ]
    )
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": False,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": "Approve & build",
                        "callback_data": f"approve_build:{candidate['id']}",
                    },
                    {
                        "text": "Reject",
                        "callback_data": f"reject:{candidate['id']}",
                    },
                ]
            ]
        },
    }
    result = telegram_api("sendMessage", payload)
    message = result.get("result", {})
    candidate["telegram_notified_at"] = utc_now()
    candidate["telegram_message"] = {
        "chat_id": message.get("chat", {}).get("id"),
        "message_id": message.get("message_id"),
    }
    return True


def is_expired_callback_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return (
        "400" in message
        and "query" in message
        and (
            "too old" in message
            or "response timeout expired" in message
            or "query id is invalid" in message
        )
    )


def answer_callback(callback_id: str, text: str) -> bool:
    try:
        telegram_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except SystemExit as exc:
        if not is_expired_callback_error(exc):
            raise
        print("[telegram] callback acknowledgement expired; continuing")
        return False
    return True


def build_candidate(
    candidate: dict[str, Any],
    *,
    builds_dir: Path,
    max_thread_posts: int,
    cookies_from_browser: str | None,
    thread_source: str,
    publish_instagram: bool,
    instagram_dry_run: bool,
    instagram_upload_r2: bool,
    instagram_media_base_url: str | None,
    instagram_caption: str | None,
    instagram_caption_file: Path | None,
    publish_buffer: bool,
    buffer_mode: str,
    buffer_dry_run: bool,
    buffer_upload_r2: bool,
    buffer_video_strategy: str,
    article_max_pages: int,
    article_min_score: int,
    article_curation_backend: str,
    article_no_title_enrichment: bool,
) -> int:
    out_dir = builds_dir / str(candidate["id"])
    source_type = candidate_source_type(candidate)
    if source_type == "article":
        article = candidate.get("article") or {}
        url = str(article.get("url") or "")
        if not url:
            raise SystemExit(f"Candidate {candidate.get('id')} has no article URL")
        builder = "build_article_carousel.py"
        cmd = [
            sys.executable,
            str(ROOT / builder),
            url,
            "--out-dir",
            str(out_dir),
            "--max-pages",
            str(article_max_pages),
            "--min-score",
            str(article_min_score),
            "--curation-backend",
            article_curation_backend,
        ]
        if article_no_title_enrichment:
            cmd.append("--no-title-enrichment")
    else:
        post = candidate.get("post") or {}
        url = str(post.get("url") or "")
        if not url:
            raise SystemExit(f"Candidate {candidate.get('id')} has no post URL")
        builder = "build_x_carousel.py"
        cmd = [
            sys.executable,
            str(ROOT / builder),
            url,
            "--out-dir",
            str(out_dir),
            "--max-thread-posts",
            str(max_thread_posts),
            "--thread-source",
            thread_source,
        ]
        if cookies_from_browser:
            cmd.extend(["--cookies-from-browser", cookies_from_browser])

    candidate["status"] = "approved"
    candidate["build_started_at"] = utc_now()
    candidate["build_dir"] = str(out_dir)
    print(f"[build] {candidate['id']} -> {out_dir}", flush=True)
    result = subprocess.run(cmd, check=False)
    candidate["build_finished_at"] = utc_now()
    candidate["build_returncode"] = result.returncode
    if result.returncode == 0:
        candidate["status"] = "built"
        candidate["manifest_path"] = str(out_dir / "manifest.json")
    else:
        candidate["status"] = "failed"
        candidate["failure"] = f"{builder} exited {result.returncode}"
        return result.returncode

    rc = 0
    if publish_instagram:
        publish_cmd = [
            sys.executable,
            str(ROOT / "instagram_publish.py"),
            str(out_dir / "manifest.json"),
        ]
        if instagram_dry_run:
            publish_cmd.append("--dry-run")
        if instagram_upload_r2:
            publish_cmd.append("--upload-r2")
        if instagram_media_base_url:
            publish_cmd.extend(["--media-base-url", instagram_media_base_url])
        if instagram_caption is not None:
            publish_cmd.extend(["--caption", instagram_caption])
        if instagram_caption_file:
            publish_cmd.extend(["--caption-file", str(instagram_caption_file)])

        candidate["instagram_publish_started_at"] = utc_now()
        candidate["instagram_publish_dry_run"] = instagram_dry_run
        print(
            f"[instagram] {'previewing' if instagram_dry_run else 'publishing'} "
            f"{candidate['id']}"
        )
        publish_result = subprocess.run(publish_cmd, check=False)
        candidate["instagram_publish_finished_at"] = utc_now()
        candidate["instagram_publish_returncode"] = publish_result.returncode
        candidate["instagram_publish_report_path"] = str(out_dir / "instagram_publish.json")
        if publish_result.returncode == 0:
            candidate["status"] = "publish_previewed" if instagram_dry_run else "published"
        else:
            candidate["status"] = "publish_failed"
            candidate["failure"] = f"instagram_publish.py exited {publish_result.returncode}"
        rc = publish_result.returncode

    if publish_buffer:
        rc = max(
            rc,
            publish_candidate_buffer(
                candidate,
                out_dir,
                mode=buffer_mode,
                dry_run=buffer_dry_run,
                upload_r2=buffer_upload_r2,
                video_strategy=buffer_video_strategy,
                media_base_url=instagram_media_base_url,
                caption=instagram_caption,
                caption_file=instagram_caption_file,
            ),
        )
    return rc


def publish_candidate_buffer(
    candidate: dict[str, Any],
    out_dir: Path,
    *,
    mode: str,
    dry_run: bool,
    upload_r2: bool,
    video_strategy: str,
    media_base_url: str | None,
    caption: str | None,
    caption_file: Path | None,
) -> int:
    buffer_cmd = [
        sys.executable,
        str(ROOT / "buffer_publish.py"),
        str(out_dir / "manifest.json"),
        "--mode",
        mode,
        "--video-strategy",
        video_strategy,
    ]
    if dry_run:
        buffer_cmd.append("--dry-run")
    if upload_r2:
        buffer_cmd.append("--upload-r2")
    if media_base_url:
        buffer_cmd.extend(["--media-base-url", media_base_url])
    if caption is not None:
        buffer_cmd.extend(["--caption", caption])
    if caption_file:
        buffer_cmd.extend(["--caption-file", str(caption_file)])

    candidate["buffer_publish_started_at"] = utc_now()
    candidate["buffer_publish_mode"] = mode
    candidate["buffer_publish_dry_run"] = dry_run
    print(f"[buffer] {'previewing' if dry_run else f'creating {mode} post for'} {candidate['id']}")
    result = subprocess.run(buffer_cmd, check=False)
    candidate["buffer_publish_finished_at"] = utc_now()
    candidate["buffer_publish_returncode"] = result.returncode
    candidate["buffer_publish_report_path"] = str(out_dir / "buffer_publish.json")
    if result.returncode != 0:
        candidate["status"] = "publish_failed"
        candidate["failure"] = f"buffer_publish.py exited {result.returncode}"
    elif dry_run:
        candidate["status"] = "buffer_previewed"
    elif mode == "draft":
        candidate["status"] = "buffer_drafted"
    elif mode == "queue":
        candidate["status"] = "buffer_queued"
    else:
        candidate["status"] = "published"
    return result.returncode


def scan_command(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    queue = load_queue(args.queue)
    posts: list[dict[str, Any]] = []
    if config.get("accounts"):
        posts = fetch_scout_posts(config, limit=args.limit)
    min_score = args.min_score if args.min_score is not None else config["min_score"]
    discovered_posts, _ = merge_candidates(queue, posts, config, min_score=min_score)

    articles = fetch_article_items(config, limit=args.article_limit)
    article_min_score = (
        args.article_min_score
        if args.article_min_score is not None
        else config["article_min_score"]
    )
    discovered_articles, _ = merge_article_candidates(
        queue,
        articles,
        config,
        min_score=article_min_score,
    )
    discovered = discovered_posts + discovered_articles

    notified = 0
    if args.notify:
        for candidate in discovered:
            if candidate.get("status") != "candidate":
                continue
            if notify_telegram(candidate):
                notified += 1

    save_queue(args.queue, queue)
    print(
        f"[scan] {len(posts)} posts fetched; {len(articles)} articles fetched; "
        f"{len(discovered)} queued; {notified} notified"
    )
    for candidate in discovered[: args.print_limit]:
        print()
        print(format_candidate(candidate))
    return 0


def list_command(args: argparse.Namespace) -> int:
    queue = load_queue(args.queue)
    candidates = filtered_candidates(queue, status=args.status, limit=args.limit)
    if args.json:
        print(json.dumps(candidates, indent=2, ensure_ascii=False))
        return 0
    if not candidates:
        print("No candidates found.")
        return 0
    for candidate in candidates:
        print(format_candidate(candidate))
        print()
    return 0


def build_kwargs_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "builds_dir": args.builds_dir,
        "max_thread_posts": args.max_thread_posts,
        "cookies_from_browser": args.cookies_from_browser,
        "thread_source": args.thread_source,
        "publish_instagram": args.publish_instagram,
        "instagram_dry_run": args.instagram_dry_run,
        "instagram_upload_r2": args.instagram_upload_r2,
        "instagram_media_base_url": args.instagram_media_base_url,
        "instagram_caption": args.instagram_caption,
        "instagram_caption_file": args.instagram_caption_file,
        "publish_buffer": args.publish_buffer,
        "buffer_mode": args.buffer_mode,
        "buffer_dry_run": args.buffer_dry_run,
        "buffer_upload_r2": args.buffer_upload_r2,
        "buffer_video_strategy": args.buffer_video_strategy,
        "article_max_pages": args.article_max_pages,
        "article_min_score": args.article_min_score_build,
        "article_curation_backend": args.article_curation_backend,
        "article_no_title_enrichment": args.article_no_title_enrichment,
    }


def approve_command(args: argparse.Namespace) -> int:
    queue = load_queue(args.queue)
    candidate = find_candidate(queue, args.candidate_id)
    candidate["status"] = "approved"
    candidate["approved_at"] = utc_now()
    candidate["updated_at"] = utc_now()
    rc = 0
    if args.run:
        rc = build_candidate(candidate, **build_kwargs_from_args(args))
    save_queue(args.queue, queue)
    return rc


def reject_command(args: argparse.Namespace) -> int:
    queue = load_queue(args.queue)
    candidate = find_candidate(queue, args.candidate_id)
    candidate["status"] = "rejected"
    candidate["rejected_at"] = utc_now()
    candidate["updated_at"] = utc_now()
    save_queue(args.queue, queue)
    print(f"[reject] {candidate['id']} rejected")
    return 0


def run_approved_command(args: argparse.Namespace) -> int:
    queue = load_queue(args.queue)
    candidates = filtered_candidates(queue, status="approved", limit=args.limit)
    if not candidates:
        print("[build] no approved candidates to run")
        return 0
    rc = 0
    for candidate in candidates:
        rc = max(rc, build_candidate(candidate, **build_kwargs_from_args(args)))
        save_queue(args.queue, queue)
    return rc


def load_telegram_state(path: Path) -> dict[str, Any]:
    return load_json_file(path, {"offset": 0})


def reset_telegram_offset(path: Path) -> None:
    write_json_file(path, {"offset": 0})
    print(f"[telegram] reset update offset in {path}", flush=True)


def process_telegram_updates(args: argparse.Namespace) -> int:
    queue = load_queue(args.queue)
    state = load_telegram_state(args.telegram_state)
    if getattr(args, "reset_offset", False):
        state["offset"] = 0
        args.reset_offset = False
    payload = {
        "offset": int(state.get("offset") or 0),
        "timeout": args.timeout,
        "allowed_updates": ["callback_query"],
    }
    print(
        f"[telegram] polling offset={payload['offset']} timeout={args.timeout}s",
        flush=True,
    )
    result = telegram_api("getUpdates", payload, timeout=max(30, args.timeout + 10))
    updates = result.get("result", [])
    processed = 0
    ignored = 0
    rc = 0
    print(f"[telegram] fetched {len(updates)} update(s)", flush=True)
    for update in updates:
        update_id = int(update.get("update_id") or 0)
        state["offset"] = max(int(state.get("offset") or 0), update_id + 1)
        callback = update.get("callback_query") or {}
        data = str(callback.get("data") or "")
        callback_id = str(callback.get("id") or "")
        if ":" not in data:
            ignored += 1
            print(f"[telegram] ignored callback with unexpected data={data!r}", flush=True)
            continue
        action, cid = data.split(":", 1)
        try:
            candidate = find_candidate(queue, cid)
        except SystemExit:
            candidate = recover_candidate_from_callback(queue, cid, callback)
            if not candidate:
                ignored += 1
                print(f"[telegram] ignored {action} for missing candidate {cid}", flush=True)
                if callback_id:
                    answer_callback(callback_id, "Candidate was not found")
                continue
        print(
            f"[telegram] callback action={action} candidate={cid} status={candidate.get('status')}",
            flush=True,
        )

        if action == "reject":
            if candidate.get("status") == "built":
                ignored += 1
                print(f"[telegram] ignored reject for already-built candidate {cid}", flush=True)
                if callback_id:
                    answer_callback(callback_id, "Already built; not rejected")
                continue
            if candidate.get("status") == "rejected":
                ignored += 1
                print(f"[telegram] ignored duplicate reject for {cid}", flush=True)
                if callback_id:
                    answer_callback(callback_id, "Already rejected")
                continue
            candidate["status"] = "rejected"
            candidate["rejected_at"] = utc_now()
            candidate["updated_at"] = utc_now()
            if callback_id:
                answer_callback(callback_id, "Rejected")
            processed += 1
        elif action == "approve_build":
            if candidate.get("status") == "built":
                ignored += 1
                print(f"[telegram] ignored approve for already-built candidate {cid}", flush=True)
                if callback_id:
                    answer_callback(callback_id, "Already built")
                continue
            candidate["status"] = "approved"
            candidate["approved_at"] = utc_now()
            candidate["updated_at"] = utc_now()
            if callback_id:
                answer_callback(callback_id, "Approved; build starting")
            if args.run:
                rc = max(rc, build_candidate(candidate, **build_kwargs_from_args(args)))
            processed += 1
        else:
            ignored += 1
            print(f"[telegram] ignored unknown callback action={action!r} candidate={cid}", flush=True)
    save_queue(args.queue, queue)
    write_json_file(args.telegram_state, state)
    print(
        f"[telegram] processed {processed} callback(s), ignored {ignored}; next offset={state.get('offset')}",
        flush=True,
    )
    return rc


def telegram_poll_command(args: argparse.Namespace) -> int:
    if args.reset_offset:
        reset_telegram_offset(args.telegram_state)
    if not args.watch:
        return process_telegram_updates(args)

    rc = 0
    print("[telegram] watching for approval callbacks", flush=True)
    while True:
        rc = max(rc, process_telegram_updates(args))
        time.sleep(args.interval)


def add_common_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--builds-dir", type=Path, default=DEFAULT_BUILDS_DIR)
    parser.add_argument("--max-thread-posts", type=int, default=8)
    parser.add_argument(
        "--cookies-from-browser",
        default=os.environ.get("X_COOKIES_FROM_BROWSER"),
        help="Pass browser cookies through to build_x_carousel.py",
    )
    parser.add_argument(
        "--thread-source",
        choices=("auto", "xai", "playwright"),
        default=os.environ.get("X_THREAD_SOURCE", "auto"),
    )
    parser.add_argument(
        "--publish-instagram",
        action="store_true",
        help="After a successful build, run instagram_publish.py",
    )
    parser.add_argument(
        "--instagram-dry-run",
        action="store_true",
        help="With --publish-instagram, validate and write an Instagram publish plan only",
    )
    parser.add_argument(
        "--instagram-upload-r2",
        action="store_true",
        help="With --publish-instagram, upload rendered carousel media to Cloudflare R2 first",
    )
    parser.add_argument(
        "--instagram-media-base-url",
        default=os.environ.get("INSTAGRAM_MEDIA_BASE_URL") or os.environ.get("IG_MEDIA_BASE_URL"),
        help="Public HTTPS base URL for rendered Instagram media files",
    )
    parser.add_argument(
        "--instagram-caption",
        help="Caption passed to instagram_publish.py",
    )
    parser.add_argument(
        "--instagram-caption-file",
        type=Path,
        help="Caption file passed to instagram_publish.py",
    )
    parser.add_argument(
        "--publish-buffer",
        action="store_true",
        help="After a successful build, run buffer_publish.py (creates a Buffer draft by default)",
    )
    parser.add_argument(
        "--buffer-mode",
        choices=("draft", "queue", "now"),
        default="draft",
        help="With --publish-buffer: draft for review in Buffer, queue to schedule, now to publish immediately",
    )
    parser.add_argument(
        "--buffer-dry-run",
        action="store_true",
        help="With --publish-buffer, validate and write the Buffer payload only",
    )
    parser.add_argument(
        "--buffer-no-upload-r2",
        dest="buffer_upload_r2",
        action="store_false",
        help="With --publish-buffer, skip uploading slides to R2 before posting",
    )
    parser.set_defaults(buffer_upload_r2=True)
    parser.add_argument(
        "--buffer-video-strategy",
        choices=("fail", "poster", "reel"),
        default="fail",
        help=(
            "How buffer_publish.py handles video slides in carousels; Buffer cannot mix "
            "video and images, so fail (default) aborts, poster uses stills, reel posts "
            "the video alone"
        ),
    )
    parser.add_argument(
        "--article-max-pages",
        type=int,
        default=6,
        help="For article candidates, maximum article-section slides",
    )
    parser.add_argument(
        "--article-min-score-build",
        type=int,
        default=6,
        help="For article candidates, section signal threshold passed to build_article_carousel.py",
    )
    parser.add_argument(
        "--article-curation-backend",
        choices=("auto", "gemini", "local"),
        default=os.environ.get("ARTICLE_CURATION_BACKEND", "auto"),
        help="For article candidates, curation backend passed to build_article_carousel.py",
    )
    parser.add_argument(
        "--article-no-title-enrichment",
        action="store_true",
        help="For article candidates, skip Gemini/OpenAI title enrichment",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scout, approve, and build carousel candidates")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan configured X accounts and article feeds")
    scan.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    scan.add_argument("--limit", type=int, default=20)
    scan.add_argument("--article-limit", type=int, default=20)
    scan.add_argument("--min-score", type=int)
    scan.add_argument("--article-min-score", type=int)
    scan.add_argument("--notify", action="store_true", help="Send new candidates to Telegram")
    scan.add_argument("--print-limit", type=int, default=5)
    scan.set_defaults(func=scan_command)

    list_parser = sub.add_parser("list", help="List queued candidates")
    list_parser.add_argument(
        "--status",
        choices=(
            "candidate",
            "approved",
            "rejected",
            "built",
            "failed",
            "publish_previewed",
            "buffer_previewed",
            "buffer_drafted",
            "buffer_queued",
            "published",
            "publish_failed",
        ),
    )
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=list_command)

    approve = sub.add_parser("approve", help="Approve a candidate and build it by default")
    approve.add_argument("candidate_id")
    approve.add_argument("--no-run", dest="run", action="store_false", help="Only mark approved")
    approve.set_defaults(func=approve_command, run=True)
    add_common_build_args(approve)

    reject = sub.add_parser("reject", help="Reject a candidate")
    reject.add_argument("candidate_id")
    reject.set_defaults(func=reject_command)

    run_approved = sub.add_parser("run-approved", help="Build approved candidates")
    run_approved.add_argument("--limit", type=int)
    run_approved.set_defaults(func=run_approved_command)
    add_common_build_args(run_approved)

    telegram = sub.add_parser("telegram-poll", help="Process Telegram approval callbacks")
    telegram.add_argument("--telegram-state", type=Path, default=DEFAULT_TELEGRAM_STATE)
    telegram.add_argument("--timeout", type=int, default=5)
    telegram.add_argument("--interval", type=int, default=10)
    telegram.add_argument("--watch", action="store_true")
    telegram.add_argument(
        "--reset-offset",
        action="store_true",
        help="Forget the saved Telegram update offset and replay pending callbacks",
    )
    telegram.add_argument("--no-run", dest="run", action="store_false", help="Approve without building")
    telegram.set_defaults(func=telegram_poll_command, run=True)
    add_common_build_args(telegram)

    return parser


def main() -> int:
    load_env_file(ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
