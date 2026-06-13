#!/usr/bin/env python3
"""Find high-signal X posts, queue them for approval, and run approved builds.

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
import urllib.request
from datetime import datetime, timezone
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
    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit(f"{path} must contain a non-empty accounts array")
    config["accounts"] = [slug_handle(str(account)) for account in accounts if slug_handle(str(account))]
    if not config["accounts"]:
        raise SystemExit(f"{path} must contain at least one usable account handle")
    config["lookback_hours"] = int(config.get("lookback_hours") or 24)
    config["max_posts_per_account"] = int(config.get("max_posts_per_account") or 5)
    config["min_score"] = int(config.get("min_score") or 55)
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

    matched_keywords = [
        keyword for keyword in config.get("include_keywords", []) if keyword in text
    ][:5]
    if matched_keywords:
        keyword_score = min(18, 4 * len(matched_keywords))
        score += keyword_score
        reasons.append("keywords: " + ", ".join(matched_keywords))

    excluded = [keyword for keyword in config.get("exclude_keywords", []) if keyword in text]
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
posts per account. Prefer posts that would make strong LLMAW carousel source material:
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


def format_candidate(candidate: dict[str, Any]) -> str:
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

    post = candidate.get("post") or {}
    reasons = "; ".join(candidate.get("score_reasons") or [])
    text = "\n".join(
        [
            "Carousel candidate",
            f"{candidate.get('score')} score - {post.get('handle', '')}",
            compact_text(str(post.get("text") or ""), 700),
            str(post.get("url") or ""),
            f"Why: {post.get('why') or reasons}",
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
) -> int:
    post = candidate.get("post") or {}
    url = str(post.get("url") or "")
    if not url:
        raise SystemExit(f"Candidate {candidate.get('id')} has no post URL")
    out_dir = builds_dir / str(candidate["id"])
    cmd = [
        sys.executable,
        str(ROOT / "build_x_carousel.py"),
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
    print(f"[build] {candidate['id']} -> {out_dir}")
    result = subprocess.run(cmd, check=False)
    candidate["build_finished_at"] = utc_now()
    candidate["build_returncode"] = result.returncode
    if result.returncode == 0:
        candidate["status"] = "built"
        candidate["manifest_path"] = str(out_dir / "manifest.json")
    else:
        candidate["status"] = "failed"
        candidate["failure"] = f"build_x_carousel.py exited {result.returncode}"
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
    posts = fetch_scout_posts(config, limit=args.limit)
    min_score = args.min_score if args.min_score is not None else config["min_score"]
    discovered, _ = merge_candidates(queue, posts, config, min_score=min_score)

    notified = 0
    if args.notify:
        for candidate in discovered:
            if candidate.get("status") != "candidate":
                continue
            if notify_telegram(candidate):
                notified += 1

    save_queue(args.queue, queue)
    print(f"[scan] {len(posts)} posts fetched; {len(discovered)} queued; {notified} notified")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scout, approve, and build X carousel candidates")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scan configured X accounts for story candidates")
    scan.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    scan.add_argument("--limit", type=int, default=20)
    scan.add_argument("--min-score", type=int)
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
