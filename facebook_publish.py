#!/usr/bin/env python3
"""Publish a generated reel manifest as a Facebook Page Reel.

This is intentionally separate from ``instagram_publish.py``. It reads the same
one-video scheduler manifest, starts a Facebook Page Reel upload session, sends
the local MP4 bytes, then finishes the Reel as published.

Dry run:
    uv run python facebook_publish.py out/.../manifest.json --dry-run

Real publish:
    export FACEBOOK_PAGE_ID=123...
    export FACEBOOK_PAGE_ACCESS_TOKEN=EA...
    uv run python facebook_publish.py out/.../manifest.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import instagram_publish as ig
from fetch_tweet_data import load_env_file

ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT_NAME = "facebook_publish.json"
VIDEO_SUFFIXES = {".mp4", ".mov"}


@dataclass
class FacebookVideoItem:
    local_path: str
    video_size: int
    slide_type: str


def channel_facebook_page_id(channel_id: str) -> str:
    value = ig.channel_env_value(channel_id, "FACEBOOK_PAGE_ID", "FB_PAGE_ID")
    if value:
        return value
    publishing = ig.channel_publishing(channel_id)
    settings = publishing.get("facebook_reels") if isinstance(publishing.get("facebook_reels"), dict) else {}
    return str(
        settings.get("page_id")
        or publishing.get("facebook_page_id")
        or publishing.get("fb_page_id")
        or ""
    ).strip()


def channel_facebook_access_token(channel_id: str) -> str:
    return ig.channel_env_value(
        channel_id,
        "FACEBOOK_PAGE_ACCESS_TOKEN",
        "FACEBOOK_ACCESS_TOKEN",
        "META_PAGE_ACCESS_TOKEN",
        "META_SYSTEM_USER_ACCESS_TOKEN",
        "INSTAGRAM_ACCESS_TOKEN",
        "IG_ACCESS_TOKEN",
    )


def resolve_facebook_page_id(
    explicit: str,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> tuple[str, str]:
    if explicit.strip():
        return explicit.strip(), "cli"
    channel_id = ig.manifest_channel_id(manifest, manifest_path)
    channel_value = channel_facebook_page_id(channel_id)
    if channel_value:
        return channel_value, f"channel:{channel_id}"
    fallback = ig.env_value("FACEBOOK_PAGE_ID", "FB_PAGE_ID")
    return fallback, "env:FACEBOOK_PAGE_ID"


def resolve_facebook_access_token(
    explicit: str,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> tuple[str, str]:
    if explicit.strip():
        return explicit.strip(), "cli"
    channel_id = ig.manifest_channel_id(manifest, manifest_path)
    channel_value = channel_facebook_access_token(channel_id)
    if channel_value:
        return channel_value, f"channel:{channel_id}"
    fallback = ig.env_value(
        "FACEBOOK_PAGE_ACCESS_TOKEN",
        "FACEBOOK_ACCESS_TOKEN",
        "META_PAGE_ACCESS_TOKEN",
        "META_SYSTEM_USER_ACCESS_TOKEN",
        "INSTAGRAM_ACCESS_TOKEN",
        "IG_ACCESS_TOKEN",
    )
    return fallback, "env:FACEBOOK_PAGE_ACCESS_TOKEN"


def resolve_page_access_token_for_publish(
    *,
    page_id: str,
    access_token: str,
    access_token_source: str,
    graph_version: str,
    graph_api_root: str,
) -> tuple[str, str]:
    response = ig.graph_request(
        page_id,
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={"fields": "access_token"},
        method="GET",
        timeout=30,
        api_name="Facebook",
    )
    page_access_token = str(response.get("access_token") or "").strip()
    if not page_access_token:
        raise SystemExit(
            "Facebook did not return a Page access token. "
            "Use FACEBOOK_PAGE_ACCESS_TOKEN or grant the Meta token Page access."
        )
    return page_access_token, f"{access_token_source}->page:{page_id}"


def video_item_from_manifest(manifest: dict[str, Any], manifest_path: Path) -> FacebookVideoItem:
    slides = manifest.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SystemExit("Manifest has no slides to publish")
    usable = [slide for slide in slides if isinstance(slide, dict)]
    if len(usable) != 1:
        raise SystemExit(f"Facebook Reels expect exactly one video slide; manifest has {len(usable)}")
    slide = usable[0]
    local_path = ig.absolute_slide_path(slide.get("path"), manifest_path)
    if not local_path.exists():
        raise SystemExit(f"Reel video does not exist: {local_path}")
    if local_path.suffix.lower() not in VIDEO_SUFFIXES:
        raise SystemExit(f"Unsupported Facebook Reel video file type: {local_path}")
    return FacebookVideoItem(
        local_path=str(local_path),
        video_size=local_path.stat().st_size,
        slide_type=str(slide.get("type") or ""),
    )


def read_caption(args: argparse.Namespace, manifest: dict[str, Any]) -> str:
    if args.caption_file:
        return args.caption_file.read_text(encoding="utf-8").strip()
    if args.caption is not None:
        return args.caption.strip()
    facebook_caption = str(manifest.get("facebook_caption") or "").strip()
    if facebook_caption:
        return facebook_caption
    return ig.read_caption(args, manifest)


def start_reel_upload(
    *,
    page_id: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> dict[str, str]:
    response = ig.graph_request(
        f"{page_id}/video_reels",
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={"upload_phase": "start"},
        method="POST",
        api_name="Facebook",
    )
    video_id = str(response.get("video_id") or response.get("id") or "")
    upload_url = str(response.get("upload_url") or "").strip()
    if not video_id or not upload_url:
        raise SystemExit(f"Facebook did not return a Reel upload session: {response}")
    return {"video_id": video_id, "upload_url": upload_url}


def upload_reel_binary(
    *,
    upload_url: str,
    access_token: str,
    video_path: Path,
    timeout: int,
) -> dict[str, Any]:
    data = video_path.read_bytes()
    request = urllib.request.Request(
        upload_url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"OAuth {access_token}",
            "Content-Type": "application/octet-stream",
            "User-Agent": "carousel-app/1.0",
            "offset": "0",
            "file_size": str(len(data)),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            if raw.strip():
                return json.loads(raw)
            return {"status": response.status}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Facebook Reel upload failed: HTTP {exc.code} {body[:500]}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Facebook Reel upload failed: {exc}") from exc


def finish_reel_publish(
    *,
    page_id: str,
    video_id: str,
    caption: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> dict[str, Any]:
    params = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
    }
    if caption:
        params["description"] = caption
    return ig.graph_request(
        f"{page_id}/video_reels",
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params=params,
        method="POST",
        api_name="Facebook",
    )


def fetch_permalink(
    *,
    video_id: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> dict[str, Any]:
    return ig.graph_request(
        video_id,
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={"fields": "permalink_url,post_id"},
        method="GET",
        timeout=30,
        api_name="Facebook",
    )


def api_steps(caption: str) -> list[dict[str, Any]]:
    finish_params: dict[str, str] = {
        "upload_phase": "finish",
        "video_id": "<video_id>",
        "video_state": "PUBLISHED",
    }
    if caption:
        finish_params["description"] = caption
    return [
        {"action": "start_reel_upload", "params": {"upload_phase": "start"}},
        {"action": "upload_video_binary", "headers": {"offset": "0", "file_size": "<bytes>"}},
        {"action": "finish_reel_publish", "params": finish_params},
    ]


def publish_to_facebook(
    *,
    item: FacebookVideoItem,
    caption: str,
    page_id: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
    upload_timeout: int,
) -> dict[str, Any]:
    start = start_reel_upload(
        page_id=page_id,
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
    )
    upload = upload_reel_binary(
        upload_url=start["upload_url"],
        access_token=access_token,
        video_path=Path(item.local_path),
        timeout=upload_timeout,
    )
    finish = finish_reel_publish(
        page_id=page_id,
        video_id=start["video_id"],
        caption=caption,
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
    )
    permalink: dict[str, Any] = {}
    try:
        permalink = fetch_permalink(
            video_id=start["video_id"],
            access_token=access_token,
            graph_version=graph_version,
            graph_api_root=graph_api_root,
        )
    except SystemExit as exc:
        print(f"[facebook] published, but permalink lookup failed: {exc}")
    return {
        "started": start,
        "upload": upload,
        "finish": finish,
        "published": {"id": start["video_id"], **finish},
        "permalink": {"permalink": str(permalink.get("permalink_url") or ""), **permalink},
    }


def build_report(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    item: FacebookVideoItem,
    caption: str,
    dry_run: bool,
    graph_version: str,
    graph_api_root: str,
    facebook_page_id: str,
    facebook_page_id_source: str,
    access_token_source: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "created_at": ig.utc_now(),
        "platform": "facebook",
        "dry_run": dry_run,
        "manifest_path": str(manifest_path),
        "source_url": manifest.get("source_url"),
        "account_name": manifest.get("account_name"),
        "facebook_page_id": facebook_page_id,
        "facebook_page_id_source": facebook_page_id_source,
        "access_token_source": access_token_source,
        "graph_api_version": graph_version,
        "graph_api_root": graph_api_root,
        "caption": caption,
        "media": [asdict(item)],
        "api_steps": api_steps(caption),
        "result": result or {},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a one-video manifest as a Facebook Page Reel")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--caption", help="Facebook caption. Defaults to facebook_caption, then instagram_caption.")
    parser.add_argument("--caption-file", type=Path, help="Read the Facebook caption from a text file")
    parser.add_argument(
        "--facebook-page-id",
        default="",
        help="Facebook Page ID. Defaults to channel publishing.facebook_reels.page_id, then FACEBOOK_PAGE_ID.",
    )
    parser.add_argument(
        "--access-token",
        default="",
        help=(
            "Facebook Page access token. Defaults to channel-specific Facebook env vars, "
            "then the same Meta/Instagram token env vars used by instagram_publish.py."
        ),
    )
    parser.add_argument(
        "--graph-api-version",
        default=ig.graph_api_version(),
        help="Graph API version, for example v25.0.",
    )
    parser.add_argument(
        "--graph-api-root",
        default=os.environ.get("FACEBOOK_GRAPH_API_ROOT", ig.META_GRAPH_API_ROOT),
    )
    parser.add_argument("--upload-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true", help="Validate and write a publish plan only")
    parser.add_argument("--out", type=Path, help=f"Write report JSON here (default: {DEFAULT_REPORT_NAME})")
    parser.add_argument("--print-json", action="store_true", help="Print the report JSON to stdout")
    return parser


def main() -> int:
    load_env_file(ROOT / ".env")
    args = build_parser().parse_args()
    manifest_path = args.manifest.resolve()
    manifest = ig.load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise SystemExit("Manifest JSON must be an object")
    page_id, page_id_source = resolve_facebook_page_id(args.facebook_page_id, manifest, manifest_path)
    access_token, token_source = resolve_facebook_access_token(args.access_token, manifest, manifest_path)
    graph_version = ig.normalize_graph_version(args.graph_api_version)
    graph_root = args.graph_api_root.rstrip("/")
    item = video_item_from_manifest(manifest, manifest_path)
    caption = read_caption(args, manifest)

    result: dict[str, Any] | None = None
    publish_token_source = token_source
    if args.dry_run:
        print(f"[facebook] dry run: validated {Path(item.local_path).name}")
    else:
        if not page_id:
            raise SystemExit("FACEBOOK_PAGE_ID or --facebook-page-id is required to publish")
        if not access_token:
            raise SystemExit(
                "FACEBOOK_PAGE_ACCESS_TOKEN, META_SYSTEM_USER_ACCESS_TOKEN, or --access-token "
                "is required to publish"
            )
        publish_token, publish_token_source = resolve_page_access_token_for_publish(
            page_id=page_id,
            access_token=access_token,
            access_token_source=token_source,
            graph_version=graph_version,
            graph_api_root=graph_root,
        )
        result = publish_to_facebook(
            item=item,
            caption=caption,
            page_id=page_id,
            access_token=publish_token,
            graph_version=graph_version,
            graph_api_root=graph_root,
            upload_timeout=args.upload_timeout,
        )
    report = build_report(
        manifest_path=manifest_path,
        manifest=manifest,
        item=item,
        caption=caption,
        dry_run=args.dry_run,
        graph_version=graph_version,
        graph_api_root=graph_root,
        facebook_page_id=page_id,
        facebook_page_id_source=page_id_source,
        access_token_source=publish_token_source,
        result=result,
    )
    report_path = args.out or manifest_path.with_name(DEFAULT_REPORT_NAME)
    ig.write_json(report_path, report)
    print(f"[facebook] wrote report -> {report_path}")
    if args.print_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
