#!/usr/bin/env python3
"""Create a branded carousel from one X/Twitter URL.

The workflow is intentionally one-input:

    uv run python build_x_carousel.py https://x.com/OpenAI/status/2061887650391625870

Outputs go to out/x_carousel by default. The first slide is a branded title
PNG. Each discovered post becomes either a branded post PNG or a branded
post+video MP4 when video is available.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from build_video_slide import (
    OUT,
    SLIDE_H,
    SLIDE_W,
    build_video_slide,
    clean_post_text,
    compact_number,
    extract_status_id,
    extract_status_url,
    format_post_date,
)
from channel import load_channel
from fetch_tweet_data import fetch_thread, resolve_xai_token
from generate_cover import DEFAULT_OPENAI_IMAGE_MODEL, generate_openai, openai_api_key

ROOT = Path(__file__).resolve().parent
FONTS = ROOT / "assets" / "archivo.css"
# Legacy voice-doc path, kept for the default channel's backward-compatible fallback.
# The active channel's voice guide is resolved via load_channel(); see channel.py.
IG_VOICE_DOC = ROOT / "brand" / "VIBECODERS_IG_VOICE.md"
DEFAULT_OUT = OUT / "x_carousel"
# Last-resort account label. The active channel's account_name normally supplies this.
DEFAULT_ACCOUNT_NAME = "vibecodersph"
PERSON_SOURCE_HANDLES = {
    "sama",
    "karpathy",
    "bcherny",
    "gdb",
    "demishassabis",
    "jeffdean",
    "fchollet",
    "ylecun",
    "clementdelangue",
    "aidangomez",
    "simonw",
    "emollick",
    "amasad",
    "rauchg",
    "jeremyphoward",
}
ORGANIZATION_SOURCE_HANDLES = {
    "openai",
    "anthropicai",
    "googledeepmind",
    "mistralai",
}

X_COOKIE_DOMAINS = ("x.com", "twitter.com")
GOOGLE_KG_ENDPOINT = "https://kgsearch.googleapis.com/v1/entities:search"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com"
DEFAULT_GEMINI_TEXT_MODEL = "gemini-3.5-flash"
DEFAULT_OPENAI_TITLE_IMAGE_SIZE = "2048x1152"
GOOGLE_WARNED: set[str] = set()
IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


def google_warn(message: str) -> None:
    if message in GOOGLE_WARNED:
        return
    GOOGLE_WARNED.add(message)
    print(message)


def gemini_api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def gemini_text_model() -> str:
    return os.environ.get("GEMINI_TEXT_MODEL") or DEFAULT_GEMINI_TEXT_MODEL


def gemini_generate_content(
    model: str,
    api_key: str | None,
    payload: dict[str, object],
    *,
    api_version: str,
    timeout: int = 30,
) -> dict[str, object] | None:
    if not api_key:
        return None
    request = Request(
        f"{GEMINI_API_ROOT}/{api_version}/models/{model}:generateContent",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": "carousel-app/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            error = error_payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                detail = f": {error['message'][:160]}"
        except (OSError, json.JSONDecodeError):
            detail = ""
        google_warn(f"[google] Gemini {model} returned HTTP {exc.code}{detail}")
    except (OSError, URLError, json.JSONDecodeError):
        google_warn(f"[google] Gemini {model} request failed; continuing without it")
    return None


def gemini_parts(payload: dict[str, object]) -> list[dict[str, object]]:
    parts: list[dict[str, object]] = []
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return parts
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        content = candidate.get("content")
        if not isinstance(content, dict):
            continue
        candidate_parts = content.get("parts")
        if isinstance(candidate_parts, list):
            parts.extend(part for part in candidate_parts if isinstance(part, dict))
    return parts


def extract_gemini_text(payload: dict[str, object] | None) -> str:
    if not payload:
        return ""
    return "\n".join(
        str(part.get("text"))
        for part in gemini_parts(payload)
        if isinstance(part.get("text"), str)
    ).strip()


def parse_json_object(text: str) -> dict[str, object] | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def string_value(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def safe_filename(value: str, fallback: str = "asset") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    if not value:
        value = fallback
    return value[:64]


def compact_topic(text: str, limit: int = 95) -> str:
    text = clean_post_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].strip()


def load_ig_voice_prompt() -> str:
    """Cover-generation voice instruction for the active channel's voice guide."""
    return load_channel().voice_prompt


