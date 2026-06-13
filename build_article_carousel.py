#!/usr/bin/env python3
"""Create a branded carousel from a long-form article URL.

The workflow mirrors build_x_carousel.py, but the source is an article:

    uv run python build_article_carousel.py https://example.com/story

Outputs go to out/article_carousel by default. The first slide is the same
LLMAW title-cover style used by the X workflow. Remaining slides are selected
from high-signal article sections only: concrete facts, benchmarks, launches,
technical details, strategy shifts, and business implications.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from build_video_slide import clean_post_text
from build_x_carousel import (
    DEFAULT_ACCOUNT_NAME,
    build_title_enrichment,
    dot_markup,
    download_image,
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

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "out" / "article_carousel"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 carousel-app/1.0"
)

BLOCK_TAGS = {"p", "h1", "h2", "h3", "li", "blockquote"}
SKIP_TAGS = {
    "aside",
    "button",
    "canvas",
    "footer",
    "form",
    "header",
    "iframe",
    "nav",
    "noscript",
    "select",
    "style",
    "svg",
    "template",
}

ARTICLE_TYPES = {
    "article",
    "newsarticle",
    "blogposting",
    "report",
    "analysisnewsarticle",
}

BOILERPLATE_PATTERNS = [
    r"\baccept cookies\b",
    r"\badvertisement\b",
    r"\ball rights reserved\b",
    r"\bclick here\b",
    r"\bcontact us\b",
    r"\bcookie policy\b",
    r"\bdaily newsletter\b",
    r"\bdownload our app\b",
    r"\bfollow us\b",
    r"\bjoin us\b",
    r"\bprivacy policy\b",
    r"\bread more\b",
    r"\brecommended for you\b",
    r"\bregister now\b",
    r"\bshare this\b",
    r"\bsign in\b",
    r"\bsign up\b",
    r"\bsubscribe\b",
    r"\bterms of service\b",
    r"\bthis website uses cookies\b",
    r"\bwe may receive compensation\b",
]

SIGNAL_TERMS = {
    "agent",
    "agentic",
    "ai",
    "api",
    "architecture",
    "available",
    "benchmark",
    "benchmarks",
    "beats",
    "coding",
    "competition",
    "context",
    "cost",
    "dataset",
    "developer",
    "evaluation",
    "framework",
    "github",
    "harness",
    "latency",
    "launch",
    "launched",
    "license",
    "model",
    "open source",
    "open-source",
    "outperform",
    "outperformed",
    "outperforms",
    "parameters",
    "pricing",
    "reasoning",
    "release",
    "released",
    "repo",
    "research",
    "score",
    "scores",
    "swe",
    "tasks",
    "technical",
    "tool",
    "training",
}

STRONG_SIGNAL_TERMS = {
    "benchmark",
    "benchmarks",
    "beats",
    "github",
    "license",
    "open source",
    "open-source",
    "outperform",
    "outperformed",
    "outperforms",
    "score",
    "scores",
    "swe-bench",
    "tool calls",
}


@dataclass
class TextBlock:
    role: str
    text: str
    index: int


@dataclass
class Article:
    source: str
    url: str
    title: str
    description: str
    site_name: str
    author: str
    published_at: str
    image_url: str
    blocks: list[TextBlock] = field(default_factory=list)
    canonical_url: str = ""


@dataclass
class CandidateSection:
    index: int
    title: str
    body: str
    score: int
    reasons: list[str]
    block_indices: list[int]


@dataclass
class CarouselPage:
    index: int
    kicker: str
    headline: str
    body: str
    stat: str
    source_heading: str
    source_indices: list[int]
    score: int
    why: str


class ArticleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[TextBlock] = []
        self.meta: dict[str, str] = {}
        self.link: dict[str, str] = {}
        self.json_ld: list[str] = []
        self._skip_depth = 0
        self._active_role = ""
        self._active_parts: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}

        if tag == "meta":
            key = attr_map.get("property") or attr_map.get("name") or attr_map.get("itemprop")
            content = attr_map.get("content", "")
            if key and content:
                self.meta[key.lower()] = normalize_space(content)
            return

        if tag == "link":
            rel = attr_map.get("rel", "").lower()
            href = attr_map.get("href", "")
            if href:
                for rel_name in rel.split():
                    self.link[rel_name] = href
            return

        if tag == "script":
            script_type = attr_map.get("type", "").lower()
            if "ld+json" in script_type:
                self._in_json_ld = True
                self._json_ld_parts = []
            else:
                self._skip_depth += 1
            return

        if tag == "title":
            self._in_title = True
            self._title_parts = []
            return

        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth:
            return

        if tag in BLOCK_TAGS:
            self._flush_active()
            self._active_role = tag
            self._active_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            if self._in_json_ld:
                text = "".join(self._json_ld_parts).strip()
                if text:
                    self.json_ld.append(text)
                self._json_ld_parts = []
                self._in_json_ld = False
            elif self._skip_depth:
                self._skip_depth -= 1
            return

        if tag == "title":
            text = normalize_space(" ".join(self._title_parts))
            if text:
                self.meta.setdefault("title", text)
            self._in_title = False
            return

        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return

        if tag == self._active_role:
            self._flush_active()

    def handle_data(self, data: str) -> None:
        if self._in_json_ld:
            self._json_ld_parts.append(data)
            return
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._active_role:
            self._active_parts.append(data)

    def close(self) -> None:
        self._flush_active()
        super().close()

    def _flush_active(self) -> None:
        if not self._active_role:
            return
        text = clean_article_text(" ".join(self._active_parts))
        if text and is_useful_block(text, self._active_role):
            self.blocks.append(TextBlock(self._active_role, text, len(self.blocks)))
        self._active_role = ""
        self._active_parts = []


def normalize_space(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def clean_article_text(value: str) -> str:
    text = normalize_space(value)
    text = re.sub(r"^\s*(?:Image Credits?|Credit|Source):\s*.+$", "", text, flags=re.I)
    return text.strip()


def is_boilerplate(text: str) -> bool:
    lowered = text.lower()
    if any(re.search(pattern, lowered) for pattern in BOILERPLATE_PATTERNS):
        return True
    if lowered in {"x", "facebook", "linkedin", "copy link", "email", "print"}:
        return True
    if len(text) < 3:
        return True
    return False


def is_useful_block(text: str, role: str) -> bool:
    if is_boilerplate(text):
        return False
    word_count = count_words(text)
    if role in {"h1", "h2", "h3"}:
        return len(text) >= 3 and 1 <= word_count <= 20
    if role == "li":
        return 5 <= word_count <= 80
    return 10 <= word_count <= 220


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'._+-]*", text))


def first_meta(meta: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = meta.get(key.lower())
        if value:
            return value
    return ""


def display_domain(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc or parsed.path
    host = host.lower().removeprefix("www.")
    return host or "source article"


def source_to_url(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https", "file"}:
        return source
    path = Path(source).expanduser()
    if path.exists():
        return path.resolve().as_uri()
    return source


def read_source(source: str, *, timeout: int) -> tuple[str, str]:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        request = Request(
            source,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise SystemExit("article HTML is larger than the 8 MB safety limit")
                charset = response.headers.get_content_charset() or "utf-8"
                final_url = response.geturl()
        except HTTPError as exc:
            raise SystemExit(f"article fetch returned HTTP {exc.code}: {source}") from exc
        except URLError as exc:
            raise SystemExit(f"article fetch failed: {exc.reason}") from exc
        return raw.decode(charset, errors="replace"), final_url

    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
    else:
        path = Path(source).expanduser()
    if not path.exists():
        raise SystemExit(f"article source is not a URL or local file: {source}")
    return path.read_text(encoding="utf-8", errors="replace"), path.resolve().as_uri()


def jsonld_objects(raw_items: list[str]) -> list[Any]:
    objects: list[Any] = []
    for raw in raw_items:
        raw = raw.strip()
        if not raw:
            continue
        try:
            objects.append(json.loads(raw))
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                try:
                    objects.append(json.loads(raw[start : end + 1]))
                except json.JSONDecodeError:
                    pass
    return objects


def flatten_jsonld(value: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(value, list):
        for child in value:
            items.extend(flatten_jsonld(child))
    elif isinstance(value, dict):
        items.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list):
            for child in graph:
                items.extend(flatten_jsonld(child))
    return items


def jsonld_type_set(item: dict[str, Any]) -> set[str]:
    raw = item.get("@type") or item.get("type")
    values = raw if isinstance(raw, list) else [raw]
    return {str(value).lower() for value in values if value}


def first_article_jsonld(raw_items: list[str]) -> dict[str, Any]:
    for raw_obj in jsonld_objects(raw_items):
        for item in flatten_jsonld(raw_obj):
            if jsonld_type_set(item) & ARTICLE_TYPES:
                return item
    return {}


def jsonld_text(value: Any) -> str:
    if isinstance(value, str):
        return normalize_space(value)
    if isinstance(value, dict):
        for key in ("name", "headline", "url"):
            text = jsonld_text(value.get(key))
            if text:
                return text
    if isinstance(value, list):
        parts = [jsonld_text(item) for item in value]
        return ", ".join(part for part in parts if part)
    return ""


def jsonld_image(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("url", "contentUrl"):
            if isinstance(value.get(key), str):
                return str(value[key])
    if isinstance(value, list):
        for item in value:
            image = jsonld_image(item)
            if image:
                return image
    return ""


def article_body_blocks(article_body: str) -> list[TextBlock]:
    blocks: list[TextBlock] = []
    for part in re.split(r"\n{2,}|(?<=[.!?])\s+(?=[A-Z0-9])", article_body):
        text = clean_article_text(part)
        if is_useful_block(text, "p"):
            blocks.append(TextBlock("p", text, len(blocks)))
    return blocks


def parse_article(source: str, html_text: str, final_url: str) -> Article:
    parser = ArticleHTMLParser()
    parser.feed(html_text)
    parser.close()

    jsonld = first_article_jsonld(parser.json_ld)
    meta = parser.meta
    base_url = final_url or source_to_url(source)
    canonical = parser.link.get("canonical", "")
    if canonical:
        canonical = urljoin(base_url, canonical)
    jsonld_url = str(jsonld.get("url") or "")
    url = canonical or (urljoin(base_url, jsonld_url) if jsonld_url else "") or base_url

    title = (
        jsonld_text(jsonld.get("headline"))
        or first_meta(meta, "og:title", "twitter:title", "title")
        or "Source article"
    )
    title = re.sub(r"\s+[|-]\s+[^|-]{2,70}$", "", title).strip() or title

    description = (
        jsonld_text(jsonld.get("description"))
        or first_meta(meta, "og:description", "twitter:description", "description")
    )
    site_name = first_meta(meta, "og:site_name", "application-name") or display_domain(url)
    author = (
        jsonld_text(jsonld.get("author"))
        or first_meta(meta, "author", "article:author", "parsely-author", "twitter:creator")
    )
    published_at = (
        jsonld_text(jsonld.get("datePublished"))
        or first_meta(meta, "article:published_time", "date", "datepublished", "pubdate")
    )
    image_url = jsonld_image(jsonld.get("image")) or first_meta(meta, "og:image", "twitter:image")
    if image_url:
        image_url = urljoin(url, image_url)

    blocks = parser.blocks
    body = jsonld_text(jsonld.get("articleBody"))
    if body and count_words(body) > count_words(" ".join(block.text for block in blocks)):
        blocks = article_body_blocks(body)

    blocks = dedupe_blocks(blocks, title)
    return Article(
        source=source,
        url=url,
        title=title,
        description=description,
        site_name=site_name,
        author=author,
        published_at=published_at,
        image_url=image_url,
        blocks=blocks,
        canonical_url=canonical,
    )


def dedupe_blocks(blocks: list[TextBlock], title: str) -> list[TextBlock]:
    clean_blocks: list[TextBlock] = []
    seen: set[str] = set()
    title_key = normalized_text_key(title)
    for block in blocks:
        text = block.text
        key = normalized_text_key(text)
        if not key or key in seen:
            continue
        if key == title_key and block.role != "h1":
            continue
        seen.add(key)
        clean_blocks.append(TextBlock(block.role, text, len(clean_blocks)))
    return clean_blocks


def normalized_text_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", text)
    sentences = [normalize_space(part) for part in parts if normalize_space(part)]
    return sentences or [normalize_space(text)]


def clamp_words(text: str, limit: int) -> str:
    words = re.findall(r"\S+", text)
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]).rstrip(" ,;:") + "..."


def sentence_word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def finish_sentence(text: str) -> str:
    text = normalize_space(text).rstrip(" ,;:")
    if not text:
        return text
    if text[-1] not in ".!?":
        text += "."
    return text


def shorten_sentence(text: str, limit: int) -> str:
    if sentence_word_count(text) <= limit:
        return text

    for pattern in (r"\s+(?:--|\u2013|\u2014)\s+", r";\s+", r":\s+"):
        parts = [part.strip() for part in re.split(pattern, text) if part.strip()]
        if len(parts) <= 1:
            continue
        first = parts[0]
        if 10 <= sentence_word_count(first) <= limit:
            return finish_sentence(first)

    comma_parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(comma_parts) > 1:
        selected: list[str] = []
        words = 0
        for part in comma_parts:
            part_words = sentence_word_count(part)
            if selected and words + part_words > limit:
                break
            selected.append(part)
            words += part_words
        candidate = ", ".join(selected)
        if 10 <= sentence_word_count(candidate) <= limit:
            return finish_sentence(candidate)

    return clamp_words(text, limit)


def compact_headline(text: str, limit: int = 9) -> str:
    text = normalize_space(text)
    text = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.I)
    text = re.sub(r"[:.;,]\s*$", "", text)
    text = clamp_words(text, limit)
    if text and text[:1].islower():
        text = text[:1].upper() + text[1:]
    return text


def section_signal_score(title: str, body: str) -> tuple[int, list[str]]:
    text = f"{title} {body}".lower()
    score = 0
    reasons: list[str] = []
    words = count_words(body)

    if 28 <= words <= 170:
        score += 2
    elif 15 <= words <= 220:
        score += 1
    else:
        score -= 2

    number_matches = re.findall(
        r"\b\d+(?:\.\d+)?[-\s]?(?:%|x|k|m|b|bn|million|billion|trillion|tokens?|steps?|tasks?|parameters?|calls?)?\b",
        text,
        flags=re.I,
    )
    if number_matches:
        score += min(5, 2 + len(number_matches))
        reasons.append("numbers")

    strong_hits = [term for term in STRONG_SIGNAL_TERMS if term in text]
    if strong_hits:
        score += min(6, 2 * len(strong_hits))
        reasons.append("strong terms")

    signal_hits = [term for term in SIGNAL_TERMS if term in text]
    if signal_hits:
        score += min(5, len(signal_hits))
        reasons.append("topic terms")

    if re.search(r"\b(?:beats?|vs\.?|versus|compared|outperform|surpass|exceed)\b", text):
        score += 3
        reasons.append("comparison")

    if re.search(r"\b(?:new|launch(?:ed|es)?|release(?:d|s)?|announc(?:ed|es))\b", text):
        score += 2
        reasons.append("news")

    if re.search(r"\b(?:github|hugging face|paper|technical report|open-source|open source)\b", text):
        score += 2
        reasons.append("distribution")

    if title.strip().lower() in {"background", "context"} and not number_matches and not strong_hits:
        score -= 4

    if re.search(r"\b(?:said|told|according to)\b", text) and not number_matches and len(signal_hits) < 2:
        score -= 1

    if is_boilerplate(body) or is_boilerplate(title):
        score -= 8
        reasons.append("boilerplate")

    return score, reasons


def build_candidate_sections(article: Article) -> list[CandidateSection]:
    candidates: list[CandidateSection] = []
    current_heading = ""
    pending: list[TextBlock] = []
    pending_heading = ""

    def flush() -> None:
        nonlocal pending, pending_heading
        if not pending:
            return
        body = " ".join(block.text for block in pending)
        score, reasons = section_signal_score(pending_heading, body)
        candidates.append(
            CandidateSection(
                index=len(candidates),
                title=pending_heading,
                body=body,
                score=score,
                reasons=reasons,
                block_indices=[block.index for block in pending],
            )
        )
        pending = []
        pending_heading = current_heading

    for block in article.blocks:
        if block.role in {"h1", "h2", "h3"}:
            flush()
            current_heading = block.text
            pending_heading = current_heading
            continue

        if not pending:
            pending_heading = current_heading
        if pending and count_words(" ".join(item.text for item in pending)) + count_words(block.text) > 150:
            flush()
            pending_heading = current_heading
        pending.append(block)

        if block.role == "blockquote" or count_words(" ".join(item.text for item in pending)) >= 90:
            flush()

    flush()

    deduped: list[CandidateSection] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = normalized_text_key(candidate.body[:260])
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def stat_from_text(text: str) -> str:
    patterns = [
        r"\b\d+(?:\.\d+)?\s?%(?!\w)",
        r"\b\d+(?:\.\d+)?[-\s]?(?:x|X)\b",
        r"\b\d+(?:\.\d+)?[-\s]?(?:K|M|B|bn|million|billion|trillion)\b",
        r"\b\d+(?:\.\d+)?[-\s]?(?:tokens?|steps?|tasks?|parameters?|tool calls?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            return normalize_space(match.group(0))[:28]
    return ""


def kicker_for_text(text: str) -> str:
    lowered = text.lower()
    if "benchmark" in lowered or "score" in lowered or "swe" in lowered:
        return "BENCHMARK"
    if "open-source" in lowered or "open source" in lowered or "github" in lowered:
        return "OPEN SOURCE"
    if "agent" in lowered or "coding" in lowered or "harness" in lowered:
        return "AGENTIC CODING"
    if "license" in lowered or "pricing" in lowered or "cost" in lowered:
        return "DISTRIBUTION"
    if re.search(r"\b(?:launch|released|announced|new)\b", lowered):
        return "THE NEWS"
    return "THE SIGNAL"


def sentence_score(sentence: str) -> int:
    score, _ = section_signal_score("", sentence)
    if len(sentence) > 260:
        score -= 2
    return score


def local_page_from_candidate(candidate: CandidateSection) -> CarouselPage:
    sentences = split_sentences(candidate.body)
    ranked = sorted(enumerate(sentences), key=lambda item: sentence_score(item[1]), reverse=True)
    fitting_ranked = [item for item in ranked if count_words(item[1]) <= 42]
    selection_pool = fitting_ranked or ranked
    chosen_indices: list[int] = []
    chosen_word_count = 0
    for index, sentence in selection_pool[:4]:
        sentence_words = count_words(sentence)
        if chosen_indices and chosen_word_count + sentence_words > 42:
            continue
        chosen_indices.append(index)
        chosen_word_count += sentence_words
        if chosen_word_count >= 30:
            break
    if not chosen_indices and selection_pool:
        chosen_indices = [selection_pool[0][0]]
    chosen_indices = sorted(chosen_indices)
    body = " ".join(sentences[index] for index in chosen_indices).strip()
    if not body:
        body = candidate.body
    if sentence_word_count(body) > 42:
        body = shorten_sentence(body, 42)
    body = clamp_words(body, 42)
    title = candidate.title
    if not title or count_words(title) < 2 or normalized_text_key(title) == normalized_text_key(body[:80]):
        title = sentences[chosen_indices[0]] if chosen_indices else candidate.body
    headline = compact_headline(title, 8)
    text_for_kicker = f"{headline} {body}"
    return CarouselPage(
        index=candidate.index,
        kicker=kicker_for_text(text_for_kicker),
        headline=headline,
        body=body,
        stat=stat_from_text(text_for_kicker),
        source_heading=candidate.title,
        source_indices=candidate.block_indices,
        score=candidate.score,
        why=", ".join(candidate.reasons[:3]),
    )


def local_curate_pages(
    candidates: list[CandidateSection],
    *,
    max_pages: int,
    min_score: int,
) -> list[CarouselPage]:
    filtered = [candidate for candidate in candidates if candidate.score >= min_score]
    filtered.sort(key=lambda candidate: (-candidate.score, candidate.index))
    ordered = sorted(filtered[:max_pages], key=lambda candidate: candidate.index)
    return [local_page_from_candidate(candidate) for candidate in ordered]


def candidate_prompt_payload(candidates: list[CandidateSection], limit: int = 26) -> list[dict[str, Any]]:
    ranked = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.index))[:limit]
    ranked = sorted(ranked, key=lambda candidate: candidate.index)
    payload: list[dict[str, Any]] = []
    for candidate in ranked:
        payload.append(
            {
                "index": candidate.index,
                "heading": candidate.title,
                "text": candidate.body[:1200],
                "local_score": candidate.score,
                "local_reasons": candidate.reasons,
            }
        )
    return payload


def gemini_curate_pages(
    article: Article,
    candidates: list[CandidateSection],
    *,
    max_pages: int,
    min_score: int,
) -> list[CarouselPage]:
    api_key = gemini_api_key()
    if not api_key:
        return []

    model = gemini_text_model()
    prompt = f"""
