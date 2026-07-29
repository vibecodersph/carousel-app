#!/usr/bin/env python3
"""Create a branded carousel from an X (Twitter) long-form Article or long post.

X Articles and long-form notes are full essays published behind a single status
URL. This workflow treats that essay like any other article: it fetches the
verbatim long-form text via the xAI x_search API, rebuilds it into the same
``Article`` structure the web-article pipeline consumes, and then reuses that
pipeline end to end -- candidate sectioning, Gemini/local curation, the
VibeCoders PH title cover, slide rendering, and the manifest.

    uv run python build_x_article_carousel.py https://x.com/satyanadella/status/2066182223213293753

Outputs go to out/x_article_carousel by default. The first slide is the shared
vibecodersph title cover; the remaining slides are the highest-signal sections of
the article, paraphrased by Gemini when GOOGLE_API_KEY is available.

Auth: XAI_API_KEY (also read from ./.env) or a Hermes xAI OAuth token, exactly
like fetch_tweet_data.py / build_x_carousel.py.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from build_article_carousel import (
    Article,
    TextBlock,
    clean_article_text,
    count_words,
    is_useful_block,
    normalize_space,
    render_carousel_from_article,
    split_sentences,
)
from channel import load_channel
from fetch_tweet_data import (
    compact_number,
    extract_json_value,
    extract_tweet_id,
    load_env_file,
    resolve_xai_token,
    xai_responses_text,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "out" / "x_article_carousel"

# Headings: short, title-style lines with no terminal sentence punctuation.
HEADING_MAX_WORDS = 12
_TERMINAL_PUNCT = ".?!,:;"
_SENTENCE_CHUNK_WORDS = 150
_PARAGRAPH_SPLIT_WORDS = 200


X_ARTICLE_FIELDS_SPEC = (
    '"id": "the post id as a string", '
    '"is_long_form": boolean (true for an X Article / long-form note, false for a short tweet), '
    '"title": "the article title or headline if the post has one, otherwise an empty string", '
    '"subtitle": "a one-sentence subtitle or the opening lede, otherwise an empty string", '
    '"full_text": "the COMPLETE verbatim body text. Preserve the original paragraph '
    "breaks as newline characters and put each section heading on its own line. Do not "
    'summarize, paraphrase, translate, shorten, or omit anything.", '
    '"author_name": "display name", '
    '"handle": "@username", '
    '"date": "Mon D, YYYY", '
    '"likes": number, "retweets": number, "replies": number, "views": number'
)


def xai_article_model() -> str:
    return os.environ.get("XAI_ARTICLE_MODEL") or os.environ.get("XAI_TWEET_MODEL") or "grok-4.3"


def fetch_x_article(tweet_id: str, token: str, *, timeout: int = 120) -> dict[str, Any]:
    """Fetch one X long-form post / Article verbatim via xAI Responses + x_search."""
    prompt = (
        f"Look up post {tweet_id} on X/Twitter. It is a long-form X post, note, or "
        f"X Article -- a full essay published under one status URL. "
        f"Return ONLY a raw JSON object (no markdown, no code fences) with these exact "
        f"fields: {X_ARTICLE_FIELDS_SPEC}"
    )
    # xai_responses_text reads XAI_TWEET_MODEL; honor an article-specific override.
    prior = os.environ.get("XAI_TWEET_MODEL")
    os.environ["XAI_TWEET_MODEL"] = xai_article_model()
    try:
        text_content = xai_responses_text(prompt, token, timeout=timeout)
    finally:
        if prior is None:
            os.environ.pop("XAI_TWEET_MODEL", None)
        else:
            os.environ["XAI_TWEET_MODEL"] = prior

    data = extract_json_value(text_content, "{", "}")
    if not isinstance(data, dict):
        raise SystemExit("xAI returned JSON that is not an object")
    data.setdefault("id", tweet_id)
    return data


def _count(data: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(data.get(key)))
    except (TypeError, ValueError):
        return 0


def looks_like_heading(line: str) -> bool:
    """A short, title-style line with no terminal sentence punctuation."""
    s = normalize_space(line)
    if not s or s[-1] in _TERMINAL_PUNCT:
        return False
    if re.match(r"^[-*•–—\d]", s):  # bullets / numbered / dash leads are body
        return False
    words = s.split()
    if not 1 <= len(words) <= HEADING_MAX_WORDS:
        return False
    meaningful = [w for w in words if len(w) >= 4 and w[0].isalpha()]
    if not meaningful:
        return False
    capitalized = sum(1 for w in meaningful if w[0].isupper())
    return capitalized / len(meaningful) >= 0.6


def _split_long_paragraph(text: str) -> list[str]:
    """Break an over-long paragraph into sentence groups within the word cap."""
    chunks: list[str] = []
    current: list[str] = []
    current_words = 0
    for sentence in split_sentences(text):
        words = count_words(sentence)
        if current and current_words + words > _SENTENCE_CHUNK_WORDS:
            chunks.append(" ".join(current))
            current = []
            current_words = 0
        current.append(sentence)
        current_words += words
    if current:
        chunks.append(" ".join(current))
    return chunks


def blocks_from_full_text(full_text: str) -> list[TextBlock]:
    """Turn an X long-form body into ordered Article blocks (h2 headings + p)."""
    blocks: list[TextBlock] = []

    def add(role: str, text: str) -> None:
        if text:
            blocks.append(TextBlock(role, text, len(blocks)))

    for raw_line in full_text.replace("\r\n", "\n").split("\n"):
        cleaned = clean_article_text(raw_line)
        if not cleaned:
            continue
        if looks_like_heading(cleaned):
            if is_useful_block(cleaned, "h2"):
                add("h2", cleaned)
            continue
        if count_words(cleaned) <= _PARAGRAPH_SPLIT_WORDS and is_useful_block(cleaned, "p"):
            add("p", cleaned)
            continue
        for chunk in _split_long_paragraph(cleaned):
            if is_useful_block(chunk, "p"):
                add("p", chunk)
    return blocks


def derive_title(data: dict[str, Any], blocks: list[TextBlock]) -> str:
    title = normalize_space(str(data.get("title") or ""))
    if title:
        return title
    for block in blocks:
        if block.role == "h2":
            return block.text
    for block in blocks:
        if block.role == "p":
            sentences = split_sentences(block.text)
            if sentences:
                return sentences[0]
    return "X Article"


def x_article_to_article(data: dict[str, Any], *, url: str) -> Article:
    """Assemble the shared Article dataclass from a fetched X long-form post."""
    full_text = str(data.get("full_text") or data.get("text") or "")
    blocks = blocks_from_full_text(full_text)
    title = derive_title(data, blocks)

    author = normalize_space(str(data.get("author_name") or data.get("author") or ""))
    handle = normalize_space(str(data.get("handle") or "")).lstrip("@")
    subtitle = normalize_space(str(data.get("subtitle") or ""))
    description = subtitle or (blocks[0].text if blocks else "")
    site_name = f"{author} on X" if author else (f"@{handle} on X" if handle else "X")

    return Article(
        source=url,
        url=url,
        title=title,
        description=description,
        site_name=site_name,
        author=author or (f"@{handle}" if handle else ""),
        published_at=normalize_space(str(data.get("date") or "")),
        image_url="",
        blocks=blocks,
        canonical_url=url,
    )


def append_x_metadata(manifest_path: Path, data: dict[str, Any], article: Article) -> None:
    """Record X-specific provenance the shared article manifest does not carry."""
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    likes = _count(data, "likes")
    retweets = _count(data, "retweets")
    replies = _count(data, "replies")
    views = _count(data, "views")
    handle = normalize_space(str(data.get("handle") or "")).lstrip("@")
    manifest["x_article"] = {
        "id": str(data.get("id") or ""),
        "handle": f"@{handle}" if handle else "",
        "author": article.author,
        "is_long_form": bool(data.get("is_long_form", True)),
        "date": article.published_at,
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "views": views,
        "likes_fmt": compact_number(likes),
        "retweets_fmt": compact_number(retweets),
        "replies_fmt": compact_number(replies),
        "views_fmt": compact_number(views) if views else "",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def build_x_article_carousel(
    url_or_id: str,
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
    tweet_id = extract_tweet_id(url_or_id)
    token = resolve_xai_token()

    print(f"[x-article] fetching long-form post {tweet_id} via xAI")
    data = fetch_x_article(tweet_id, token, timeout=timeout)

    handle = normalize_space(str(data.get("handle") or "")).lstrip("@")
    url = f"https://x.com/{handle}/status/{tweet_id}" if handle else f"https://x.com/i/status/{tweet_id}"
    article = x_article_to_article(data, url=url)

    if not data.get("is_long_form", True):
        print(
            "[x-article] WARNING: xAI reports this is a short post, not a long-form "
            "Article. build_x_carousel.py is the better fit for short tweets."
        )
    if not article.blocks:
        raise SystemExit(
            "could not extract enough long-form text to build a carousel; this status "
            "may be a short tweet -- try build_x_carousel.py instead"
        )

    print(
        f"[x-article] {len(article.blocks)} block(s) extracted from "
        f"{article.author or '@' + handle} -- {count_words(' '.join(b.text for b in article.blocks))} words"
    )

    manifest_path = render_carousel_from_article(
        article,
        out_dir=out_dir,
        max_pages=max_pages,
        min_score=min_score,
        title=title,
        account_name=account_name,
        curation_backend=curation_backend,
        first_page_only=first_page_only,
        no_title_enrichment=no_title_enrichment,
        source_type="x_article",
        source_badge="X ARTICLE",
    )
    append_x_metadata(manifest_path, data, article)
    print(f"[x-article] wrote manifest -> {manifest_path}")
    return manifest_path


def main() -> int:
    load_env_file(ROOT / ".env")
    ap = argparse.ArgumentParser(
        description="Build a branded carousel from an X long-form Article URL (--channel selects branding/language/voice)"
    )
    ap.add_argument("source", help="X/Twitter Article URL or status ID")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--max-pages", type=int, default=6, help="Maximum article-section slides")
    ap.add_argument(
        "--min-score",
        type=int,
        default=3,
        help="Minimum local signal score required before a section can become a slide. "
        "X Articles are curated thought pieces, so this defaults lower than the web "
        "article builder (3 vs 6).",
    )
    ap.add_argument("--title", help="Override generated title slide text")
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
        "--account-name",
        default=os.environ.get("ARTICLE_CAROUSEL_ACCOUNT_NAME"),
        help="Override the account/publisher name on the title slide (default: channel account_name)",
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
        help="Render only the title/cover page after extraction and curation",
    )
    ap.add_argument(
        "--no-title-enrichment",
        action="store_true",
        help="Skip Gemini/OpenAI title enrichment and use a plain title cover",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="xAI fetch timeout in seconds (long-form posts need more time)",
    )
    args = ap.parse_args()

    if args.max_pages < 1:
        raise SystemExit("--max-pages must be at least 1")

    # Select the active channel for every load_channel() call in this process.
    if args.channel:
        os.environ["CAROUSEL_CHANNEL"] = args.channel
    account_name = args.account_name or load_channel().account_name

    build_x_article_carousel(
        args.source,
        out_dir=args.out_dir,
        max_pages=args.max_pages,
        min_score=args.min_score,
        title=args.title,
        account_name=account_name,
        curation_backend=args.curation_backend,
        first_page_only=args.first_page_only,
        no_title_enrichment=args.no_title_enrichment,
        timeout=args.timeout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
