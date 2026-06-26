#!/usr/bin/env python3
"""Backfill Facebook Page posts from already-published Instagram reports."""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Any

import instagram_publish as ig
from channel import load_channel


def normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def report_media_items(report: dict[str, Any]) -> list[ig.MediaItem]:
    items: list[ig.MediaItem] = []
    for raw in report.get("media") or []:
        if not isinstance(raw, dict):
            continue
        items.append(
            ig.MediaItem(
                index=int(raw.get("index") or len(items) + 1),
                kind=str(raw.get("kind") or ""),
                local_path=str(raw.get("local_path") or ""),
                public_url=str(raw.get("public_url") or ""),
                slide_type=str(raw.get("slide_type") or ""),
                source_url=str(raw.get("source_url") or ""),
            )
        )
    return items


def report_channel_matches(
    *,
    report_path: pathlib.Path,
    report: dict[str, Any],
    manifest: dict[str, Any],
    channel_id: str,
) -> bool:
    account = str(manifest.get("account_name") or report.get("account_name") or "").lower()
    channel = str(manifest.get("channel_id") or report.get("channel_id") or "")
    return channel == channel_id or channel_id in str(report_path) or channel_id.replace("_", "") in account


def load_local_reports(channel_id: str, out_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
    reports: dict[str, dict[str, Any]] = {}
    for report_path in out_dir.rglob(ig.DEFAULT_REPORT_NAME):
        try:
            report = json.loads(report_path.read_text())
        except Exception:
            continue
        if report.get("dry_run"):
            continue
        media_id = str(((report.get("result") or {}).get("published") or {}).get("id") or "")
        if not media_id:
            continue
        manifest_path = pathlib.Path(str(report.get("manifest_path") or report_path.with_name("manifest.json")))
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            manifest = {}
        if not report_channel_matches(
            report_path=report_path,
            report=report,
            manifest=manifest,
            channel_id=channel_id,
        ):
            continue
        reports[media_id] = {
            "path": report_path,
            "manifest_path": manifest_path,
            "report": report,
            "manifest": manifest,
            "items": report_media_items(report),
        }
    return reports


def fetch_instagram_media(
    *,
    instagram_user_id: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> list[dict[str, Any]]:
    response = ig.graph_request(
        f"{instagram_user_id}/media",
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={
            "fields": "id,caption,media_type,media_product_type,permalink,timestamp",
            "limit": "100",
        },
        method="GET",
        timeout=30,
        api_name="Instagram",
    )
    media = response.get("data") if isinstance(response.get("data"), list) else []
    return sorted(
        [item for item in media if isinstance(item, dict)],
        key=lambda item: str(item.get("timestamp") or ""),
    )


def fetch_facebook_posts(
    *,
    facebook_page_id: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
    since: str,
) -> list[dict[str, Any]]:
    response = ig.graph_request(
        f"{facebook_page_id}/posts",
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={
            "fields": "id,message,created_time,permalink_url",
            "limit": "100",
            "since": since,
        },
        method="GET",
        timeout=30,
        api_name="Facebook",
    )
    posts = response.get("data") if isinstance(response.get("data"), list) else []
    return [post for post in posts if isinstance(post, dict)]


def matching_facebook_post(caption: str, posts: list[dict[str, Any]]) -> dict[str, Any] | None:
    caption_norm = normalized_text(caption)
    if not caption_norm:
        return None
    for post in posts:
        message_norm = normalized_text(post.get("message"))
        if caption_norm == message_norm:
            return post
        if message_norm and (caption_norm in message_norm or message_norm in caption_norm):
            if min(len(caption_norm), len(message_norm)) > 80:
                return post
    return None


def default_since() -> str:
    return "2026-06-17"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill Facebook Page posts from Instagram publish reports")
    parser.add_argument("--channel", default="aibrief_jp")
    parser.add_argument("--since", default=default_since(), help="Fetch Facebook posts since this date")
    parser.add_argument("--out-dir", type=pathlib.Path, default=ig.ROOT / "out")
    parser.add_argument("--summary", type=pathlib.Path, help="Write summary JSON here")
    parser.add_argument("--dry-run", action="store_true", help="Only plan; do not publish to Facebook")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    ig.load_env_file(ig.ROOT / ".env")
    channel = load_channel(args.channel)
    manifest_stub = {"channel_id": args.channel}
    manifest_path = ig.ROOT / "out" / "manifest.json"
    graph_version = ig.normalize_graph_version(ig.graph_api_version())
    facebook_root = ig.facebook_graph_api_root().rstrip("/")
    instagram_root = ig.graph_api_root().rstrip("/")

    instagram_user_id, instagram_user_id_source = ig.resolve_instagram_user_id("", manifest_stub, manifest_path)
    instagram_token, instagram_token_source = ig.resolve_instagram_access_token("", manifest_stub, manifest_path)
    facebook_page_id, facebook_page_id_source = ig.resolve_facebook_page_id("", manifest_stub, manifest_path)
    facebook_token, facebook_token_source = ig.resolve_facebook_access_token("", manifest_stub, manifest_path)
    (
        facebook_page_id,
        facebook_page_id_source,
        facebook_token,
        facebook_token_source,
    ) = ig.derive_facebook_page_credentials(
        facebook_page_id=facebook_page_id,
        facebook_page_id_source=facebook_page_id_source,
        access_token=facebook_token,
        access_token_source=facebook_token_source,
        graph_version=graph_version,
        graph_api_root=facebook_root,
        instagram_user_id=instagram_user_id,
        instagram_username=channel.handle,
    )
    if not facebook_page_id or not facebook_token:
        raise SystemExit(f"Could not resolve Facebook Page ID/token for {args.channel}")

    local_reports = load_local_reports(args.channel, args.out_dir)
    live_media = fetch_instagram_media(
        instagram_user_id=instagram_user_id,
        access_token=instagram_token,
        graph_version=graph_version,
        graph_api_root=instagram_root,
    )
    facebook_posts = fetch_facebook_posts(
        facebook_page_id=facebook_page_id,
        access_token=facebook_token,
        graph_version=graph_version,
        graph_api_root=facebook_root,
        since=args.since,
    )

    summary = {
        "created_at": ig.utc_now(),
        "dry_run": args.dry_run,
        "channel_id": args.channel,
        "instagram_user_id": instagram_user_id,
        "instagram_user_id_source": instagram_user_id_source,
        "instagram_access_token_source": instagram_token_source,
        "facebook_page_id": facebook_page_id,
        "facebook_page_id_source": facebook_page_id_source,
        "facebook_access_token_source": facebook_token_source,
        "live_instagram_count": len(live_media),
        "existing_facebook_count": len(facebook_posts),
        "published": [],
        "would_publish": [],
        "already_posted": [],
        "unsupported": [],
        "missing_local_report": [],
        "errors": [],
    }

    for media in live_media:
        media_id = str(media.get("id") or "")
        permalink = str(media.get("permalink") or "")
        timestamp = str(media.get("timestamp") or "")
        caption = str(media.get("caption") or "")
        local = local_reports.get(media_id)
        fb_match = matching_facebook_post(caption, facebook_posts)
        if fb_match:
            summary["already_posted"].append(
                {
                    "instagram_media_id": media_id,
                    "instagram_permalink": permalink,
                    "facebook_post_id": fb_match.get("id"),
                    "facebook_permalink": fb_match.get("permalink_url"),
                }
            )
            continue
        if not local:
            summary["missing_local_report"].append(
                {
                    "instagram_media_id": media_id,
                    "instagram_permalink": permalink,
                    "timestamp": timestamp,
                    "reason": "No local instagram_publish.json with public media URLs",
                }
            )
            continue
        items = local["items"]
        supported, reason = ig.facebook_publish_support(items)
        if not supported:
            summary["unsupported"].append(
                {
                    "instagram_media_id": media_id,
                    "instagram_permalink": permalink,
                    "report_path": str(local["path"]),
                    "reason": reason,
                }
            )
            continue
        planned = {
            "instagram_media_id": media_id,
            "instagram_permalink": permalink,
            "report_path": str(local["path"]),
            "media_count": len(items),
            "media_kinds": sorted({item.kind for item in items}),
        }
        if args.dry_run:
            print(f"[backfill] would post {media_id} -> Facebook")
            summary["would_publish"].append(planned)
            continue
        print(f"[backfill] posting {media_id} -> Facebook")
        try:
            result = ig.publish_to_facebook_page(
                items,
                message=str(local["report"].get("caption") or caption),
                facebook_page_id=facebook_page_id,
                access_token=facebook_token,
                graph_version=graph_version,
                graph_api_root=facebook_root,
            )
        except SystemExit as exc:
            error = str(exc)
            print(f"[backfill] failed {media_id}: {error}")
            summary["errors"].append({**planned, "error": error})
            continue
        facebook_section = ig.build_facebook_report_section(
            enabled=True,
            items=items,
            message=str(local["report"].get("caption") or caption),
            graph_version=graph_version,
            graph_api_root=facebook_root,
            facebook_page_id=facebook_page_id,
            facebook_page_id_source=facebook_page_id_source,
            access_token_source=facebook_token_source,
            result=result,
        )
        local["report"]["facebook"] = facebook_section
        ig.write_json(local["path"], local["report"])
        published = result.get("published") if isinstance(result.get("published"), dict) else {}
        fb_permalink = result.get("permalink") if isinstance(result.get("permalink"), dict) else {}
        summary["published"].append(
            {
                **planned,
                "facebook_post_id": published.get("id"),
                "facebook_permalink": fb_permalink.get("permalink_url") or fb_permalink.get("permalink"),
            }
        )

    summary_path = args.summary or (
        ig.ROOT
        / "out"
        / f"facebook_backfill_{args.channel}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    ig.write_json(summary_path, summary)
    print(f"[backfill] wrote summary -> {summary_path}")
    print(
        json.dumps(
            {
                key: len(summary[key])
                for key in (
                    "published",
                    "would_publish",
                    "already_posted",
                    "unsupported",
                    "missing_local_report",
                    "errors",
                )
            },
            indent=2,
        )
    )
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