You are an editorial producer turning one article into an Instagram carousel.
Choose only the highest-signal article sections: concrete technical facts,
benchmarks, launches, open-source details, adoption signals, pricing,
strategic stakes, or credible quantified claims.

Return JSON only with this exact shape:
{{
  "pages": [
    {{
      "source_indices": [0],
      "kicker": "BENCHMARK",
      "headline": "short headline, 3 to 8 words",
      "body": "paraphrased slide copy, 18 to 38 words",
      "stat": "optional number chip, max 22 chars",
      "why": "short reason this is high signal"
    }}
  ]
}}

Rules:
- Pick 2 to {max_pages} pages.
- Do not copy long article wording. Paraphrase tightly.
- Each page must stand on a specific fact, benchmark, technical detail, or implication.
- Skip intro fluff, event promos, newsletter language, author bio, generic quotes, and background unless it changes the story.
- No markdown, citations, extra keys, hashtags, emojis, or quotation marks around the body.

Article:
Title: {article.title}
Source: {article.site_name}
Description: {article.description}

Candidate sections:
{json.dumps(candidate_prompt_payload(candidates), ensure_ascii=False)}
""".strip()

    payload: dict[str, object] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    response = gemini_generate_content(
        model,
        api_key,
        payload,
        api_version=os.environ.get("GEMINI_TEXT_API_VERSION") or "v1beta",
        timeout=45,
    )
    parsed = parse_json_object(extract_gemini_text(response))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("pages"), list):
        return []

    by_index = {candidate.index: candidate for candidate in candidates}
    pages: list[CarouselPage] = []
    for raw_page in parsed["pages"]:
        if not isinstance(raw_page, dict):
            continue
        source_indices_raw = raw_page.get("source_indices")
        if not isinstance(source_indices_raw, list):
            continue
        source_indices = []
        source_score = 0
        source_heading = ""
        for value in source_indices_raw:
            try:
                source_index = int(value)
            except (TypeError, ValueError):
                continue
            candidate = by_index.get(source_index)
            if not candidate:
                continue
            source_indices.extend(candidate.block_indices)
            source_score = max(source_score, candidate.score)
            if not source_heading and candidate.title:
                source_heading = candidate.title
        headline = string_value(raw_page.get("headline"))
        body = string_value(raw_page.get("body"))
        if not headline or not body or source_score < min_score:
            continue
        if count_words(body) > 48:
            body = clamp_words(body, 42)
        pages.append(
            CarouselPage(
                index=len(pages),
                kicker=string_value(raw_page.get("kicker"))[:24].upper() or kicker_for_text(body),
                headline=compact_headline(headline, 9),
                body=body,
                stat=string_value(raw_page.get("stat"))[:28],
                source_heading=source_heading,
                source_indices=source_indices,
                score=source_score,
                why=string_value(raw_page.get("why")),
            )
        )
        if len(pages) >= max_pages:
            break
    return pages


def curate_pages(
    article: Article,
    candidates: list[CandidateSection],
    *,
    max_pages: int,
    min_score: int,
    backend: str,
) -> tuple[list[CarouselPage], str]:
    if backend in {"auto", "gemini"}:
        pages = gemini_curate_pages(
            article,
            candidates,
            max_pages=max_pages,
            min_score=min_score,
        )
        if pages:
            return pages, "gemini"
        if backend == "gemini":
            print("[article] Gemini curation unavailable or empty; using local scoring fallback")
    pages = local_curate_pages(candidates, max_pages=max_pages, min_score=min_score)
    return pages, "local"


def article_as_post(article: Article) -> dict[str, str]:
    text = article.description or " ".join(block.text for block in article.blocks[:3]) or article.title
    return {
        "url": article.url,
        "id": hashlib.sha1(article.url.encode("utf-8")).hexdigest()[:12],
        "author": article.author or article.site_name or display_domain(article.url),
        "handle": "",
        "text": clean_post_text(f"{article.title}. {text}"),
        "date": article.published_at,
        "views": "",
        "likes": "",
        "reposts": "",
        "replies": "",
    }


def maybe_add_article_image(title_context: dict[str, Any], article: Article, out_dir: Path) -> None:
    if title_context.get("topic_image_path") or not article.image_url:
        return
    image_path = download_image(article.image_url, out_dir / "title_assets", "article-og-image")
    if image_path:
        title_context["topic_image_path"] = image_path
        title_context["image_provider"] = "article_og_image"


def slide_font_sizes(headline: str, body: str) -> tuple[int, int]:
    headline_len = len(headline)
    body_words = count_words(body)
    headline_size = 78
    if headline_len > 72:
        headline_size = 58
    elif headline_len > 52:
        headline_size = 64
    elif headline_len > 36:
        headline_size = 70

    body_size = 38
    if body_words > 40 or len(body) > 260:
        body_size = 31
    elif body_words > 32 or len(body) > 210:
        body_size = 34
    return headline_size, body_size


def source_label(article: Article) -> str:
    bits = [article.site_name or display_domain(article.url)]
    if article.published_at:
        bits.append(article.published_at[:10])
    return " / ".join(bits)


def render_article_slide(
    article: Article,
    page: CarouselPage,
    out_path: Path,
    active: int,
    count: int,
) -> Path:
    html_path = out_path.with_suffix(".html")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    safe_kicker = html.escape(page.kicker or "THE SIGNAL")
    safe_headline = html.escape(page.headline)
    safe_body = html.escape(page.body)
    safe_stat = html.escape(page.stat)
    safe_source = html.escape(source_label(article).upper())
    safe_heading = html.escape(page.source_heading)
    headline_size, body_size = slide_font_sizes(page.headline, page.body)
    stat_markup = f'<div class="stat-chip">{safe_stat}</div>' if page.stat else ""
    heading_key = normalized_text_key(page.source_heading)
    headline_key = normalized_text_key(page.headline)
    show_heading = bool(
        page.source_heading
        and heading_key
        and headline_key
        and heading_key != headline_key
        and not heading_key.endswith(headline_key)
    )
    heading_markup = f'<div class="source-heading">{safe_heading}</div>' if show_heading else ""

    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{shared_css()}
.article-source {{
  position: absolute;
  top: 76px;
  left: 72px;
  right: 72px;
  display: flex;
  align-items: center;
  gap: 18px;
  color: var(--primary);
}}
.article-source::before {{
  content: '';
  width: 44px;
  height: 4px;
  background: var(--primary);
}}
.article-source span {{
  font-size: 23px;
  font-weight: 820;
  letter-spacing: 0;
  text-transform: uppercase;
}}
.kicker {{
  top: 170px;
}}
.signal {{
  position: absolute;
  top: 266px;
  left: 72px;
  right: 72px;
  bottom: 178px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}}
.source-heading {{
  margin-bottom: 24px;
  max-width: 820px;
  color: rgba(20, 18, 14, 0.62);
  font-size: 25px;
  line-height: 1.18;
  font-weight: 760;
}}
.headline {{
  max-width: 900px;
  font-size: {headline_size}px;
  line-height: 1.02;
  font-weight: 870;
  letter-spacing: 0;
  color: var(--fg);
}}
.rule {{
  width: 100%;
  height: 2px;
  margin: 36px 0 34px;
  background: var(--rule);
}}
.body {{
  max-width: 900px;
  color: var(--ink-soft);
  font-size: {body_size}px;
  line-height: 1.28;
  font-weight: 620;
}}
.stat-chip {{
  align-self: flex-start;
  margin-top: 34px;
  padding: 14px 18px 13px;
  border: 2px solid var(--primary);
  color: var(--primary);
  font-size: 30px;
  line-height: 1;
  font-weight: 850;
  letter-spacing: 0;
  text-transform: uppercase;
}}
.source-label {{
  position: absolute;
  left: 96px;
  right: 96px;
  bottom: 112px;
  text-align: center;
  font-size: 21px;
  line-height: 1.2;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--primary);
}}
.dots {{
  bottom: 62px;
}}
</style></head>
<body>
<div class="slide">
  <div class="article-source"><span>ARTICLE</span></div>
  <div class="kicker"><em>{safe_kicker}</em></div>
  <div class="signal">
    {heading_markup}
    <h1 class="headline">{safe_headline}</h1>
    <div class="rule"></div>
    <div class="body">{safe_body}</div>
    {stat_markup}
  </div>
  <div class="source-label">{safe_source}</div>
  <div class="dots">{dot_markup(active, count)}</div>
</div>
</body></html>"""
    html_path.write_text(html_text)
    render_html_slide(html_path, out_path)
    return out_path