def kg_search(
    query: str,
    api_key: str | None,
    *,
    types: tuple[str, ...] = (),
    limit: int = 5,
) -> list[dict[str, object]]:
    if not api_key or not query.strip():
        return []
    params: list[tuple[str, str]] = [
        ("query", query.strip()),
        ("key", api_key),
        ("limit", str(limit)),
        ("languages", "en"),
        ("indent", "false"),
    ]
    params.extend(("types", item) for item in types)
    request = Request(
        f"{GOOGLE_KG_ENDPOINT}?{urlencode(params)}",
        headers={"User-Agent": "carousel-app/1.0"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        google_warn(
            f"[google] Knowledge Graph lookup returned HTTP {exc.code}; "
            "check GOOGLE_KG_API_KEY and API enablement"
        )
        return []
    except (OSError, URLError, json.JSONDecodeError):
        google_warn("[google] Knowledge Graph lookup failed; continuing without Google images")
        return []
    items = payload.get("itemListElement", [])
    return [item for item in items if isinstance(item, dict)]


def entity_from_kg_item(item: dict[str, object]) -> dict[str, object] | None:
    result = item.get("result")
    if not isinstance(result, dict):
        return None
    name = result.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    image = result.get("image")
    detailed = result.get("detailedDescription")
    return {
        "id": result.get("@id", ""),
        "name": name.strip(),
        "description": result.get("description", ""),
        "types": result.get("@type", []),
        "image_url": image.get("contentUrl") if isinstance(image, dict) else "",
        "source_url": image.get("url") if isinstance(image, dict) else result.get("url", ""),
        "license": image.get("license") if isinstance(image, dict) else "",
        "detail": detailed.get("articleBody") if isinstance(detailed, dict) else "",
        "score": item.get("resultScore", 0),
    }


def first_kg_entity(
    query: str,
    api_key: str | None,
    *,
    types: tuple[str, ...] = (),
    require_image: bool = False,
    reject_types: tuple[str, ...] = (),
) -> dict[str, object] | None:
    for item in kg_search(query, api_key, types=types, limit=6):
        entity = entity_from_kg_item(item)
        if not entity:
            continue
        entity_types = {str(item_type) for item_type in entity.get("types", [])}
        if any(rejected in entity_types for rejected in reject_types):
            continue
        if require_image and not entity.get("image_url"):
            continue
        return entity
    return None


def image_extension(content_type: str, url: str) -> str:
    content_type = content_type.split(";", 1)[0].strip().lower()
    if content_type in IMAGE_CONTENT_TYPES:
        return IMAGE_CONTENT_TYPES[content_type]
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def download_image(url: object, out_dir: Path, stem: str) -> Path | None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 carousel-app/1.0",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            content_type = response.headers.get("Content-Type", "")
            data = response.read(8_000_001)
    except (OSError, URLError):
        return None
    if len(data) > 8_000_000:
        return None
    ext = image_extension(content_type, url)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    path = out_dir / f"{safe_filename(stem)}-{digest}{ext}"
    path.write_bytes(data)
    return path


def normalized_handle(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "", str(value or "").strip().lstrip("@").lower())


def normalize_x_profile_image_url(url: object) -> str:
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return ""
    if "twimg.com/profile_images/" in url:
        url = re.sub(r"([_-])normal(\.[A-Za-z0-9]+)(?=$|\?)", r"\g<1>400x400\g<2>", url)
        url = re.sub(r"([?&])name=normal\b", r"\1name=400x400", url)
    return url


def profile_image_url_from_metadata(metadata: dict[str, object] | None) -> str:
    if not metadata:
        return ""
    for key in (
        "uploader_thumbnail",
        "uploader_avatar",
        "channel_thumbnail",
        "channel_avatar",
        "creator_thumbnail",
        "creator_avatar",
        "thumbnail",
    ):
        url = normalize_x_profile_image_url(metadata.get(key))
        if url and "twimg.com/profile_images/" in url:
            return url
    thumbnails = metadata.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in thumbnails:
            if not isinstance(item, dict):
                continue
            url = normalize_x_profile_image_url(item.get("url"))
            if url and "twimg.com/profile_images/" in url:
                return url
    return ""


def normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def company_candidate_terms(posts: list[dict[str, str]]) -> list[str]:
    terms: list[str] = []
    stopwords = {
        "AI",
        "API",
        "Codex",
        "From",
        "Jun",
        "Reply",
        "Show",
        "The",
        "These",
        "This",
        "Translate",
        "We",
        "What",
    }

    def add(term: str) -> None:
        term = re.sub(r"\s+", " ", term.strip(" .,:;()[]{}"))
        if len(term) < 3 or term in stopwords:
            return
        if normalized_key(term) and all(normalized_key(term) != normalized_key(existing) for existing in terms):
            terms.append(term)

    for post in posts:
        add(post.get("author", ""))
        handle = post.get("handle", "").lstrip("@")
        if handle:
            add(handle)
        text = post.get("text", "")
        for match in re.finditer(
            r"\b(?:[A-Z][A-Za-z0-9&._-]{2,}|[A-Z]{2,})"
            r"(?:\s+(?:[A-Z][A-Za-z0-9&._-]{2,}|[A-Z]{2,})){0,3}",
            text,
        ):
            add(match.group(0))
            if len(terms) >= 8:
                break
        if len(terms) >= 8:
            break
    return terms[:8]


def find_company_entities(posts: list[dict[str, str]], api_key: str | None) -> list[dict[str, object]]:
    candidates = company_candidate_terms(posts)
    if not api_key:
        return [{"name": term, "query": term} for term in candidates[:1]]

    companies: list[dict[str, object]] = []
    seen: set[str] = set()
    for term in candidates:
        queries = [f"{term} company", term]
        for query in queries:
            entity = (
                first_kg_entity(query, api_key, types=("Organization",), reject_types=("Person",))
                or first_kg_entity(query, api_key, reject_types=("Person",))
            )
            if not entity:
                continue
            key = normalized_key(str(entity["name"]))
            if key in seen:
                break
            seen.add(key)
            entity["query"] = query
            companies.append(entity)
            break
        if len(companies) >= 3:
            break
    if companies:
        return companies
    return [{"name": term, "query": term} for term in candidates[:1]]


def find_ceo_entity(company: dict[str, object], api_key: str | None) -> dict[str, object] | None:
    if not api_key:
        return None
    company_name = str(company.get("name", ""))
    queries = [
        f"{company_name} CEO",
        f"{company_name} chief executive officer",
    ]
    for query in queries:
        for item in kg_search(query, api_key, types=("Person",), limit=6):
            entity = entity_from_kg_item(item)
            if not entity:
                continue
            haystack = " ".join(
                str(entity.get(field, "")) for field in ("name", "description", "detail")
            ).lower()
            if "ceo" in haystack or "chief executive" in haystack or company_name.lower() in haystack:
                entity["company"] = company_name
                entity["query"] = query
                return entity
    return None


def find_topic_entity(topic: str, companies: list[dict[str, object]], api_key: str | None) -> dict[str, object] | None:
    if not api_key:
        return None
    queries = [topic]
    for company in companies[:2]:
        company_name = str(company.get("name", ""))
        if company_name:
            queries.insert(0, f"{company_name} {topic}")
            queries.append(company_name)
    seen: set[str] = set()
    for query in queries:
        query = compact_topic(query, 110)
        key = normalized_key(query)
        if not key or key in seen:
            continue
        seen.add(key)
        entity = first_kg_entity(query, api_key, require_image=True)
        if entity:
            return entity
    return None


def canonical_x_url(value: str) -> str:
    status_url = extract_status_url(value) or value.strip()
    status_id = extract_status_id(status_url)
    if not status_id:
        raise SystemExit(f"could not find an X status id in: {value}")
    match = re.search(r"https://(?:www\.)?(?:x|twitter)\.com/([^/]+)/status/", status_url)
    handle = match.group(1) if match else "i"
    return f"https://x.com/{handle}/status/{status_id}"


def run_json(cmd: list[str]) -> dict[str, object] | None:
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def fetch_metadata(url: str, cookies_from_browser: str | None) -> dict[str, object] | None:
    cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--dump-json"]
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    cmd.append(url)
    return run_json(cmd)


def metadata_has_video(metadata: dict[str, object] | None) -> bool:
    if not metadata:
        return False
    formats = metadata.get("formats")
    return isinstance(formats, list) and any(fmt.get("vcodec") != "none" for fmt in formats if isinstance(fmt, dict))


def parse_browser_cookie_spec(value: str) -> tuple[str, str | None, str | None, str | None]:
    browser_spec, _, container = value.partition("::")
    browser_profile, _, profile = browser_spec.partition(":")
    browser, _, keyring = browser_profile.partition("+")
    return browser, profile or None, keyring or None, container or None


def same_site_for_playwright(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.lower()
    if normalized == "strict":
        return "Strict"
    if normalized == "lax":
        return "Lax"
    if normalized == "none":
        return "None"
    return None


def cookie_domain_is_x(domain: str) -> bool:
    domain = domain.lstrip(".").lower()
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in X_COOKIE_DOMAINS)


def expires_for_playwright(value: int) -> int:
    if value <= 0:
        return -1
    if value > 10_000_000_000:
        value = int(value / 1_000_000 - 11_644_473_600)
    return value if value > 0 else -1


def load_playwright_cookies(cookies_from_browser: str | None) -> list[dict[str, object]]:
    if not cookies_from_browser:
        return []
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ModuleNotFoundError:
        print("[x] yt-dlp is required to import browser cookies; continuing without them")
        return []

    browser_name, profile, keyring, container = parse_browser_cookie_spec(cookies_from_browser)
    try:
        cookie_jar = extract_cookies_from_browser(
            browser_name,
            profile=profile,
            keyring=keyring,
            container=container,
        )
    except Exception as exc:
        print(f"[x] could not load browser cookies from {cookies_from_browser}: {exc}")
        return []

    cookies: list[dict[str, object]] = []
    for cookie in cookie_jar:
        if not cookie_domain_is_x(cookie.domain):
            continue
        item: dict[str, object] = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "secure": bool(cookie.secure),
            "httpOnly": bool(
                cookie.has_nonstandard_attr("HttpOnly")
                or cookie.has_nonstandard_attr("HTTPOnly")
            ),
        }
        if cookie.expires is not None:
            item["expires"] = expires_for_playwright(int(cookie.expires))
        same_site = same_site_for_playwright(
            cookie.get_nonstandard_attr("SameSite")
            or cookie.get_nonstandard_attr("sameSite")
        )
        if same_site:
            item["sameSite"] = same_site
        cookies.append(item)
    print(f"[x] loaded {len(cookies)} X/Twitter cookies from {cookies_from_browser}")
    return cookies


def post_from_metadata(url: str, metadata: dict[str, object] | None) -> dict[str, str]:
    metadata = metadata or {}
    author = str(metadata.get("uploader") or "Source post")
    handle = str(metadata.get("uploader_id") or "")
    if handle and not handle.startswith("@"):
        handle = f"@{handle}"
    text = clean_post_text(metadata.get("description")) or clean_post_text(metadata.get("title")) or "Source post"
    date = format_post_date(metadata.get("timestamp"))
    views = compact_number(metadata.get("view_count"))
    likes = compact_number(metadata.get("like_count"))
    reposts = compact_number(metadata.get("repost_count"))
    replies = compact_number(metadata.get("comment_count"))
    status_id = extract_status_id(url) or ""
    return {
        "url": url,
        "id": status_id,
        "author": author,
        "handle": handle,
        "text": text,
        "date": date,
        "views": views,
        "likes": likes,
        "reposts": reposts,
        "replies": replies,
        "profile_image_url": profile_image_url_from_metadata(metadata),
    }


def is_embed_stop_line(line: str) -> bool:
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
    if re.search(rf"\b(?:{months})\b\s+\d{{1,2}},\s+\d{{4}}", line):
        return True
    if re.match(r"^(?:Read|Show|View)\b", line):
        return True
    if re.match(r"^\d[\d,.]*\s*(?:Views?|Likes?|Reposts?|Quotes?|Bookmarks?|Replies?)$", line):
        return True
    return line in {"Reply", "Repost", "Like", "View", "Share", "Copy link", "Translate post"}


def post_from_embed_text(url: str, embed_text: str) -> dict[str, str] | None:
    status_id = extract_status_id(url)
    if not status_id:
        return None
    lines = [line.strip() for line in embed_text.splitlines() if line.strip()]
    if not lines:
        return None

    handle = next((line for line in lines if line.startswith("@")), "")
    handle_index = lines.index(handle) if handle else -1
    author = ""
    if handle_index > 0:
        author = re.sub(r"\s*[✓✔].*$", "", lines[handle_index - 1]).strip()
    elif lines and not lines[0].startswith("@"):
        author = re.sub(r"\s*[✓✔].*$", "", lines[0]).strip()

    content_lines: list[str] = []
    start = handle_index + 1 if handle_index >= 0 else 1
    for line in lines[start:]:
        if is_embed_stop_line(line):
            break
        if line in {author, handle, "·", "Follow"}:
            continue
        content_lines.append(line)

    text = clean_post_text("\n".join(content_lines))
    return {
        "url": canonical_x_url(url),
        "id": status_id,
        "author": author or "Source post",
        "handle": handle,
        "text": text or "Source post",
        "date": "",
        "views": "",
        "likes": "",
        "reposts": "",
        "replies": "",
    }


def fetch_embed_post(url: str) -> dict[str, str] | None:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        return None

    status_id = extract_status_id(url)
    if not status_id:
        return None
    embed_url = (
        "https://platform.twitter.com/embed/Tweet.html"
        f"?id={status_id}&theme=dark&width=550&hideThread=true&dnt=true"
    )
    print(f"[x] reading embed metadata {status_id}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 620, "height": 1600}, device_scale_factor=1)
            page.goto(embed_url, wait_until="networkidle")
            page.wait_for_timeout(1000)
            embed_text = page.locator("article").first.inner_text(timeout=10000)
            profile_image_url = page.evaluate(
                """() => {
                    const article = document.querySelector('article');
                    if (!article) return "";
                    const images = Array.from(article.querySelectorAll('img'));
                    const profile = images.find((img) => {
                        const src = img.currentSrc || img.src || "";
                        return src.includes("/profile_images/");
                    });
                    return profile ? (profile.currentSrc || profile.src || "") : "";
                }"""
            )
            browser.close()
    except Exception:
        return None
    post = post_from_embed_text(url, embed_text)
    if post and profile_image_url:
        post["profile_image_url"] = normalize_x_profile_image_url(profile_image_url)
    return post


def article_to_post(article: dict[str, object]) -> dict[str, str] | None:
    url = str(article.get("url") or "")
    status_id = extract_status_id(url)
    if not status_id:
        return None
    text = str(article.get("text") or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    author = str(article.get("author") or "")
    handle = str(article.get("handle") or "")
    if not author and handle:
        for i, line in enumerate(lines):
            if line == handle and i > 0:
                author = re.sub(r"\s*[✓✔].*$", "", lines[i - 1]).strip()
                break
    handle_name = handle.lstrip("@").lower()
    content_lines = []
    for line in lines:
        normalized = re.sub(r"\s*[✓✔].*$", "", line).strip()
        if line in {author, handle, "·", "Follow"}:
            continue
        if normalized and normalized == author:
            continue
        if handle_name and normalized.lower() == handle_name:
            continue
        if re.match(r"^\d+[KMB]?$", line):
            continue
        if line in {"Reply", "Repost", "Like", "View", "Share"}:
            continue
        content_lines.append(line)
    content = clean_post_text("\n".join(content_lines))
    return {
        "url": canonical_x_url(url),
        "id": status_id,
        "author": author or "Source post",
        "handle": handle,
        "text": content,
        "date": "",
        "views": "",
        "likes": "",
        "reposts": "",
        "replies": "",
        "profile_image_url": normalize_x_profile_image_url(article.get("profile_image_url")),
    }


def post_from_xai(data: dict[str, object]) -> dict[str, str] | None:
    status_id = str(data.get("id") or "")
    if not re.fullmatch(r"\d{10,}", status_id):
        return None
    text = clean_post_text(str(data.get("text") or ""))
    if not text:
        return None
    url = str(data.get("url") or "")
    if not url:
        handle = str(data.get("handle") or "").lstrip("@") or "i"
        url = f"https://x.com/{handle}/status/{status_id}"
    return {
        "url": canonical_x_url(url),
        "id": status_id,
        "author": str(data.get("author") or "Source post"),
        "handle": str(data.get("handle") or ""),
        "text": text,
        "date": str(data.get("date") or ""),
        "views": str(data.get("views_fmt") or ""),
        "likes": str(data.get("likes_fmt") or ""),
        "reposts": str(data.get("retweets_fmt") or ""),
        "replies": str(data.get("replies_fmt") or ""),
        "profile_image_url": normalize_x_profile_image_url(data.get("profile_image_url")),
    }


def discover_thread_posts_xai(url: str, max_posts: int) -> list[dict[str, str]]:
    token = resolve_xai_token(required=False)
    if not token:
        return []
    status_id = extract_status_id(url)
    if not status_id:
        return []
    print("[x] fetching thread via xAI x_search")
    try:
        thread = fetch_thread(status_id, token, max_posts=max_posts)
    except SystemExit as exc:
        print(f"[x] xAI thread fetch failed ({exc}); trying Playwright discovery")
        return []
    except Exception as exc:
        print(f"[x] xAI thread fetch error ({exc}); trying Playwright discovery")
        return []

    posts = [post for post in (post_from_xai(item) for item in thread) if post]
    if not any(post["id"] == status_id for post in posts):
        print("[x] xAI thread result did not include the requested post; ignoring it")
        return []
    print(f"[x] xAI found {len(posts)} thread post(s)")
    return posts[:max_posts]


def discover_thread_posts(url: str, max_posts: int, cookies_from_browser: str | None) -> list[dict[str, str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        return []

    print("[x] looking for thread posts")
    article_script = """
    () => Array.from(document.querySelectorAll('article')).map((article, index) => {
        const timeLink = article.querySelector('time')?.closest('a')?.href || "";
        const statusLinks = Array.from(article.querySelectorAll('a[href*="/status/"]')).map((a) => a.href);
        const status = timeLink || statusLinks.find(Boolean) || "";
        const links = Array.from(article.querySelectorAll('a[href^="/"], a[href^="https://x.com/"], a[href^="https://twitter.com/"]'));
        const handleLink = links.map((a) => a.textContent || "").find((text) => /^@/.test(text.trim())) || "";
        const author = Array.from(article.querySelectorAll('a[role="link"] span'))
          .map((el) => el.textContent || "").find((text) => text && !text.startsWith("@")) || "";
        const profileImage = Array.from(article.querySelectorAll('img'))
          .map((img) => img.currentSrc || img.src || "")
          .find((src) => src.includes("/profile_images/")) || "";
        const rect = article.getBoundingClientRect();
        return {
            url: status,
            handle: handleLink.trim(),
            author: author.trim(),
            text: article.innerText || "",
            profile_image_url: profileImage,
            y: Math.round(rect.top + window.scrollY),
            index,
        };
    })
    """

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                viewport={"width": 760, "height": 2200},
                device_scale_factor=1,
                color_scheme="dark",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
            )
            cookies = load_playwright_cookies(cookies_from_browser)
            if cookies:
                try:
                    context.add_cookies(cookies)
                except Exception as exc:
                    print(f"[x] could not add browser cookies to Playwright: {exc}")
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector("article", timeout=20000)
            page.wait_for_timeout(2500)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            articles: list[dict[str, object]] = []
            seen_snapshot_ids: set[str] = set()

            def collect_visible_articles() -> int:
                added = 0
                for article in page.evaluate(article_script):
                    status_id = extract_status_id(str(article.get("url") or ""))
                    if not status_id:
                        continue
                    snapshot_key = f"{status_id}:{article.get('y')}:{article.get('index')}"
                    if snapshot_key in seen_snapshot_ids:
                        continue
                    seen_snapshot_ids.add(snapshot_key)
                    articles.append(article)
                    added += 1
                return added

            collect_visible_articles()
            for selector in ("text=/Show this thread/i", "text=/Show more replies/i", "text=/Read .*repl/i"):
                try:
                    page.locator(selector).first.click(timeout=2500)
                    page.wait_for_timeout(1600)
                    collect_visible_articles()
                except Exception:
                    pass

            stable_rounds = 0
            max_rounds = max(6, min(18, max_posts * 3))
            for _ in range(max_rounds):
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(900)
                added = collect_visible_articles()
                if added:
                    stable_rounds = 0
                else:
                    stable_rounds += 1
                if stable_rounds >= 3:
                    break
            browser.close()
    except Exception:
        return []

    unique_posts: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for article in articles:
        post = article_to_post(article)
        if not post or post["id"] in seen_ids:
            continue
        seen_ids.add(post["id"])
        unique_posts.append(post)

    target_id = extract_status_id(url)
    target_index = next((i for i, post in enumerate(unique_posts) if post["id"] == target_id), -1)
    if target_index < 0:
        return unique_posts[:max_posts]

    target = unique_posts[target_index]
    thread_handle = target.get("handle", "").lower()
    if not thread_handle:
        thread_handle = next((post["handle"].lower() for post in unique_posts if post.get("handle")), "")

    start = target_index
    while start > 0:
        previous = unique_posts[start - 1]
        previous_handle = previous.get("handle", "").lower()
        if thread_handle and previous_handle and previous_handle != thread_handle:
            break
        start -= 1

    posts: list[dict[str, str]] = []
    for post in unique_posts[start:]:
        post_handle = post.get("handle", "").lower()
        if thread_handle and post_handle and post_handle != thread_handle:
            if any(item["id"] == target_id for item in posts):
                break
            continue
        posts.append(post)
        if len(posts) >= max_posts:
            break

    if len(posts) <= 1 and not cookies_from_browser:
        print(
            "[x] only one public post was visible; if this is a thread, "
            "try --cookies-from-browser chrome"
        )
    return posts


def capture_embed(status_id: str, out_path: Path, *, width: int = 620) -> Path:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit("playwright is required to capture X embeds") from exc

    url = (
        "https://platform.twitter.com/embed/Tweet.html"
        f"?id={status_id}&theme=dark&width={width}&hideThread=true&dnt=true"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[x] capturing embed {status_id} -> {out_path.name}")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width + 60, "height": 2400}, device_scale_factor=2)
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(1500)
        page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('*')) {
                    const cs = getComputedStyle(el);
                    if (cs.webkitLineClamp && cs.webkitLineClamp !== 'none') {
                        el.style.webkitLineClamp = 'unset';
                        el.style.display = 'block';
                    }
                    if (el.tagName === 'A' && el.textContent.trim() === 'Show more') {
                        el.style.display = 'none';
                    }
                }
            }"""
        )
        page.wait_for_timeout(300)
        page.locator("article").first.screenshot(path=str(out_path))
        browser.close()
    return out_path


def clamp_words(text: str, limit: int) -> str:
    words = re.findall(r"[\w’'-]+", text)
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit])


def split_title_for_two_tone(text: str) -> tuple[str, str]:
    parts = text.split()
    if not parts:
        return "", ""
    if len(parts) < 3:
        return text, ""
    accent_count = max(1, round(len(parts) * 0.36))
    if len(parts) >= 7:
        accent_count = max(2, accent_count)
    accent_count = min(accent_count, 5, len(parts) - 1)
    return " ".join(parts[:-accent_count]), " ".join(parts[-accent_count:])


ACCENT_STOPWORDS = {
    "a",
    "about",
    "amid",
    "an",
    "and",
    "ang",
    "are",
    "as",
    "at",
    "but",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "its",
    "lang",
    "may",
    "mga",
    "mo",
    "na",
    "ng",
    "nito",
    "natin",
    "niyo",
    "of",
    "on",
    "or",
    "our",
    "pa",
    "para",
    "rin",
    "sa",
    "that",
    "the",
    "their",
    "this",
    "to",
    "via",
    "with",
    "yung",
}


def accent_word_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9'._+-]*", text):
        word = match.group(0).strip("'._+-")
        if word:
            candidates.append(word)
    return candidates


def choose_accent_word(text: str) -> str:
    candidates = accent_word_candidates(text)
    if not candidates:
        return ""
    meaningful = [word for word in candidates if word.lower() not in ACCENT_STOPWORDS]
    return (meaningful or candidates)[-1]


def bracket_single_accent_word(headline: str, accent_word: str = "") -> str:
    headline = headline.replace("\u2014", ",").strip()
    if not headline:
        return ""

    bracket_match = re.search(r"\[([^\[\]]+)\]", headline)
    explicit_accent = (accent_word.strip().strip("[]") if accent_word else "") or (
        bracket_match.group(1).strip() if bracket_match else ""
    )
    target = (
        choose_accent_word(explicit_accent)
        or choose_accent_word(headline)
        or explicit_accent
    )
    plain = headline.replace("[", "").replace("]", "")
    if not target:
        return plain
    if not re.search(r"[A-Za-z0-9]", target):
        start = plain.find(target)
        if start < 0:
            return plain
        end = start + len(target)
        return f"{plain[:start]}[{plain[start:end]}]{plain[end:]}"
    pattern = re.compile(rf"(?<![\w'])({re.escape(target)})(?![\w'])", re.I)
    updated, count = pattern.subn(r"[\1]", plain, count=1)
    return updated if count else plain


def title_from_post(post: dict[str, str]) -> tuple[str, str]:
    text = post.get("text", "")
    text = re.split(r"[.!?]\s+", text.strip())[0] or text
    words = clamp_words(text, 14)
    if not words:
        return "The Post", ""
    return split_title_for_two_tone(words)


def with_bracketed_accent(headline: str, accent_word: str) -> str:
    return bracket_single_accent_word(headline, accent_word)


def contains_japanese(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text))


def is_katakana_char(value: str) -> bool:
    return bool(value and re.match(r"[\u30a0-\u30ffー]", value))


def is_ascii_word_char(value: str) -> bool:
    return bool(value and re.match(r"[A-Za-z0-9'._+-]", value))


def japanese_phrase_chunks(text: str, max_chars: int = 12) -> list[str]:
    """Create browser-safe Japanese line chunks without splitting terms mid-word."""
    text = re.sub(r"\s+", " ", text).strip()
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
    )
    soft_particles = set("でにをがはへともか")
    for index, char in enumerate(text):
        current += char
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if char in "、。！？!?":
            chunks.append(current)
            current = ""
            continue
        if len(current) >= 5 and (
            current.endswith(soft_suffixes) or char in soft_particles
        ):
            if next_char and next_char not in "、。！？!?":
                chunks.append(current)
                current = ""
                continue
        if len(current) >= max_chars:
            if (is_katakana_char(char) and is_katakana_char(next_char)) or (
                is_ascii_word_char(char) and is_ascii_word_char(next_char)
            ):
                continue
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


TERM_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9'._+-]*|[\u3400-\u9fff々〆ヵヶ]{1,6}[\u30a0-\u30ffー]+|[\u30a0-\u30ffー]+"
)


def inline_text_markup(text: str) -> str:
    pieces: list[str] = []
    position = 0
    for match in TERM_RE.finditer(text):
        pieces.append(html.escape(text[position : match.start()]))
        pieces.append(f'<span class="term">{html.escape(match.group(0))}</span>')
        position = match.end()
    pieces.append(html.escape(text[position:]))
    return "".join(pieces)


def phrase_text_markup(text: str, *, accent: str = "", max_chars: int = 12) -> str:
    if not contains_japanese(text):
        if accent and accent in text:
            before, after = text.split(accent, 1)
            return (
                inline_text_markup(before)
                + f'<span class="accent">{html.escape(accent)}</span>'
                + inline_text_markup(after)
            )
        return inline_text_markup(text)
    chunks = japanese_phrase_chunks(text, max_chars=max_chars)
    accent_used = False
    phrase_markup: list[str] = []
    for chunk in chunks:
        if accent and not accent_used and accent in chunk:
            before, after = chunk.split(accent, 1)
            inner = (
                inline_text_markup(before)
                + f'<span class="accent">{html.escape(accent)}</span>'
                + inline_text_markup(after)
            )
            accent_used = True
        else:
            inner = inline_text_markup(chunk)
        phrase_markup.append(f'<span class="jp-phrase">{inner}</span>')
    return "".join(phrase_markup)


def normalize_cover_copy(analysis: dict[str, object] | None) -> dict[str, str]:
    if not isinstance(analysis, dict):
        return {}
    raw = analysis.get("cover")
    if not isinstance(raw, dict):
        raw = analysis.get("cover_copy")
    if not isinstance(raw, dict):
        return {}
    headline = string_value(raw.get("headline"))
    if not headline:
        return {}
    cover = {
        "kicker": string_value(raw.get("kicker") or raw.get("kicker_line")),
        "headline": with_bracketed_accent(headline, string_value(raw.get("accent_word"))),
        "swipe_line": string_value(raw.get("swipe_line") or raw.get("swipe")),
    }
    if "\u2014" in cover["headline"]:
        cover["headline"] = cover["headline"].replace("\u2014", ",")
    return {key: value for key, value in cover.items() if value}


def normalize_instagram_caption(
    analysis: dict[str, object] | None,
    *,
    source_url: str,
) -> str:
    if not isinstance(analysis, dict):
        return ""
    raw_caption = analysis.get("instagram_caption")
    caption = str(raw_caption or "").strip() if raw_caption is not None else ""
    if not caption:
        return ""
    caption = caption.replace("\u2014", ",")
    caption = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in caption.splitlines())
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()
    caption = re.sub(r"\s+(?=#[A-Za-z0-9_])", "\n\n", caption, count=1)
    caption = re.sub(r"\s+Source:\s*", "\n\nSource: ", caption)
    if source_url and "source:" not in caption.lower():
        caption = f"{caption}\n\nSource: {source_url}"
    if len(caption) > 1200:
        caption = caption[:1200].rsplit(" ", 1)[0].strip()
        if source_url and "source:" not in caption.lower():
            caption = f"{caption}\n\nSource: {source_url}"
    return caption


def fallback_post_explanation(post: dict[str, str], index: int) -> dict[str, str]:
    channel = load_channel()
    if channel.language_name.lower().startswith("japanese"):
        return {
            "url": post.get("url", ""),
            "headline": "元投稿の要点",
            "body": "このあと、元投稿の画面をそのまま確認します。重要なポイントだけ先に押さえておきましょう。",
        }
    text = clean_post_text(post.get("text", ""))
    first_sentence = re.split(r"(?<=[.!?])\s+", text.strip())[0] if text else ""
    return {
        "url": post.get("url", ""),
        "headline": f"Source Note {index}",
        "body": first_sentence or "Read the original source on the next slide.",
    }


def explainer_kicker_label() -> str:
    channel = load_channel()
    if channel.language_name.lower().startswith("japanese"):
        return "要点"
    return "KEY POINT"


def normalize_post_explanations(
    analysis: dict[str, object] | None,
    posts: list[dict[str, str]],
) -> list[dict[str, str]]:
    raw_items = analysis.get("post_summaries") if isinstance(analysis, dict) else None
    raw_items = raw_items if isinstance(raw_items, list) else []
    explanations: list[dict[str, str]] = []
    for index, post in enumerate(posts, start=1):
        raw = raw_items[index - 1] if index - 1 < len(raw_items) else None
        raw = raw if isinstance(raw, dict) else {}
        headline = string_value(raw.get("headline") or raw.get("title"))
        body = string_value(raw.get("body") or raw.get("summary") or raw.get("text"))
        url = string_value(raw.get("url")) or post.get("url", "")
        if not headline or not body:
            fallback = fallback_post_explanation(post, index)
            headline = headline or fallback["headline"]
            body = body or fallback["body"]
            url = url or fallback["url"]
        body = re.sub(r"\s+", " ", body.replace("\u2014", ",")).strip()
        explanations.append(
            {
                "url": url,
                "headline": headline.replace("\u2014", ","),
                "body": body,
            }
        )
    return explanations


def manual_cover_copy(
    *,
    kicker: str | None,
    headline: str | None,
    swipe_line: str | None,
) -> dict[str, str]:
    cover: dict[str, str] = {}
    if kicker:
        cover["kicker"] = string_value(kicker)
    if headline:
        cover["headline"] = bracket_single_accent_word(string_value(headline))
    if swipe_line:
        cover["swipe_line"] = string_value(swipe_line)
    return {key: value for key, value in cover.items() if value}


def headline_markup_from_brackets(headline: str) -> tuple[str, str, bool]:
    headline = re.sub(r"\s+", " ", bracket_single_accent_word(headline)).strip()
    match = re.search(r"\[([^\[\]]+)\]", headline)
    if not match:
        return phrase_text_markup(headline), headline, False
    before = headline[: match.start()].replace("[", "").replace("]", "")
    accent = match.group(1).strip()
    after = headline[match.end() :].replace("[", "").replace("]", "")
    plain = re.sub(r"\s+", " ", f"{before}{accent}{after}").strip()
    markup = phrase_text_markup(plain, accent=accent, max_chars=11)
    return markup, plain, True


def fallback_headline_markup(post: dict[str, str], title: str | None) -> tuple[str, str]:
    headline, accent = split_title_for_two_tone(title) if title else title_from_post(post)
    title_text = " ".join(part for part in (headline, accent) if part)
    safe_headline = html.escape(headline)
    safe_accent = html.escape(accent)
    accent_markup = f' <span class="accent">{safe_accent}</span>' if safe_accent else ""
    return f"{safe_headline}{accent_markup}", title_text


def title_font_size(text: str) -> int:
    if contains_japanese(text):
        if len(text) > 42:
            return 56
        if len(text) > 34:
            return 62
        if len(text) > 26:
            return 68
        if len(text) > 18:
            return 76
        return 84
    if len(text) > 110:
        return 58
    if len(text) > 90:
        return 64
    if len(text) > 64:
        return 70
    return 84


def asset_uri(path: object) -> str:
    if isinstance(path, Path) and path.exists():
        return path.resolve().as_uri()
    return ""


def source_profile_asset_uri(context: dict[str, object]) -> str:
    source_people = context.get("source_people")
    if not isinstance(source_people, list):
        return ""
    for person in source_people:
        if not isinstance(person, dict):
            continue
        uri = asset_uri(person.get("profile_image_path"))
        if uri:
            return uri
    return ""


def gemini_post_brief(posts: list[dict[str, str]]) -> list[dict[str, str]]:
    brief: list[dict[str, str]] = []
    for post in posts[:8]:
        brief.append(
            {
                "author": post.get("author", ""),
                "handle": post.get("handle", ""),
                "text": clean_post_text(post.get("text", ""))[:1800],
                "url": post.get("url", ""),
            }
        )
    return brief


def gemini_title_analysis(
    posts: list[dict[str, str]],
    fallback_topic: str,
    api_key: str | None,
    *,
    source_type: str = "x",
) -> dict[str, object] | None:
    if not api_key:
        return None
    model = gemini_text_model()
    channel = load_channel()
    lang = channel.language_name
    brand = channel.brand_name
    voice_prompt = load_ig_voice_prompt() or channel.default_cover_voice()
    is_article = source_type == "article"
    source_description = "an article source" if is_article else "X/Twitter posts"
    source_payload_label = "Source article JSON" if is_article else "Posts JSON"
    prompt = f"""
You prepare editorial carousel title-slide metadata from {source_description}.
Use Google Search grounding when available to identify companies and their current CEOs.
Return JSON only with this exact shape:
{{
  "topic": "short topic, 4 to 10 words",
  "cover": {{
    "kicker": "short section label or handle, 1 to 3 words",
    "headline": "{lang}-native IG cover line with exactly one [accent] word",
    "accent_word": "same accent word without brackets",
    "swipe_line": "short {lang} swipe prompt"
  }},
  "instagram_caption": "short {lang} Instagram caption with one CTA, clean hashtags, and source attribution",
  "post_summaries": [
    {{
      "url": "source post URL copied exactly",
      "headline": "short {lang} explanation-slide headline",
      "body": "clear, concise {lang} explanation of this post before showing the original source"
    }}
  ],
  "companies": [
    {{"name": "Company name", "ceo_name": "Current CEO name"}}
  ]
}}

Rules:
- Write cover.kicker, cover.headline, cover.accent_word, cover.swipe_line, and
  instagram_caption in {lang}.
- Apply the {brand} Instagram voice guide below when writing cover.kicker,
  cover.headline, cover.accent_word, and cover.swipe_line.
- The cover headline must not be a neutral summary of the source. It should be a
  witty {lang} hook that earns the swipe while staying true to the source.
- cover.headline must contain exactly one bracketed accent word, like [alam].
- cover.accent_word must match the bracketed word without brackets.
- instagram_caption should be 3 to 4 short blocks separated by blank lines:
  1) one witty {lang} hook,
  2) one useful true line about what the carousel teaches,
  3) one CTA only,
  4) clean hashtags and Source: {posts[0].get("url", "")}
