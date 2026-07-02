#!/usr/bin/env python3
"""Publish rendered research carousel queue items.

This is intentionally small and queue-aware: it picks the oldest/highest row
already marked ``rendered``, invokes instagram_publish.py, and writes the
publish result back to the research carousel queue.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from channel import load_channel

ROOT = Path(__file__).resolve().parent
DEFAULT_QUEUE = ROOT / "out" / "research_idea_generator" / "carousel_brief_queue.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, fallback: Any | None = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if fallback is not None:
            return fallback
        raise


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def string_value(value: object) -> str:
    return str(value or "").strip()


def manifest_channel_id(path: Path) -> str:
    manifest = read_json(path)
    return string_value(manifest.get("channel_id")) if isinstance(manifest, dict) else ""


def report_identity(report_path: Path) -> tuple[str, str, str]:
    report = read_json(report_path, {})
    result = report.get("result") if isinstance(report, dict) else {}
    result = result if isinstance(result, dict) else {}
    published = result.get("published") if isinstance(result.get("published"), dict) else {}
    permalink = result.get("permalink") if isinstance(result.get("permalink"), dict) else {}
    return (
        string_value(report.get("created_at") if isinstance(report, dict) else ""),
        string_value(published.get("id")),
        string_value(permalink.get("permalink")),
    )


def rendered_items(queue: dict[str, Any], *, channel_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in queue.get("items", []):
        if not isinstance(item, dict) or item.get("status") != "rendered":
            continue
        manifest_path = Path(string_value(item.get("renderedManifestPath")))
        if not manifest_path.exists():
            item["lastError"] = f"renderedManifestPath does not exist: {manifest_path}"
            continue
        if manifest_channel_id(manifest_path) != channel_id:
            continue
        candidates.append(item)
    return sorted(
        candidates,
        key=lambda item: (
            string_value(item.get("scheduledAt")),
            string_value(item.get("renderedAt")),
            -float(item.get("score") or 0),
            string_value(item.get("firstSeenAt")),
            string_value(item.get("briefId")),
        ),
    )


def build_publish_command(
    manifest_path: Path,
    report_path: Path,
    *,
    dry_run: bool,
    upload_r2: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "instagram_publish.py"),
        str(manifest_path),
        "--out",
        str(report_path),
    ]
    if dry_run:
        command.append("--dry-run")
    if upload_r2:
        command.append("--upload-r2")
    return command


def publish_next(args: argparse.Namespace) -> int:
    load_channel(args.channel)
    queue_path = args.queue.resolve()
    queue = read_json(queue_path, {"version": 1, "items": []})
    if not isinstance(queue, dict):
        raise SystemExit(f"Queue JSON must be an object: {queue_path}")

    candidates = rendered_items(queue, channel_id=args.channel)
    if not candidates:
        write_json(queue_path, queue)
        print(f"[research-carousel] no rendered {args.channel} items to publish")
        return 0

    item = candidates[0]
    manifest_path = Path(string_value(item.get("renderedManifestPath"))).resolve()
    report_name = "instagram_publish_dry_run.json" if args.dry_run else "instagram_publish.json"
    report_path = manifest_path.with_name(report_name)
    command = build_publish_command(
        manifest_path,
        report_path,
        dry_run=args.dry_run,
        upload_r2=not args.no_upload_r2,
    )

    item["lastPublishAttemptAt"] = utc_now()
    item["lastPublishDryRun"] = bool(args.dry_run)
    item.pop("lastError", None)
    write_json(queue_path, queue)

    verb = "previewing" if args.dry_run else "publishing"
    print(f"[research-carousel] {verb} {item.get('briefId')} -> {manifest_path}")
    result = subprocess.run(command, check=False)
    item["lastPublishFinishedAt"] = utc_now()
    item["lastPublishReturncode"] = result.returncode

    if result.returncode != 0:
        item["status"] = "failed"
        item["lastError"] = f"instagram_publish.py exited {result.returncode}"
        queue["updatedAt"] = utc_now()
        write_json(queue_path, queue)
        return result.returncode

    if args.dry_run:
        item["dryRunReportPath"] = str(report_path)
        item["status"] = "rendered"
    else:
        created_at, media_id, permalink = report_identity(report_path)
        item["status"] = "published"
        item["publishReportPath"] = str(report_path)
        item["publishedAt"] = created_at or utc_now()
        if media_id:
            item["mediaId"] = media_id
        if permalink:
            item["permalink"] = permalink
    queue["updatedAt"] = utc_now()
    write_json(queue_path, queue)
    print(f"[research-carousel] {item['status']} {item.get('briefId')}")
    if item.get("permalink"):
        print(f"[research-carousel] permalink {item['permalink']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish the next rendered research carousel from the queue")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--channel", default="aibrief_jp")
    parser.add_argument("--dry-run", action="store_true", help="Validate through instagram_publish.py without posting")
    parser.add_argument("--no-upload-r2", action="store_true", help="Skip R2 uploads and use existing public/media URLs")
    return parser


def main() -> int:
    return publish_next(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