def page_manifest(page: CarouselPage, slide_path: Path, article: Article, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "type": "article-section",
        "path": str(slide_path),
        "source_url": article.url,
        "kicker": page.kicker,
        "headline": page.headline,
        "body": page.body,
        "stat": page.stat,
        "source_heading": page.source_heading,
        "source_indices": page.source_indices,
        "score": page.score,
        "why": page.why,
    }


def manifest_article(article: Article) -> dict[str, Any]:
    return {
        "url": article.url,
        "canonical_url": article.canonical_url,
        "title": article.title,
        "description": article.description,
        "site_name": article.site_name,
        "author": article.author,
        "published_at": article.published_at,
        "image_url": article.image_url,
        "block_count": len(article.blocks),
    }


def manifest_title_context(context: dict[str, Any]) -> dict[str, Any]:
    topic_image_path = context.get("topic_image_path")
    return {
        "topic": context.get("topic", ""),
        "provider": context.get("provider", ""),
        "image_provider": context.get("image_provider", ""),
        "google_enabled": bool(context.get("google_enabled")),
        "gemini_text_model": context.get("gemini_text_model", ""),
        "openai_image_model": context.get("openai_image_model", ""),
        "openai_image_size": context.get("openai_image_size", ""),
        "generated_image_prompt": context.get("generated_image_prompt", ""),
        "topic_image_path": str(topic_image_path) if isinstance(topic_image_path, Path) else "",
    }


