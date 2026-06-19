#!/usr/bin/env python3
"""Fetch tweet content + metadata via xAI Responses API with x_search.

Auth resolution order:
  1. XAI_API_KEY environment variable (also read from ./.env)
  2. Hermes xAI OAuth token from ~/.hermes/auth.json (SuperGrok/X Premium+)

Usage:
    uv run python fetch_tweet_data.py https://x.com/bcherny/status/2064431111154053187
    uv run python fetch_tweet_data.py 2064431111154053187 --out tweet.json
    uv run python fetch_tweet_data.py <url-or-id> --thread --max-posts 12
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
DEFAULT_XAI_MODEL = "grok-4.3"


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


def extract_tweet_id(raw: str) -> str:
    m = re.search(r"(?:status/|^)(\d{10,})", raw)
    if not m:
        raise SystemExit(f"Could not extract tweet ID from: {raw}")
    return m.group(1)


def compact_number(value: int | None) -> str:
    if value is None:
        return ""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return str(value)


def get_hermes_token() -> str:
    """Get newest xAI OAuth access token from Hermes auth.json credential pool."""
    auth_path = Path.home() / ".hermes" / "auth.json"
    if not auth_path.exists():
        return ""

    try:
        data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    pool = data.get("credential_pool", {})

    best_token = ""
    best_refresh = ""
    for entry in pool.get("xai-oauth", []):
        refresh = entry.get("last_refresh", "")
        token = entry.get("access_token", "")
        status = entry.get("last_status", "")
        if token and refresh > best_refresh and status != "exhausted":
            best_token = token
            best_refresh = refresh

    return best_token


def resolve_xai_token(*, required: bool = True) -> str:
    """XAI_API_KEY first, then the Hermes OAuth pool. Empty string when absent."""
    token = os.environ.get("XAI_API_KEY", "").strip() or get_hermes_token()
    if not token and required:
        raise SystemExit(
            "No xAI credentials found. Set XAI_API_KEY or run: hermes auth add xai-oauth"
        )
    return token


def xai_model() -> str:
    return os.environ.get("XAI_TWEET_MODEL") or DEFAULT_XAI_MODEL


def xai_responses_with_usage(
    prompt: str,
    token: str,
    *,
    timeout: int = 90,
) -> tuple[str, dict[str, Any]]:
    """Call the xAI Responses API with x_search and return text plus usage."""
    model = xai_model()
    payload = {
        "model": model,
        "input": prompt,
        "tools": [{"type": "x_search"}],
    }
    req = urllib.request.Request(
        XAI_RESPONSES_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        raise SystemExit(f"xAI API error {e.code}: {body[:500]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error: {e.reason}")

    text_content = ""
    for item in result.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text_content += part.get("text", "")

    if not text_content:
        raise SystemExit("xAI returned no content")
    usage = result.get("usage") if isinstance(result, dict) else {}
    if isinstance(usage, dict):
        usage = dict(usage)
    else:
        usage = {}
    usage.setdefault("model", model)
    return text_content, usage


def xai_responses_text(prompt: str, token: str, *, timeout: int = 90) -> str:
    """Call the xAI Responses API with x_search and return the output text."""
    text_content, _usage = xai_responses_with_usage(prompt, token, timeout=timeout)
    return text_content


def extract_json_value(text: str, opener: str, closer: str) -> Any:
    start = text.find(opener)
    end = text.rfind(closer)
    if start < 0 or end <= start:
        raise SystemExit(f"Could not parse JSON from response:\n{text[:500]}")
    return json.loads(text[start : end + 1])


def normalize_post(data: dict[str, Any], fallback_id: str = "") -> dict[str, Any]:
    """Map a raw model-returned post object to the stable output shape."""
    tweet_id = str(data.get("id") or fallback_id).strip()
    author = str(data.get("author_name") or data.get("author") or "").strip()
    handle = str(data.get("handle") or "").strip().lstrip("@")

    def count(key: str) -> int:
        value = data.get(key)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    likes = count("likes")
    retweets = count("retweets")
    replies = count("replies")
    views = count("views")
    return {
        "id": tweet_id,
        "text": str(data.get("full_text") or data.get("text") or "").strip(),
        "author": author,
        "handle": f"@{handle}" if handle else "",
        "date": str(data.get("date") or "").strip(),
        "likes": likes,
        "retweets": retweets,
        "replies": replies,
        "views": views,
        "has_video": bool(data.get("has_video")),
        "url": f"https://x.com/{handle}/status/{tweet_id}" if handle and tweet_id else "",
        "likes_fmt": compact_number(likes),
        "retweets_fmt": compact_number(retweets),
        "replies_fmt": compact_number(replies),
        "views_fmt": compact_number(views) if views else "",
    }


POST_FIELDS_SPEC = (
    '"id": "tweet id as a string", '
    '"full_text": "the complete tweet text", '
    '"author_name": "display name", '
    '"handle": "@username", '
    '"date": "Mon D, YYYY", '
    '"likes": number, '
    '"retweets": number, '
    '"replies": number, '
    '"views": number, '
    '"has_video": boolean (true only when the tweet itself contains a video)'
)


def fetch_tweet(tweet_id: str, token: str) -> dict[str, Any]:
    """Fetch a single tweet via xAI Responses API with x_search."""
    prompt = (
        f"Look up tweet {tweet_id} on X/Twitter. "
        f"Return ONLY a raw JSON object (no markdown, no code fences) "
        f"with these exact fields: {POST_FIELDS_SPEC}"
    )
    text_content = xai_responses_text(prompt, token, timeout=45)
    data = extract_json_value(text_content, "{", "}")
    if not isinstance(data, dict):
        raise SystemExit("xAI returned JSON that is not an object")
    post = normalize_post(data, fallback_id=tweet_id)
    post["id"] = post["id"] or tweet_id
    return post


def fetch_thread(tweet_id: str, token: str, *, max_posts: int = 25) -> list[dict[str, Any]]:
    """Fetch every same-author post of the thread containing tweet_id, in order.

    Returns at least one post (the target tweet) on success. Posts are sorted
    by tweet id ascending, which is chronological for X snowflake ids.
    """
    prompt = (
        f"Tweet {tweet_id} on X/Twitter may be part of a thread: a chain of "
        f"consecutive posts where the SAME author replies to their own previous post. "
        f"Find the complete thread it belongs to, from the first post of the thread "
        f"to the last, including posts before and after tweet {tweet_id}. "
        f"Include ONLY posts authored by the thread author replying to themselves; "
        f"exclude replies from other accounts and exclude the author's replies to "
        f"other people. If the tweet is not part of a thread, return just that tweet. "
        f"Return ONLY a raw JSON array (no markdown, no code fences) ordered first "
        f"post to last, where each element has these exact fields: {POST_FIELDS_SPEC}"
    )
    text_content = xai_responses_text(prompt, token, timeout=120)
    data = extract_json_value(text_content, "[", "]")
    if not isinstance(data, list):
        raise SystemExit("xAI returned JSON that is not an array")

    posts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        post = normalize_post(item)
        if not re.fullmatch(r"\d{10,}", post["id"]) or post["id"] in seen:
            continue
        if not post["text"]:
            continue
        seen.add(post["id"])
        posts.append(post)

    posts.sort(key=lambda post: int(post["id"]))

    # The thread must contain the requested tweet; otherwise trust nothing.
    if not any(post["id"] == tweet_id for post in posts):
        single = fetch_tweet(tweet_id, token)
        return [single]

    # Threads have one author: keep the target's handle only.
    target = next(post for post in posts if post["id"] == tweet_id)
    target_handle = target["handle"].lower()
    if target_handle:
        posts = [
            post
            for post in posts
            if not post["handle"] or post["handle"].lower() == target_handle
        ]

    return posts[:max_posts]


def main() -> int:
    load_env_file(ROOT / ".env")
    ap = argparse.ArgumentParser(
        description="Fetch tweet content via xAI Responses API"
    )
    ap.add_argument("tweet", help="Tweet URL or ID")
    ap.add_argument("--out", "-o", type=Path, help="Save JSON to file instead of stdout")
    ap.add_argument(
        "--thread",
        action="store_true",
        help="Fetch the full same-author thread containing the tweet",
    )
    ap.add_argument(
        "--max-posts",
        type=int,
        default=25,
        help="Maximum thread posts to return (with --thread)",
    )
    args = ap.parse_args()

    tweet_id = extract_tweet_id(args.tweet)
    token = resolve_xai_token()

    if args.thread:
        print(f"Fetching thread for tweet {tweet_id} via xAI...", file=sys.stderr)
        data: Any = fetch_thread(tweet_id, token, max_posts=args.max_posts)
    else:
        print(f"Fetching tweet {tweet_id} via xAI...", file=sys.stderr)
        data = fetch_tweet(tweet_id, token)

    output = json.dumps(data, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(output + "\n")
        print(f"Saved {args.out}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