- Keep instagram_caption under 900 characters.
- post_summaries must include exactly one item for each source post, in the same
  order as the input. Copy each source URL exactly from the input.
- Each post_summaries headline/body pair becomes the translated explanation slide
  before the original screenshot or video. Keep it clear, concise, and useful.
- For Japanese, keep post_summaries.body to one or two short sentences. Do not
  paste a literal line-by-line translation; explain what the viewer needs to know.
- Avoid generic hype phrases like "completely change," "game-changing,"
  "ultimate guide," "must-read," "let us know in the comments below," and
  "stop scrolling."
- instagram_caption must not use markdown bullets or em dashes.
- Include at most 3 companies.
- Include a CEO only when the company is clearly involved in the post or thread.
- Prefer the current CEO over founders, product leaders, or former CEOs.
- If no company is clearly involved, return an empty companies array.
- Do not include markdown, comments, source citations, or extra keys.
- Do not use em dashes.

{brand} Instagram voice guide:
{voice_prompt}

Fallback topic: {fallback_topic}
{source_payload_label}:
{json.dumps(gemini_post_brief(posts), ensure_ascii=False)}
""".strip()
    base_payload: dict[str, object] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.35,
            "responseMimeType": "application/json",
        },
    }
    api_version = os.environ.get("GEMINI_TEXT_API_VERSION") or "v1beta"
    payloads = [
        {**base_payload, "tools": [{"google_search": {}}]},
        base_payload,
    ]
    for payload in payloads:
        response = gemini_generate_content(
            model,
            api_key,
            payload,
            api_version=api_version,
            timeout=45,
        )
        parsed = parse_json_object(extract_gemini_text(response))
        if parsed:
            return parsed
    google_warn("[google] Gemini could not return title metadata; using local topic fallback")
    return None


def normalize_gemini_companies(
    analysis: dict[str, object] | None,
    posts: list[dict[str, str]],
) -> list[dict[str, object]]:
    companies: list[dict[str, object]] = []
    seen: set[str] = set()
    raw_companies = analysis.get("companies") if isinstance(analysis, dict) else None
    if isinstance(raw_companies, list):
        for raw_company in raw_companies:
            if not isinstance(raw_company, dict):
                continue
            name = string_value(raw_company.get("name"))
            if not name:
                continue
            key = normalized_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            company: dict[str, object] = {
                "name": name,
                "query": name,
                "provider": "gemini",
            }
            ceo_name = string_value(raw_company.get("ceo_name"))
            if ceo_name:
                company["ceo_name"] = ceo_name
            companies.append(company)
            if len(companies) >= 3:
                break
    if companies:
        return companies
    fallback_companies = find_company_entities(posts, None)
    for company in fallback_companies[:1]:
        company["provider"] = "local"
    return fallback_companies[:1]


def ceos_from_companies(companies: list[dict[str, object]]) -> list[dict[str, object]]:
    ceos: list[dict[str, object]] = []
    seen: set[str] = set()
    for company in companies:
        ceo_name = string_value(company.get("ceo_name"))
        company_name = string_value(company.get("name"))
        key = normalized_key(f"{ceo_name} {company_name}")
        if not ceo_name or not key or key in seen:
            continue
        seen.add(key)
        ceos.append(
            {
                "name": ceo_name,
                "company": company_name,
                "description": "CEO",
                "query": f"{ceo_name} {company_name}",
                "provider": "gemini",
            }
        )
    return ceos


def is_person_source_post(post: dict[str, str]) -> bool:
    handle = normalized_handle(post.get("handle"))
    if not handle or handle in ORGANIZATION_SOURCE_HANDLES:
        return False
    return handle in PERSON_SOURCE_HANDLES


def source_person_from_post(post: dict[str, str]) -> dict[str, object] | None:
    if not is_person_source_post(post):
        return None
    author = string_value(post.get("author"))
    handle = normalized_handle(post.get("handle"))
    name = author if author and author != "Source post" else f"@{handle}"
    person: dict[str, object] = {
        "name": name,
        "handle": f"@{handle}",
        "query": f"{name} @{handle}".strip(),
        "provider": "source_post",
    }
    profile_image_url = normalize_x_profile_image_url(post.get("profile_image_url"))
    if profile_image_url:
        person["profile_image_url"] = profile_image_url
    return person


def download_source_profile_images(source_people: list[dict[str, object]], assets_dir: Path) -> None:
    for person in source_people:
        if person.get("profile_image_path"):
            continue
        profile_image_url = person.get("profile_image_url")
        image_path = download_image(
            profile_image_url,
            assets_dir,
            f"source-profile-{person.get('handle') or person.get('name') or 'person'}",
        )
        if image_path:
            person["profile_image_path"] = image_path


def default_title_image_prompt(
    topic: str,
    companies: list[dict[str, object]],
    ceos: list[dict[str, object]],
    source_people: list[dict[str, object]],
) -> str:
    company_line = ", ".join(string_value(company.get("name")) for company in companies if company.get("name"))
    source_bits = []
    for person in source_people:
        name = string_value(person.get("name"))
        handle = string_value(person.get("handle"))
        if name and handle:
            source_bits.append(f"{name} ({handle})")
        elif name or handle:
            source_bits.append(name or handle)
    source_line = ", ".join(source_bits)
    ceo_bits = []
    for ceo in ceos:
        ceo_name = string_value(ceo.get("name"))
        company_name = string_value(ceo.get("company"))
        if not ceo_name:
            continue
        ceo_bits.append(f"{ceo_name} of {company_name}" if company_name else ceo_name)
    ceo_line = ", ".join(ceo_bits)
    has_source_profile_image = any(
        person.get("profile_image_path") or person.get("profile_image_url")
        for person in source_people
    )
    parts = [
        f"Horizontal editorial cover art for an Instagram carousel about '{topic}'.",
        "Cream/off-white paper background (#F4F2EC).",
        "Dark ink (#16140F) and rust/terracotta accent color (#C0552E).",
        "Abstract geometric composition, premium print magazine aesthetic, textured paper, editorial gravitas, intellectual but not cold.",
        "The image must be visually relevant to the post topic, using symbolic editorial imagery rather than literal app UI.",
    ]
    if source_line:
        if has_source_profile_image:
            parts.append(
                "The source post is by this person: "
                f"{source_line}. Do not generate or invent this person's face. "
                "Their real X profile image will be composited separately on the title slide; create supporting abstract editorial art that leaves room for that avatar."
            )
        else:
            parts.append(
                "The source post is by this person: "
                f"{source_line}. Do not invent a portrait or face for them; use symbolic, non-literal editorial imagery instead."
            )
    if ceo_line and not has_source_profile_image:
        parts.append(
            f"Add a tasteful editorial portrait element of the CEO: {ceo_line}."
        )
    elif ceo_line:
        parts.append(
            f"Company leadership context: {ceo_line}. Represent this context symbolically; do not add any extra realistic human portraits."
        )
    if source_line and ceo_line and not has_source_profile_image:
        parts.append("Keep any CEO portrait secondary to the source author context.")
    if company_line:
        parts.append(f"Company context: {company_line}. Do not show logos or brand marks.")
    return " ".join(parts)


def title_image_prompt(
    topic: str,
    companies: list[dict[str, object]],
    ceos: list[dict[str, object]],
    source_people: list[dict[str, object]],
    analysis: dict[str, object] | None,
) -> str:
    prompt = default_title_image_prompt(topic, companies, ceos, source_people)
    has_source_profile_image = any(
        person.get("profile_image_path") or person.get("profile_image_url")
        for person in source_people
    )
    people_style = (
        "- Do not include human faces, portraits, headshots, silhouettes, or figure studies in the generated artwork; the real X avatar will be composited separately."
        if has_source_profile_image
        else "- Keep people as tasteful editorial portrait elements, integrated into the concept rather than corporate headshots."
    )
    return f"""
{prompt}