def build_article_carousel(
    source: str,
    *,
    out_dir: Path,
    max_pages: int,
    min_score: int,
    title: str | None,
    account_name: str,
    curation_backend: str,
    first_page_only: bool,
    no_title_enrichment: bool,
    timeout: int,
) -> Path:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    account_name = account_name.strip() or DEFAULT_ACCOUNT_NAME

    print(f"[article] reading {source}")
    html_text, final_url = read_source(source, timeout=timeout)
    article = parse_article(source, html_text, final_url)
    if not article.blocks:
        raise SystemExit("could not extract enough article text to build a carousel")

    candidates = build_candidate_sections(article)
    pages, used_backend = curate_pages(
        article,
        candidates,
        max_pages=max_pages,
        min_score=min_score,
        backend=curation_backend,
    )
    if not pages:
        raise SystemExit(
            "no high-signal article sections passed the filter; lower --min-score "
            "or try an article with more concrete details"
        )

    if first_page_only:
        pages_to_render: list[CarouselPage] = []
    else:
        pages_to_render = pages

    total = len(pages) + 1
    post = article_as_post(article)
    title_text = title or article.title
    if no_title_enrichment:
        title_context: dict[str, Any] = {
            "topic": title_text,
            "provider": "local",
            "image_provider": "",
            "google_enabled": False,
            "gemini_text_model": "",
            "openai_image_model": "",
            "openai_image_size": "",
            "generated_image_prompt": "",
            "topic_image_path": None,
        }
    else:
        title_context = build_title_enrichment([post], title=title_text, out_dir=out_dir)
    maybe_add_article_image(title_context, article, out_dir)

    slides: list[dict[str, Any]] = []
    title_path = out_dir / "slide_01.png"
    render_title_slide(post, title_path, total, title_text, title_context, account_name)
    slides.append({"index": 1, "type": "title", "path": str(title_path), "source_url": article.url})

    for slide_index, page in enumerate(pages_to_render, start=2):
        slide_path = out_dir / f"slide_{slide_index:02d}.png"
        render_article_slide(article, page, slide_path, slide_index, total)
        slides.append(page_manifest(page, slide_path, article, slide_index))

    article_report_path = out_dir / "source_article.json"
    article_report_path.write_text(
        json.dumps(
            {
                **manifest_article(article),
                "blocks": [
                    {"index": block.index, "role": block.role, "text": block.text}
                    for block in article.blocks
                ],
                "candidate_sections": [
                    {
                        "index": candidate.index,
                        "title": candidate.title,
                        "body": candidate.body,
                        "score": candidate.score,
                        "reasons": candidate.reasons,
                        "block_indices": candidate.block_indices,
                    }
                    for candidate in candidates
                ],
            },
            indent=2,
        )
        + "\n"
    )

    manifest = {
        "source_type": "article",
        "source_url": article.url,
        "article": manifest_article(article),
        "section_count": len(candidates),
        "selected_section_count": len(pages),
        "slide_count": total,
        "rendered_slide_count": len(slides),
        "first_page_only": first_page_only,
        "account_name": account_name,
        "curation_backend": used_backend,
        "min_score": min_score,
        "max_pages": max_pages,
        "source_article_path": str(article_report_path),
        "title_context": manifest_title_context(title_context),
        "slides": slides,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[article] selected {len(pages)} high-signal section(s) via {used_backend}")
    print(f"[article] wrote manifest -> {manifest_path}")
    return manifest_path


def main() -> int:
    load_env_file(ROOT / ".env")
    ap = argparse.ArgumentParser(description="Build an LLMAW carousel from an article URL")
    ap.add_argument("source", help="Article URL, file:// URL, or local HTML file")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--max-pages", type=int, default=6, help="Maximum article-section slides")
    ap.add_argument(
        "--min-score",
        type=int,
        default=6,
        help="Minimum local signal score required before a section can become a slide",
    )
    ap.add_argument("--title", help="Override generated title slide text")
    ap.add_argument(
        "--account-name",
        default=os.environ.get("ARTICLE_CAROUSEL_ACCOUNT_NAME", DEFAULT_ACCOUNT_NAME),
        help="Account or publisher name displayed in the title slide template",
    )
    ap.add_argument(
        "--curation-backend",
        choices=("auto", "gemini", "local"),
        default=os.environ.get("ARTICLE_CURATION_BACKEND", "auto"),
        help="Use Gemini for paraphrased editorial pages when available, otherwise local scoring",
    )
    ap.add_argument(
        "--first-page-only",
        action="store_true",
        help="Render only the title/cover page after article extraction and curation",
    )
    ap.add_argument(
        "--no-title-enrichment",
        action="store_true",
        help="Skip Gemini/OpenAI title enrichment and use article metadata or fallback art",
    )
    ap.add_argument("--timeout", type=int, default=30, help="Article fetch timeout in seconds")
    args = ap.parse_args()

    if args.max_pages < 1:
        raise SystemExit("--max-pages must be at least 1")

    build_article_carousel(
        args.source,
        out_dir=args.out_dir,
        max_pages=args.max_pages,
        min_score=args.min_score,
        title=args.title,
        account_name=args.account_name,
        curation_backend=args.curation_backend,
        first_page_only=args.first_page_only,
        no_title_enrichment=args.no_title_enrichment,
        timeout=args.timeout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
