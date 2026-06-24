#!/usr/bin/env python3
"""Publish a generated reel manifest to TikTok via the Content Posting API.

This is the TikTok analog of ``instagram_publish.py``. It reads the same reel
manifest the scheduler writes (one video slide), then posts it to TikTok in one
of two modes:

- ``direct`` (``/v2/post/publish/video/init/``, scope ``video.publish``): the
  video is posted straight to the account. A ``creator_info`` preflight returns
  the privacy levels the account allows. **Unaudited apps may only post
  privately** (``SELF_ONLY``); TikTok rejects public posts from an unaudited
  client with ``unaudited_client_can_only_post_to_private_accounts``.
- ``inbox`` (``/v2/post/publish/inbox/video/init/``, scope ``video.upload``):
  the video lands as a draft in the user's TikTok notifications. The user opens
  it, writes/edits the caption, picks a privacy level, and taps post. This is
  the only path that yields a *public* post on an unaudited app, so it is the
  default. The caption built by the scheduler is written to ``caption.txt`` for
  the user to paste.

The bytes reach TikTok one of two ways (``--source``):

- ``file`` (default): direct chunked ``FILE_UPLOAD`` of the local mp4. Needs no
  domain verification, so it works out of the box.
- ``pull``: upload the mp4 to Cloudflare R2 first (the same path Instagram
  uses), then hand TikTok a ``PULL_FROM_URL``. The R2 public domain must be
  verified in the TikTok developer portal first.

Dry run (no credentials, no network):
    uv run python tiktok_publish.py out/.../manifest.json --dry-run

Real publish (after TIKTOK_SETUP.md):
    export TIKTOK_ACCESS_TOKEN=act....
    uv run python tiktok_publish.py out/.../manifest.json --mode inbox
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Reuse Instagram's hardened R2 upload + env/manifest plumbing rather than
# duplicating it; only the TikTok API surface below is new.
import instagram_publish as ig
from channel import load_channel
from fetch_tweet_data import load_env_file

ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT_NAME = "tiktok_publish.json"

TIKTOK_API_ROOT = "https://open.tiktokapis.com"
CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
DIRECT_INIT_PATH = "/v2/post/publish/video/init/"
INBOX_INIT_PATH = "/v2/post/publish/inbox/video/init/"
STATUS_FETCH_PATH = "/v2/post/publish/status/fetch/"

# Posting modes and how the bytes are transferred.
MODE_DIRECT = "direct"
MODE_INBOX = "inbox"
SOURCE_FILE = "file"
SOURCE_PULL = "pull"

DEFAULT_PRIVACY = "SELF_ONLY"
PRIVACY_LEVELS = {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
TITLE_MAX_RUNES = 2200

# TikTok caps a single FILE_UPLOAD chunk at 64 MiB; reels are far smaller, so we
# always send the whole file as one chunk when it fits, else fail loudly.
MAX_SINGLE_CHUNK_BYTES = 64 * 1024 * 1024

# status/fetch lifecycle. The success terminal differs by mode: a direct post
# finishes at PUBLISH_COMPLETE, an inbox draft at SEND_TO_USER_INBOX.
STATUS_PUBLISH_COMPLETE = "PUBLISH_COMPLETE"
STATUS_SEND_TO_USER_INBOX = "SEND_TO_USER_INBOX"
STATUS_FAILED = "FAILED"
SUCCESS_STATUSES = {MODE_DIRECT: STATUS_PUBLISH_COMPLETE, MODE_INBOX: STATUS_SEND_TO_USER_INBOX}


class TikTokPublishError(RuntimeError):
    """Raised when TikTok rejects a request or a publish ends in FAILED."""


@dataclass
class VideoItem:
    local_path: str
    video_size: int
    public_url: str = ""


# --------------------------------------------------------------------------- #
# Credentials                                                                  #
# --------------------------------------------------------------------------- #
def channel_tiktok_access_token(channel_id: str) -> str:
    """Per-channel token (TIKTOK_ACCESS_TOKEN_<CHANNEL>) then the global one."""
    return ig.channel_env_value(channel_id, "TIKTOK_ACCESS_TOKEN", "TIKTOK_TOKEN")


def resolve_tiktok_access_token(
    explicit: str,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> tuple[str, str]:
    if explicit.strip():
        return explicit.strip(), "cli"
    channel_id = ig.manifest_channel_id(manifest, manifest_path)
    channel_value = channel_tiktok_access_token(channel_id)
    if channel_value:
        return channel_value, f"channel:{channel_id}"
    return ig.env_value("TIKTOK_ACCESS_TOKEN", "TIKTOK_TOKEN"), "env:TIKTOK_ACCESS_TOKEN"


# --------------------------------------------------------------------------- #
# Manifest -> video + caption                                                  #
# --------------------------------------------------------------------------- #
def video_item_from_manifest(manifest: dict[str, Any], manifest_path: Path) -> VideoItem:
    slides = manifest.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SystemExit("Manifest has no slides to publish")
    videos = [s for s in slides if isinstance(s, dict)]
    if len(videos) != 1:
        raise SystemExit(
            f"TikTok expects exactly one video per post; manifest has {len(videos)} slide(s)"
        )
    local_path = ig.absolute_slide_path(videos[0].get("path"), manifest_path)
    if not local_path.exists():
        raise SystemExit(f"Reel video does not exist: {local_path}")
    if local_path.suffix.lower() not in VIDEO_SUFFIXES:
        raise SystemExit(f"Unsupported TikTok video file type: {local_path}")
    return VideoItem(local_path=str(local_path), video_size=local_path.stat().st_size)


def read_caption(args: argparse.Namespace, manifest: dict[str, Any]) -> str:
    if args.caption_file:
        text = args.caption_file.read_text().strip()
    elif args.caption is not None:
        text = args.caption.strip()
    else:
        text = str(
            manifest.get("tiktok_caption")
            or manifest.get("instagram_caption")
            or manifest.get("topic")
            or ""
        ).strip()
    # TikTok counts UTF-16 code units; trim defensively so init never 400s.
    if len(text) > TITLE_MAX_RUNES:
        text = text[:TITLE_MAX_RUNES]
    return text


# --------------------------------------------------------------------------- #
# TikTok HTTP                                                                   #
# --------------------------------------------------------------------------- #
def tiktok_request(
    path: str,
    *,
    access_token: str,
    body: dict[str, Any] | None = None,
    method: str = "POST",
    timeout: int = 60,
) -> dict[str, Any]:
    url = f"{TIKTOK_API_ROOT}{path}"
    data = json.dumps(body or {}).encode("utf-8") if method.upper() == "POST" else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "carousel-app/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise SystemExit(f"TikTok API error {exc.code}: {raw[:400]}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"TikTok API request failed: {exc}") from exc
    error = payload.get("error") if isinstance(payload, dict) else None
    code = str(error.get("code")) if isinstance(error, dict) else ""
    if code and code != "ok":
        message = error.get("message") if isinstance(error, dict) else ""
        log_id = error.get("log_id") if isinstance(error, dict) else ""
        raise SystemExit(f"TikTok API error [{code}]: {message} (log_id={log_id})")
    return payload


def query_creator_info(access_token: str) -> dict[str, Any]:
    payload = tiktok_request(CREATOR_INFO_PATH, access_token=access_token)
    return payload.get("data") if isinstance(payload.get("data"), dict) else {}


def source_info(item: VideoItem, source: str) -> dict[str, Any]:
    if source == SOURCE_PULL:
        return {"source": "PULL_FROM_URL", "video_url": item.public_url}
    if item.video_size > MAX_SINGLE_CHUNK_BYTES:
        raise SystemExit(
            f"Reel is {item.video_size} bytes; FILE_UPLOAD single chunk caps at "
            f"{MAX_SINGLE_CHUNK_BYTES}. Use --source pull or chunk the upload."
        )
    return {
        "source": "FILE_UPLOAD",
        "video_size": item.video_size,
        "chunk_size": item.video_size,
        "total_chunk_count": 1,
    }


def post_info(*, title: str, privacy_level: str, args: argparse.Namespace) -> dict[str, Any]:
    return {
        "title": title,
        "privacy_level": privacy_level,
        "disable_comment": bool(args.disable_comment),
        "disable_duet": bool(args.disable_duet),
        "disable_stitch": bool(args.disable_stitch),
    }


def init_body(
    *,
    mode: str,
    item: VideoItem,
    source: str,
    title: str,
    privacy_level: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    body: dict[str, Any] = {"source_info": source_info(item, source)}
    if mode == MODE_DIRECT:
        body["post_info"] = post_info(title=title, privacy_level=privacy_level, args=args)
    return body


def upload_file_chunk(upload_url: str, item: VideoItem, *, timeout: int) -> int:
    """PUT the whole reel as a single chunk to the upload_url init handed back."""
    data = Path(item.local_path).read_bytes()
    size = len(data)
    request = urllib.request.Request(
        upload_url,
        data=data,
        method="PUT",
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
            "User-Agent": "carousel-app/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"TikTok chunk upload failed: HTTP {exc.code} {raw[:300]}") from exc
    except OSError as exc:
        raise SystemExit(f"TikTok chunk upload failed: {exc}") from exc


def fetch_status(publish_id: str, *, access_token: str) -> dict[str, Any]:
    payload = tiktok_request(
        STATUS_FETCH_PATH,
        access_token=access_token,
        body={"publish_id": publish_id},
        timeout=30,
    )
    return payload.get("data") if isinstance(payload.get("data"), dict) else {}


def wait_for_publish(
    publish_id: str,
    *,
    mode: str,
    access_token: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    success = SUCCESS_STATUSES[mode]
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = fetch_status(publish_id, access_token=access_token)
        status = str(last.get("status") or "")
        if status == success:
            return last
        if status == STATUS_FAILED:
            raise TikTokPublishError(
                f"TikTok publish {publish_id} failed: {last.get('fail_reason') or last}"
            )
        print(f"[tiktok] publish {publish_id} status={status or 'pending'}")
        time.sleep(interval_seconds)
    raise TikTokPublishError(f"Timed out waiting for TikTok publish {publish_id}: {last}")


def post_permalink(status_payload: dict[str, Any], creator_info: dict[str, Any]) -> str:
    """Build a tiktok.com URL once moderation exposes a public post id."""
    ids = status_payload.get("publicaly_available_post_id")
    post_id = ""
    if isinstance(ids, list) and ids:
        post_id = str(ids[0])
    username = str(creator_info.get("creator_username") or "").strip()
    if post_id and username:
        return f"https://www.tiktok.com/@{username}/video/{post_id}"
    return ""


def publish_to_tiktok(
    item: VideoItem,
    *,
    mode: str,
    source: str,
    title: str,
    privacy_level: str,
    access_token: str,
    creator_info: dict[str, Any],
    args: argparse.Namespace,
    wait_timeout: int,
    wait_interval: int,
) -> dict[str, Any]:
    init_path = DIRECT_INIT_PATH if mode == MODE_DIRECT else INBOX_INIT_PATH
    body = init_body(
        mode=mode, item=item, source=source, title=title, privacy_level=privacy_level, args=args
    )
    print(f"[tiktok] init {mode} post ({source}) -> {init_path}")
    init = tiktok_request(init_path, access_token=access_token, body=body)
    data = init.get("data") if isinstance(init.get("data"), dict) else {}
    publish_id = str(data.get("publish_id") or "")
    if not publish_id:
        raise SystemExit(f"TikTok did not return a publish_id: {init}")

    if source == SOURCE_FILE:
        upload_url = str(data.get("upload_url") or "")
        if not upload_url:
            raise SystemExit(f"TikTok FILE_UPLOAD init returned no upload_url: {init}")
        print(f"[tiktok] uploading {item.video_size} bytes")
        upload_file_chunk(upload_url, item, timeout=wait_timeout)

    status = wait_for_publish(
        publish_id,
        mode=mode,
        access_token=access_token,
        timeout_seconds=wait_timeout,
        interval_seconds=wait_interval,
    )
    permalink = post_permalink(status, creator_info)
    return {
        "publish_id": publish_id,
        "published": {"id": permalink_post_id(status) or publish_id},
        "permalink": {"permalink": permalink},
        "status": status,
    }


def permalink_post_id(status_payload: dict[str, Any]) -> str:
    ids = status_payload.get("publicaly_available_post_id")
    if isinstance(ids, list) and ids:
        return str(ids[0])
    return ""


# --------------------------------------------------------------------------- #
# Insights (read-only analytics for sync-insights)                             #
# --------------------------------------------------------------------------- #
def query_video_metrics(post_ids: list[str], *, access_token: str) -> dict[str, Any]:
    """Pull view/like/comment/share counts for the app's own posted videos.

    Uses ``/v2/video/query/`` (scope ``video.list``). Returns the raw payload;
    ``parse_video_metrics`` maps it onto the ledger's insight columns.
    """
    fields = "id,view_count,like_count,comment_count,share_count"
    return tiktok_request(
        f"/v2/video/query/?fields={fields}",
        access_token=access_token,
        body={"filters": {"video_ids": post_ids}},
        timeout=30,
    )


def parse_video_metrics(payload: dict[str, Any], post_id: str) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    videos = data.get("videos") if isinstance(data.get("videos"), list) else []
    for video in videos:
        if isinstance(video, dict) and str(video.get("id")) == post_id:
            return {
                "views": video.get("view_count"),
                "likes": video.get("like_count"),
                "comments": video.get("comment_count"),
                "shares": video.get("share_count"),
            }
    return {}


# --------------------------------------------------------------------------- #
# Report                                                                        #
# --------------------------------------------------------------------------- #
def api_steps(*, mode: str, source: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    init_path = DIRECT_INIT_PATH if mode == MODE_DIRECT else INBOX_INIT_PATH
    steps: list[dict[str, Any]] = []
    if mode == MODE_DIRECT:
        steps.append({"action": "query_creator_info", "path": CREATOR_INFO_PATH})
    steps.append({"action": "init_publish", "path": init_path, "body": body})
    if source == SOURCE_FILE:
        steps.append({"action": "put_video_chunk", "to": "<upload_url from init>"})
    steps.append({"action": "fetch_status", "path": STATUS_FETCH_PATH, "until": SUCCESS_STATUSES[mode]})
    return steps


def build_report(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    item: VideoItem,
    title: str,
    mode: str,
    source: str,
    privacy_level: str,
    dry_run: bool,
    access_token_source: str,
    creator_info: dict[str, Any],
    init_body_preview: dict[str, Any],
    uploads: list[dict[str, Any]],
    result: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "created_at": ig.utc_now(),
        "platform": "tiktok",
        "dry_run": dry_run,
        "manifest_path": str(manifest_path),
        "channel_id": manifest.get("channel_id"),
        "account_name": manifest.get("account_name"),
        "source_url": manifest.get("source_url"),
        "mode": mode,
        "source": source,
        "privacy_level": privacy_level if mode == MODE_DIRECT else "(set by user in app)",
        "title": title,
        "access_token_source": access_token_source,
        "creator_info": creator_info,
        "media": asdict(item),
        "uploads": uploads,
        "api_steps": api_steps(mode=mode, source=source, body=init_body_preview),
        "result": result or {},
    }


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a reel manifest to TikTok")
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--mode",
        choices=(MODE_INBOX, MODE_DIRECT),
        default=ig.env_value("TIKTOK_POST_MODE") or MODE_INBOX,
        help="inbox = drop a draft for the user to finish (default, works unaudited); "
        "direct = post straight to the account (unaudited apps forced to SELF_ONLY)",
    )
    parser.add_argument(
        "--source",
        choices=(SOURCE_FILE, SOURCE_PULL),
        default=ig.env_value("TIKTOK_UPLOAD_SOURCE") or SOURCE_FILE,
        help="file = direct chunked upload (no domain verification); "
        "pull = upload to R2 then PULL_FROM_URL (R2 domain must be verified)",
    )
    parser.add_argument(
        "--privacy-level",
        choices=sorted(PRIVACY_LEVELS),
        default=ig.env_value("TIKTOK_PRIVACY_LEVEL") or DEFAULT_PRIVACY,
        help="Direct-post privacy. Unaudited apps may only use SELF_ONLY.",
    )
    parser.add_argument("--disable-comment", action="store_true")
    parser.add_argument("--disable-duet", action="store_true")
    parser.add_argument("--disable-stitch", action="store_true")
    parser.add_argument("--caption", help="Override caption/title (default: manifest caption)")
    parser.add_argument("--caption-file", type=Path, help="Read the caption from a text file")
    parser.add_argument("--access-token", default="", help="Override TikTok user access token")
    # R2 args mirror instagram_publish.py so --source pull reuses its uploader.
    parser.add_argument("--upload-r2", action="store_true", help="(implied by --source pull)")
    parser.add_argument("--r2-bucket", default=ig.env_value("R2_BUCKET"))
    parser.add_argument("--r2-key-prefix")
    parser.add_argument("--r2-public-base-url", default=ig.env_value("R2_PUBLIC_BASE_URL"))
    parser.add_argument("--r2-timeout", type=int, default=120)
    parser.add_argument("--wait-timeout", type=int, default=600)
    parser.add_argument("--wait-interval", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="Validate and write a plan only")
    parser.add_argument("--out", type=Path, help=f"Report path (default: {DEFAULT_REPORT_NAME})")
    parser.add_argument("--print-json", action="store_true")
    return parser


def main() -> int:
    load_env_file(ROOT / ".env")
    args = build_parser().parse_args()
    manifest_path = args.manifest.resolve()
    manifest = ig.load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise SystemExit("Manifest JSON must be an object")

    item = video_item_from_manifest(manifest, manifest_path)
    title = read_caption(args, manifest)
    access_token, token_source = resolve_tiktok_access_token(args.access_token, manifest, manifest_path)
    mode = args.mode
    source = SOURCE_PULL if args.upload_r2 else args.source
    privacy_level = args.privacy_level

    uploads: list[dict[str, Any]] = []
    creator_info: dict[str, Any] = {}

    if source == SOURCE_PULL and not args.dry_run:
        media_item = ig.MediaItem(
            index=1,
            kind="video",
            local_path=item.local_path,
            public_url="",
            slide_type="video",
            source_url=str(manifest.get("source_url") or ""),
        )
        uploads = ig.upload_media_to_r2(
            [media_item],
            ig.r2_config(args, manifest_path, args.r2_public_base_url.strip()),
            timeout=args.r2_timeout,
        )
        item.public_url = media_item.public_url

    init_body_preview = init_body(
        mode=mode, item=item, source=source, title=title, privacy_level=privacy_level, args=args
    )

    result: dict[str, Any] | None = None
    if args.dry_run:
        print(f"[tiktok] dry run: {mode} post via {source.upper()} ({item.video_size} bytes)")
    else:
        if not access_token:
            raise SystemExit("TIKTOK_ACCESS_TOKEN or --access-token is required to publish")
        if mode == MODE_DIRECT:
            creator_info = query_creator_info(access_token)
            allowed = creator_info.get("privacy_level_options")
            if isinstance(allowed, list) and allowed and privacy_level not in allowed:
                raise SystemExit(
                    f"privacy_level {privacy_level} not allowed for this account. "
                    f"Allowed: {allowed}. Unaudited apps must use SELF_ONLY."
                )
        result = publish_to_tiktok(
            item,
            mode=mode,
            source=source,
            title=title,
            privacy_level=privacy_level,
            access_token=access_token,
            creator_info=creator_info,
            args=args,
            wait_timeout=args.wait_timeout,
            wait_interval=args.wait_interval,
        )

    report = build_report(
        manifest_path=manifest_path,
        manifest=manifest,
        item=item,
        title=title,
        mode=mode,
        source=source,
        privacy_level=privacy_level,
        dry_run=args.dry_run,
        access_token_source=token_source,
        creator_info=creator_info,
        init_body_preview=init_body_preview,
        uploads=uploads,
        result=result,
    )
    report_path = args.out or manifest_path.with_name(DEFAULT_REPORT_NAME)
    ig.write_json(report_path, report)
    print(f"[tiktok] wrote report -> {report_path}")
    if args.print_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