Format and style:
- 16:9 horizontal composition, 2048x1152.
- Make it feel like the output of generate_cover.py, not a corporate headshot.
{people_style}
- Use abstract editorial metaphors, architectural shapes, paper texture, ink wash, grain, and restrained magazine-cover composition.
- No visible text of any kind: no letters, words, numbers, labels, captions, logos, app icons, brand marks, code, UI, screenshots, charts, diagrams, flowchart boxes, badges, posters, glass-board writing, or watermark.
- Do not place any graphic or symbol that resembles text.
- Carousel typography will sit outside the image, not over it.
""".strip()


def generated_openai_image_path(out_dir: Path, topic: str, prompt: str) -> Path:
    digest = hashlib.sha1(f"openai\n{topic}\n{prompt}".encode("utf-8")).hexdigest()[:10]
    return out_dir / "title_assets" / f"openai-topic-{digest}.png"


def openai_title_image_model() -> str:
    return os.environ.get("OPENAI_IMAGE_MODEL") or DEFAULT_OPENAI_IMAGE_MODEL


def openai_title_image_size() -> str:
    return os.environ.get("OPENAI_TITLE_IMAGE_SIZE") or DEFAULT_OPENAI_TITLE_IMAGE_SIZE


def generate_openai_topic_image(
    topic: str,
    companies: list[dict[str, object]],
    ceos: list[dict[str, object]],
    source_people: list[dict[str, object]],
    analysis: dict[str, object] | None,
    out_dir: Path,
) -> tuple[Path | None, str]:
    if not openai_api_key():
        return None, ""
    prompt = title_image_prompt(topic, companies, ceos, source_people, analysis)
    path = generated_openai_image_path(out_dir, topic, prompt)
    model = openai_title_image_model()
    size = openai_title_image_size()
    if path.exists():
        print(f"[openai] using cached GPT Image title cover -> {path}")
        return path, prompt
    try:
        generate_openai(prompt, path, model=model, size=size)
    except (SystemExit, Exception) as exc:
        print(f"[openai] GPT Image title cover failed; using non-AI title fallback ({exc})")
        return None, prompt
    print(f"[openai] generated GPT Image title cover -> {path}")
    return path, prompt


def build_title_enrichment(
    posts: list[dict[str, str]],
    *,
    title: str | None,
    out_dir: Path,
    cover_kicker: str | None = None,
    cover_headline: str | None = None,
    cover_swipe_line: str | None = None,
    source_type: str = "x",
) -> dict[str, object]:
    api_key = gemini_api_key()
    kg_api_key = os.environ.get("GOOGLE_KG_API_KEY")
    topic = title or " ".join(part for part in title_from_post(posts[0]) if part).strip()
    topic = compact_topic(topic or posts[0].get("text", "Source post"))
    assets_dir = out_dir / "title_assets"
    if api_key:
        print("[google] enriching title slide with Google AI Studio / Gemini")
    else:
        print("[google] GOOGLE_API_KEY or GEMINI_API_KEY not set; using generated title visual")

    analysis = gemini_title_analysis(posts, topic, api_key, source_type=source_type)
    if isinstance(analysis, dict):
        gemini_topic = compact_topic(string_value(analysis.get("topic")))
        if gemini_topic:
            topic = gemini_topic
    cover_copy = normalize_cover_copy(analysis)
    instagram_caption = normalize_instagram_caption(
        analysis,
        source_url=posts[0].get("url", ""),
    )
    post_explanations = normalize_post_explanations(analysis, posts)
    cover_override = manual_cover_copy(
        kicker=cover_kicker,
        headline=cover_headline,
        swipe_line=cover_swipe_line,
    )
    if cover_override:
        cover_copy = {**cover_copy, **cover_override}

    source_person = source_person_from_post(posts[0])
    source_people = [source_person] if source_person else []
    companies = normalize_gemini_companies(analysis, posts)
    if source_people:
        source_keys = {
            normalized_key(string_value(person.get("name")))
            for person in source_people
            if person.get("name")
        }
        source_keys.update(
            normalized_key(string_value(person.get("handle")).lstrip("@"))
            for person in source_people
            if person.get("handle")
        )
        companies = [
            company
            for company in companies
            if normalized_key(string_value(company.get("name"))) not in source_keys
        ]
    ceos = ceos_from_companies(companies)
    download_source_profile_images(source_people, assets_dir)
    topic_image_path, generated_prompt = generate_openai_topic_image(
        topic,
        companies,
        ceos,
        source_people,
        analysis,
        out_dir,
    )
    image_provider = "openai" if topic_image_path else ""
    if not generated_prompt:
        generated_prompt = title_image_prompt(topic, companies, ceos, source_people, analysis)
    topic_entity = None

    if kg_api_key:
        print("[google] optional Knowledge Graph image lookup enabled via GOOGLE_KG_API_KEY")
        for company in companies:
            company_name = string_value(company.get("name"))
            kg_entity = first_kg_entity(
                f"{company_name} company",
                kg_api_key,
                types=("Organization",),
                reject_types=("Person",),
            )
            if not kg_entity:
                continue
            for key in ("description", "source_url", "license", "image_url"):
                if kg_entity.get(key):
                    company[key] = kg_entity[key]
            image_path = download_image(
                company.get("image_url"),
                assets_dir,
                f"company-{company.get('name', 'company')}",
            )
            if image_path:
                company["image_path"] = image_path

        for ceo in ceos:
            ceo_name = string_value(ceo.get("name"))
            company_name = string_value(ceo.get("company"))
            kg_entity = first_kg_entity(
                f"{ceo_name} {company_name}",
                kg_api_key,
                types=("Person",),
                require_image=True,
            )
            if not kg_entity:
                continue
            for key in ("description", "source_url", "license", "image_url"):
                if kg_entity.get(key):
                    ceo[key] = kg_entity[key]
            image_path = download_image(
                ceo.get("image_url"),
                assets_dir,
                f"ceo-{ceo.get('name', 'ceo')}",
            )
            if image_path:
                ceo["image_path"] = image_path

        for person in source_people:
            person_name = string_value(person.get("name"))
            person_handle = string_value(person.get("handle"))
            kg_entity = first_kg_entity(
                f"{person_name} {person_handle}",
                kg_api_key,
                types=("Person",),
                require_image=True,
            )
            if not kg_entity:
                continue
            for key in ("description", "source_url", "license", "image_url"):
                if kg_entity.get(key):
                    person[key] = kg_entity[key]
            image_path = download_image(
                person.get("image_url"),
                assets_dir,
                f"source-person-{person.get('name', 'person')}",
            )
            if image_path:
                person["image_path"] = image_path

        if not topic_image_path:
            topic_image_path = next(
                (
                    person.get("image_path")
                    for person in source_people
                    if isinstance(person.get("image_path"), Path)
                ),
                None,
            )
            image_provider = "source_person" if topic_image_path else image_provider

        if not topic_image_path:
            topic_entity = find_topic_entity(topic, companies, kg_api_key)
            if topic_entity:
                topic_image_path = download_image(
                    topic_entity.get("image_url"),
                    assets_dir,
                    f"topic-{topic_entity.get('name', 'topic')}",
                )
                image_provider = "knowledge_graph" if topic_image_path else image_provider
    if not topic_image_path:
        topic_image_path = next(
            (
                person.get("image_path")
                for person in source_people
                if isinstance(person.get("image_path"), Path)
            ),
            None,
        )
        image_provider = "source_person" if topic_image_path else image_provider
    if not topic_image_path:
        topic_image_path = next(
            (
                person.get("profile_image_path")
                for person in source_people
                if isinstance(person.get("profile_image_path"), Path)
            ),
            None,
        )
        image_provider = "source_profile" if topic_image_path else image_provider
    if not topic_image_path:
        topic_image_path = next(
            (
                company.get("image_path")
                for company in companies
                if isinstance(company.get("image_path"), Path)
            ),
            None,
        )
        image_provider = "knowledge_graph" if topic_image_path else image_provider
    if not topic_image_path:
        topic_image_path = next(
            (
                ceo.get("image_path")
                for ceo in ceos
                if isinstance(ceo.get("image_path"), Path)
            ),
            None,
        )
        image_provider = "knowledge_graph" if topic_image_path else image_provider

    context: dict[str, object] = {
        "topic": topic,
        "companies": companies,
        "ceos": ceos,
        "source_people": source_people,
        "topic_entity": topic_entity,
        "topic_image_path": topic_image_path,
        "cover_copy": cover_copy,
        "post_explanations": post_explanations,
        "instagram_caption": instagram_caption,
        "brand_voice_doc": load_channel().voice_doc_rel,
        "google_enabled": bool(api_key),
        "provider": "gemini" if api_key else "local",
        "image_provider": image_provider,
        "gemini_text_model": gemini_text_model() if api_key else "",
        "openai_image_model": openai_title_image_model() if openai_api_key() else "",
        "openai_image_size": openai_title_image_size() if openai_api_key() else "",
        "generated_image_prompt": generated_prompt,
    }
    return context


def title_visual_markup(context: dict[str, object]) -> str:
    topic_image_uri = asset_uri(context.get("topic_image_path"))
    bg_style = f' style="background-image: url({topic_image_uri})"' if topic_image_uri else ""
    source_profile_uri = source_profile_asset_uri(context)
    avatar_markup = ""
    visual_class = "visual-card"
    if str(context.get("image_provider") or "") == "article_og_image":
        # Raw article OG images are usually literal stock photos whose palette
        # clashes with the cream/terracotta brand. Duotone them so the fallback
        # cover still reads as a vibecodersph editorial card.
        visual_class += " is-og-fallback"
    if source_profile_uri:
        visual_class += " has-source-avatar"
        safe_profile_uri = html.escape(source_profile_uri, quote=True)
        avatar_markup = f"""
    <div class="source-avatar">
      <img src="{safe_profile_uri}" alt="">
    </div>"""
    return f"""
  <div class="{visual_class}">
    <div class="visual-bg"{bg_style}></div>
    <div class="visual-fallback"></div>
{avatar_markup}
  </div>
"""


def shared_css() -> str:
    return f"""
{FONTS.read_text()}

:root {{
  --bg: #F4F2EC;
  --bg-top: #E9E6DF;
  --fg: #16140F;
  --ink-soft: rgba(20, 18, 14, 0.78);
  --primary: #C0552E;
  --rule: rgba(20, 18, 14, 0.28);
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ margin: 0; background: #555; font-family: 'Archivo', sans-serif; }}
.slide {{
  width: {SLIDE_W}px;
  height: {SLIDE_H}px;
  position: relative;
  overflow: hidden;
  background: linear-gradient(180deg, var(--bg-top) 0%, #F1EEE6 48%, var(--bg) 100%);
  color: var(--fg);
}}
.kicker {{
  position: absolute;
  top: 176px;
  left: 120px;
  right: 120px;
  display: flex;
  align-items: center;
  gap: 28px;
}}
.kicker::before, .kicker::after {{
  content: '';
  flex: 1;
  height: 2px;
  background: var(--rule);
}}
.kicker em {{
  font-style: normal;
  font-size: 25px;
  font-weight: 700;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--ink-soft);
  white-space: nowrap;
}}
.dots {{
  position: absolute;
  left: 56px;
  right: 56px;
  bottom: 72px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  font-weight: 760;
  letter-spacing: 0;
  line-height: 1;
  color: var(--primary);
}}
"""


def dot_markup(active: int, count: int, swipe_line: str = "") -> str:
    if active >= count:
        return ""
    text = string_value(swipe_line)
    if not text and load_channel().language_name.lower().startswith("japanese"):
        text = "スワイプで続きへ"
    text = text or "swipe for more"
    return f"<span>{html.escape(text)}</span>"


def render_title_slide(
    post: dict[str, str],
    out_path: Path,
    count: int,
    title: str | None,
    title_context: dict[str, object],
    account_name: str,
) -> Path:
    cover_copy = title_context.get("cover_copy")
    cover_copy = cover_copy if isinstance(cover_copy, dict) else {}
    if title:
        headline_markup, title_text = fallback_headline_markup(post, title)
    elif string_value(cover_copy.get("headline")):
        headline_markup, title_text, has_accent = headline_markup_from_brackets(
            string_value(cover_copy.get("headline"))
        )
        if not has_accent:
            headline_markup, title_text = fallback_headline_markup(post, title)
    else:
        headline_markup, title_text = fallback_headline_markup(post, title)
    font_size = title_font_size(title_text)
    html_path = out_path.with_suffix(".html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    safe_account_name = html.escape(account_name.strip() or DEFAULT_ACCOUNT_NAME)
    swipe_line = string_value(cover_copy.get("swipe_line")) if not title else ""
    visual = title_visual_markup(title_context)
    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{shared_css()}
.visual-card {{
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 790px;
  overflow: hidden;
  background: #151713;
}}
.visual-bg, .visual-fallback {{
  position: absolute;
  inset: 0;
}}
.visual-bg {{
  z-index: 1;
  background-position: center;
  background-size: cover;
  filter: saturate(0.96) contrast(1.02);
}}
.visual-card.is-og-fallback .visual-bg {{
  filter: grayscale(1) contrast(1.06) brightness(1.04);
}}
.visual-card.is-og-fallback .visual-bg::after {{
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(152deg, rgba(192, 85, 46, 0.66) 0%, rgba(22, 20, 15, 0.82) 100%);
  mix-blend-mode: multiply;
}}
.visual-card::after {{
  content: '';
  position: absolute;
  z-index: 2;
  inset: 0;
  background:
    linear-gradient(180deg, rgba(244, 242, 236, 0) 42%, rgba(244, 242, 236, 0.24) 62%, var(--bg) 100%);
  pointer-events: none;
}}
.source-avatar {{
  position: absolute;
  z-index: 3;
  right: 72px;
  bottom: 86px;
  width: 316px;
  height: 316px;
  border-radius: 50%;
  padding: 10px;
  background:
    linear-gradient(135deg, rgba(192, 85, 46, 0.96), rgba(244, 242, 236, 0.86) 54%, rgba(22, 20, 15, 0.9));
  box-shadow:
    0 28px 70px rgba(22, 20, 15, 0.34),
    0 0 0 2px rgba(244, 242, 236, 0.72);
}}
.source-avatar::before {{
  content: '';
  position: absolute;
  inset: -20px;
  border-radius: 50%;
  border: 2px solid rgba(192, 85, 46, 0.34);
}}
.source-avatar img {{
  position: relative;
  width: 100%;
  height: 100%;
  display: block;
  border-radius: 50%;
  object-fit: cover;
  border: 8px solid rgba(244, 242, 236, 0.94);
}}
.visual-fallback {{
  z-index: 0;
  background:
    linear-gradient(135deg, rgba(192, 85, 46, 0.74), rgba(22, 20, 15, 0.94)),
    repeating-linear-gradient(90deg, rgba(244, 242, 236, 0.12) 0 2px, transparent 2px 18px);
}}
.title-cluster {{
  position: absolute;
  left: 56px;
  right: 56px;
  top: 742px;
  bottom: 168px;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  text-align: left;
  z-index: 3;
}}
.account-rule {{
  display: flex;
  align-items: center;
  gap: 22px;
  margin-bottom: 30px;
  color: var(--primary);
}}
.account-rule::before,
.account-rule::after {{
  content: '';
  flex: 1;
  height: 2px;
  background: var(--rule);
}}
.account-rule span {{
  font-size: 24px;
  font-weight: 820;
  letter-spacing: 0;
  line-height: 1;
  text-transform: uppercase;
  color: var(--primary);
}}
.headline {{
  font-size: {font_size}px;
  font-weight: 850;
  letter-spacing: 0;
  line-height: 1.03;
  color: var(--fg);
  line-break: strict;
  overflow-wrap: normal;
  text-wrap: balance;
  word-break: normal;
}}
.headline .accent {{
  color: var(--primary);
}}
.headline .jp-phrase {{
  display: inline-block;
}}
.headline .term {{
  white-space: nowrap;
}}
.dots {{
  bottom: 116px;
}}
</style></head>
<body>
<div class="slide">
  {visual}
  <div class="title-cluster">
    <div class="account-rule"><span>{safe_account_name}</span></div>
    <h1 class="headline">{headline_markup}</h1>
  </div>
  <div class="dots">{dot_markup(1, count, swipe_line)}</div>
</div>
</body></html>"""
    html_path.write_text(html_text)
    render_html_slide(html_path, out_path)
    return out_path


def post_explanation(
    title_context: dict[str, object],
    post_index: int,
    post: dict[str, str],
) -> dict[str, str]:
    raw_items = title_context.get("post_explanations")
    raw_items = raw_items if isinstance(raw_items, list) else []
    raw = raw_items[post_index] if post_index < len(raw_items) else None
    if isinstance(raw, dict):
        headline = string_value(raw.get("headline") or raw.get("title"))
        body = string_value(raw.get("body") or raw.get("summary") or raw.get("text"))
        if headline and body:
            return {
                "headline": headline,
                "body": body,
                "url": string_value(raw.get("url")) or post.get("url", ""),
            }
    return fallback_post_explanation(post, post_index + 1)


def render_explainer_slide(
    post: dict[str, str],
    explanation: dict[str, str],
    out_path: Path,
    active: int,
    count: int,
) -> Path:
    html_path = out_path.with_suffix(".html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    channel = load_channel()
    default_headline = "元投稿の要点" if channel.language_name.lower().startswith("japanese") else "Source Note"
    headline = explanation.get("headline", default_headline)
    body = explanation.get("body", "")
    kicker_label = html.escape(explainer_kicker_label())
    headline_markup = phrase_text_markup(headline, max_chars=10)
    body_markup = "".join(
        f"<p>{phrase_text_markup(paragraph, max_chars=17)}</p>"
        for paragraph in re.split(r"\n{2,}", body)
        if paragraph.strip()
    )
    source = html.escape(f"{post.get('author', 'Source')} {post.get('handle', '')}".strip())
    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{shared_css()}
.explainer {{
  position: absolute;
  inset: 0;
  padding: 116px 72px 132px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}}
.explainer-kicker {{
  display: flex;
  align-items: center;
  gap: 18px;
  margin-bottom: 54px;
  color: var(--primary);
}}
.explainer-kicker::before,
.explainer-kicker::after {{
  content: '';
  flex: 1;
  height: 2px;
  background: var(--rule);
}}
.explainer-kicker span {{
  font-size: 22px;
  font-weight: 820;
  letter-spacing: 0.16em;
  line-height: 1;
  text-transform: uppercase;
  white-space: nowrap;
}}
.explainer-title {{
  font-size: 74px;
  font-weight: 860;
  letter-spacing: 0;
  line-height: 1.07;
  margin-bottom: 42px;
  color: var(--fg);
  line-break: strict;
  overflow-wrap: normal;
  text-wrap: balance;
  word-break: normal;
}}
.explainer-body {{
  max-width: 924px;
  color: var(--fg);
  font-size: 43px;
  font-weight: 650;
  letter-spacing: 0;
  line-height: 1.42;
}}
.explainer-body p + p {{
  margin-top: 28px;
}}
.jp-phrase {{
  display: inline-block;
}}
.term {{
  white-space: nowrap;
}}
.source-note {{
  position: absolute;
  left: 72px;
  right: 72px;
  bottom: 154px;
  color: var(--primary);
  font-size: 22px;
  font-weight: 820;
  letter-spacing: 0.12em;
  line-height: 1;
  text-align: center;
  text-transform: uppercase;
}}
.dots {{
  bottom: 78px;
}}
</style></head>
<body>
<div class="slide">
  <section class="explainer">
    <div class="explainer-kicker"><span>{kicker_label}</span></div>
    <h1 class="explainer-title">{headline_markup}</h1>
    <div class="explainer-body">{body_markup}</div>
  </section>
  <div class="source-note">{source}</div>
  <div class="dots">{dot_markup(active, count)}</div>
</div>
</body></html>"""
    html_path.write_text(html_text)
    render_html_slide(html_path, out_path)
    return out_path


def render_post_slide(post: dict[str, str], embed_png: Path, out_path: Path, active: int, count: int) -> Path:
    html_path = out_path.with_suffix(".html")
    label = html.escape(f"{post.get('author', 'Source post')} {post.get('handle', '')}".strip())
    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
{shared_css()}
.shot-wrap {{
  position: absolute;
  top: 148px;
  left: 40px;
  width: 1000px;
  height: 1044px;
  display: flex;
  align-items: center;
  justify-content: center;
}}
.tweet-shot {{
  max-width: 100%;
  max-height: 100%;
  border-radius: 28px;
  box-shadow: 0 30px 70px rgba(20, 18, 14, 0.22);
}}
.source-label {{
  position: absolute;
  left: 120px;
  right: 120px;
  bottom: 112px;
  text-align: center;
  font-size: 23px;
  font-weight: 800;
  letter-spacing: 0.13em;
  text-transform: uppercase;
  color: var(--primary);
}}
.dots {{
  bottom: 62px;
}}
</style></head>
<body>
<div class="slide">
  <div class="shot-wrap"><img class="tweet-shot" src="{embed_png.resolve().as_uri()}"></div>
  <div class="source-label">{label}</div>
  <div class="dots">{dot_markup(active, count)}</div>
</div>
</body></html>"""
    html_path.write_text(html_text)
    render_html_slide(html_path, out_path)
    return out_path


def render_html_slide(html_path: Path, out_path: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit("playwright is required to render carousel slides") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": SLIDE_W, "height": SLIDE_H}, device_scale_factor=1)
        page.goto(html_path.resolve().as_uri())
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(300)
        page.locator(".slide").screenshot(path=str(out_path))
        browser.close()


def manifest_entity(entity: dict[str, object]) -> dict[str, object]:
    item: dict[str, object] = {}
    for key in (
        "name",
        "description",
        "query",
        "source_url",
        "license",
        "company",
        "ceo_name",
        "handle",
        "profile_image_url",
        "provider",
    ):
        value = entity.get(key)
        if value:
            item[key] = value
    image_path = entity.get("image_path")
    if isinstance(image_path, Path):
        item["image_path"] = str(image_path)
    profile_image_path = entity.get("profile_image_path")
    if isinstance(profile_image_path, Path):
        item["profile_image_path"] = str(profile_image_path)
    return item


def manifest_title_context(context: dict[str, object]) -> dict[str, object]:
    topic_image_path = context.get("topic_image_path")
    topic_entity = context.get("topic_entity")
    return {
        "topic": context.get("topic", ""),
        "cover_copy": context.get("cover_copy", {}),
        "post_explanations": context.get("post_explanations", []),
        "instagram_caption": context.get("instagram_caption", ""),
        "brand_voice_doc": context.get("brand_voice_doc", ""),
        "provider": context.get("provider", ""),
        "image_provider": context.get("image_provider", ""),
        "google_enabled": bool(context.get("google_enabled")),
        "gemini_text_model": context.get("gemini_text_model", ""),
        "openai_image_model": context.get("openai_image_model", ""),
        "openai_image_size": context.get("openai_image_size", ""),
        "generated_image_prompt": context.get("generated_image_prompt", ""),
        "topic_entity": manifest_entity(topic_entity) if isinstance(topic_entity, dict) else None,
        "topic_image_path": str(topic_image_path) if isinstance(topic_image_path, Path) else "",
        "companies": [
            manifest_entity(company)
            for company in context.get("companies", [])
            if isinstance(company, dict)
        ],
        "ceos": [
            manifest_entity(ceo)
            for ceo in context.get("ceos", [])
            if isinstance(ceo, dict)
        ],
        "source_people": [
            manifest_entity(person)
            for person in context.get("source_people", [])
            if isinstance(person, dict)
        ],
    }


def build_x_carousel(
    url: str,
    *,
    out_dir: Path,
    max_thread_posts: int,
    title: str | None,
    cover_kicker: str | None,
    cover_headline: str | None,
    cover_swipe_line: str | None,
    account_name: str,
    no_thread: bool,
    first_page_only: bool,
    cookies_from_browser: str | None,
    thread_source: str = "auto",
) -> Path:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    account_name = account_name.strip() or DEFAULT_ACCOUNT_NAME
    url = canonical_x_url(url)
    posts: list[dict[str, str]] = []
    used_thread_source = ""
    if not no_thread:
        if thread_source in ("auto", "xai"):
            posts = discover_thread_posts_xai(url, max_thread_posts)
            if posts:
                used_thread_source = "xai"
        if not posts and thread_source in ("auto", "playwright"):
            posts = discover_thread_posts(url, max_thread_posts, cookies_from_browser)
            if posts:
                used_thread_source = "playwright"
    if not posts:
        metadata = fetch_metadata(url, cookies_from_browser)
        embed_post = fetch_embed_post(url) if metadata is None else None
        posts = [embed_post or post_from_metadata(url, metadata)]
        used_thread_source = "single-post"

    first_metadata = fetch_metadata(posts[0]["url"], cookies_from_browser)
    if first_metadata:
        posts[0] = {**posts[0], **post_from_metadata(posts[0]["url"], first_metadata)}
    elif posts[0].get("text") == "Source post":
        embed_post = fetch_embed_post(posts[0]["url"])
        if embed_post:
            posts[0] = {**posts[0], **embed_post}
    if is_person_source_post(posts[0]) and not posts[0].get("profile_image_url"):
        embed_post = fetch_embed_post(posts[0]["url"])
        if embed_post and embed_post.get("profile_image_url"):
            posts[0]["profile_image_url"] = embed_post["profile_image_url"]

    total = 1 if first_page_only else 1 + (len(posts) * 2)
    slides: list[dict[str, object]] = []
    title_path = out_dir / "slide_01.png"
    title_context = build_title_enrichment(
        posts,
        title=title,
        out_dir=out_dir,
        cover_kicker=cover_kicker,
        cover_headline=cover_headline,
        cover_swipe_line=cover_swipe_line,
    )
    render_title_slide(posts[0], title_path, total, title, title_context, account_name)
    slides.append({"index": 1, "type": "title", "path": str(title_path), "source_url": posts[0]["url"]})

    if first_page_only:
        posts_to_render: list[dict[str, str]] = []
    else:
        posts_to_render = posts

    slide_index = 2
    for post_index, post in enumerate(posts_to_render):
        source_url = post["url"]
        metadata = fetch_metadata(source_url, cookies_from_browser)
        if metadata:
            post = {**post, **post_from_metadata(source_url, metadata)}
        elif post.get("text") == "Source post":
            embed_post = fetch_embed_post(source_url)
            if embed_post:
                post = {**post, **embed_post}

        explanation = post_explanation(title_context, post_index, post)
        explainer_path = out_dir / f"slide_{slide_index:02d}.png"
        render_explainer_slide(post, explanation, explainer_path, slide_index, total)
        slides.append(
            {
                "index": slide_index,
                "type": "post-explanation",
                "path": str(explainer_path),
                "source_url": source_url,
                "headline": explanation.get("headline", ""),
                "body": explanation.get("body", ""),
            }
        )
        slide_index += 1

        if metadata_has_video(metadata):
            out_path = out_dir / f"slide_{slide_index:02d}.mp4"
            frame_path = out_dir / f"slide_{slide_index:02d}_frame.png"
            poster_path = out_dir / f"slide_{slide_index:02d}_poster.png"
            build_video_slide(
                source=source_url,
                tweet_embed_file=None,
                out_path=out_path,
                frame_out=frame_path,
                poster_out=poster_path,
                caption="",
                kicker="",
                source_label=post.get("handle", ""),
                active=slide_index,
                count=total,
                fit="cover",
                fps=30,
                mute=False,
                cookies_from_browser=cookies_from_browser,
                layout="post-video",
                post_author=post.get("author"),
                post_handle=post.get("handle"),
                post_text=post.get("text"),
                post_date=post.get("date"),
            )
            slides.append(
                {
                    "index": slide_index,
                    "type": "post-video",
                    "path": str(out_path),
                    "poster": str(poster_path),
                    "source_url": source_url,
                }
            )
        else:
            status_id = post["id"] or extract_status_id(source_url)
            if not status_id:
                raise SystemExit(f"could not find status id for {source_url}")
            embed_path = out_dir / f"source_post_{slide_index:02d}.png"
            capture_embed(status_id, embed_path)
            out_path = out_dir / f"slide_{slide_index:02d}.png"
            render_post_slide(post, embed_path, out_path, slide_index, total)
            slides.append({"index": slide_index, "type": "post", "path": str(out_path), "source_url": source_url})
        slide_index += 1

    manifest = {
        "channel_id": load_channel().id,
        "source_url": url,
        "thread_source": used_thread_source,
        "thread_post_count": len(posts),
        "slide_count": total,
        "rendered_slide_count": len(slides),
        "first_page_only": first_page_only,
        "account_name": account_name,
        "title_context": manifest_title_context(title_context),
        "instagram_caption": title_context.get("instagram_caption", ""),
        "slides": slides,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[x] wrote manifest -> {manifest_path}")
    return manifest_path


def main() -> int:
    load_env_file(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="X/Twitter status URL")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--max-thread-posts", type=int, default=8)
    ap.add_argument("--title", help="Override generated title slide text")
    ap.add_argument(
        "--cover-kicker",
        help="Override the generated cover kicker metadata; visible brand line remains account name",
    )
    ap.add_argument(
        "--cover-headline",
        help="Override the generated cover headline; wrap the accent word in [brackets]",
    )
    ap.add_argument("--cover-swipe-line", help="Override the generated cover swipe line")
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
        default=os.environ.get("X_CAROUSEL_ACCOUNT_NAME"),
        help="Override the account/publisher name on the title slide (default: channel account_name)",
    )
    ap.add_argument("--no-thread", action="store_true", help="Only build from the supplied post")
    ap.add_argument(
        "--first-page-only",
        action="store_true",
        help="Render only the title/cover page after metadata is fetched",
    )
    ap.add_argument(
        "--thread-source",
        choices=("auto", "xai", "playwright"),
        default=os.environ.get("X_THREAD_SOURCE", "auto"),
        help=(
            "Thread discovery backend: xAI x_search API, Playwright page scrape, "
            "or auto (xAI when credentials exist, otherwise Playwright). "
            "Also configurable with X_THREAD_SOURCE."
        ),
    )
    ap.add_argument(
        "--cookies-from-browser",
        default=os.environ.get("X_COOKIES_FROM_BROWSER"),
        help=(
            "Use browser cookies for X thread discovery and gated media "
            "(also configurable with X_COOKIES_FROM_BROWSER)"
        ),
    )
    args = ap.parse_args()

    # Select the active channel for every load_channel() call in this process.
    if args.channel:
        os.environ["CAROUSEL_CHANNEL"] = args.channel
    channel = load_channel()
    account_name = args.account_name or channel.account_name

    if not shutil.which("ffmpeg"):
        print("warning: ffmpeg not found; video posts will fail to render", file=sys.stderr)

    build_x_carousel(
        args.url,
        out_dir=args.out_dir,
        max_thread_posts=args.max_thread_posts,
        title=args.title,
        cover_kicker=args.cover_kicker,
        cover_headline=args.cover_headline,
        cover_swipe_line=args.cover_swipe_line,
        account_name=account_name,
        no_thread=args.no_thread,
        first_page_only=args.first_page_only,
        cookies_from_browser=args.cookies_from_browser,
        thread_source=args.thread_source,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
