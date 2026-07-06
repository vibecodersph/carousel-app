#!/usr/bin/env python3
"""Render queued research carousel briefs into publishable manifests."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from channel import load_channel

ROOT = Path(__file__).resolve().parent
DEFAULT_QUEUE = ROOT / "out" / "research_idea_generator" / "carousel_brief_queue.json"
DEFAULT_RENDER_ROOT = ROOT / "out" / "research_idea_generator" / "rendered_queue"
RENDERABLE_STATUSES = {"new", "scheduled", "failed"}
AUTO_COVER_TEMPLATES = {"", "auto", "best", "dynamic", "match"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def string_value(value: object) -> str:
    return str(value or "").strip()


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


def parse_instant(value: object) -> datetime | None:
    text = string_value(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_due(item: dict[str, Any], *, now: datetime, include_future: bool) -> bool:
    if include_future:
        return True
    scheduled_at = string_value(item.get("scheduledAt"))
    if not scheduled_at:
        return True
    parsed = parse_instant(scheduled_at)
    if parsed is None:
        item["lastError"] = f"Invalid scheduledAt value: {scheduled_at}"
        return False
    return parsed <= now


def slug(value: object, fallback: str) -> str:
    text = string_value(value) or fallback
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return text[:80] or fallback


def manifest_is_usable(path_value: object, *, channel_id: str) -> bool:
    path_text = string_value(path_value)
    if not path_text:
        return False
    path = Path(path_text)
    if not path.exists():
        return False
    manifest = read_json(path, {})
    return isinstance(manifest, dict) and string_value(manifest.get("channel_id")) == channel_id


def render_candidates(
    queue: dict[str, Any],
    *,
    channel_id: str,
    now: datetime,
    include_future: bool,
    force: bool = False,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in queue.get("items", []):
        if not isinstance(item, dict):
            continue
        if string_value(item.get("channelId")) not in {"", channel_id}:
            continue
        if string_value(item.get("status")) not in RENDERABLE_STATUSES:
            continue
        if not force and manifest_is_usable(item.get("renderedManifestPath"), channel_id=channel_id):
            continue
        brief_path = Path(string_value(item.get("briefPath")))
        if not brief_path.exists():
            item["lastError"] = f"briefPath does not exist: {brief_path}"
            continue
        if not is_due(item, now=now, include_future=include_future):
            continue
        candidates.append(item)
    return sorted(
        candidates,
        key=lambda item: (
            string_value(item.get("scheduledAt")),
            -float(item.get("score") or 0),
            string_value(item.get("firstSeenAt")),
            string_value(item.get("briefId")),
        ),
    )


def build_render_command(
    item: dict[str, Any],
    out_dir: Path,
    *,
    channel_id: str,
    cover_style: str,
    cover_template: str,
    generate_images: bool,
    no_carousel_music: bool,
    localize_copy: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "build_idea_carousel.py"),
        "--input",
        string_value(item.get("briefPath")),
        "--index",
        str(int(item.get("briefIndex") or 0)),
        "--channel",
        channel_id,
        "--cover-style",
        cover_style,
        "--cover-template",
        cover_template,
        "--out-dir",
        str(out_dir),
    ]
    if not generate_images:
        command.append("--no-generate-images")
    if localize_copy:
        command.append("--localize-copy")
    if no_carousel_music:
        command.append("--no-carousel-music")
    return command


def should_generate_images(args: argparse.Namespace) -> bool:
    if getattr(args, "no_generate_images", False):
        return False
    if getattr(args, "generate_images", False):
        return True
    return bool(getattr(args, "generate_images_by_default", True))


def cover_template_for_item(item: dict[str, Any], requested_template: str) -> str:
    requested = string_value(requested_template)
    if requested.lower().replace("-", "_") not in AUTO_COVER_TEMPLATES:
        return requested
    for key in ("coverTemplate", "cover_template", "studyTemplate", "study_template"):
        value = string_value(item.get(key))
        if value:
            return value
    return requested or "auto"


def render_queue(args: argparse.Namespace) -> int:
    load_channel(args.channel)
    now = parse_instant(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise SystemExit(f"Invalid --now value: {args.now}")
    queue_path = args.queue.resolve()
    queue = read_json(queue_path, {"version": 1, "items": []})
    if not isinstance(queue, dict):
        raise SystemExit(f"Queue JSON must be an object: {queue_path}")

    candidates = render_candidates(
        queue,
        channel_id=args.channel,
        now=now,
        include_future=args.include_future,
        force=args.force,
    )
    if args.limit > 0:
        candidates = candidates[:args.limit]
    if not candidates:
        write_json(queue_path, queue)
        print(f"[research-carousel-render] no {args.channel} queue items need rendering")
        return 0

    rendered = 0
    for item in candidates:
        item_id = slug(item.get("id"), f"brief-{rendered + 1}")
        out_dir = args.out_root.resolve() / item_id
        manifest_path = out_dir / "manifest.json"
        command = build_render_command(
            item,
            out_dir,
            channel_id=args.channel,
            cover_style=args.cover_style,
            cover_template=cover_template_for_item(item, args.cover_template),
            generate_images=should_generate_images(args),
            no_carousel_music=args.no_carousel_music,
            localize_copy=args.localize_copy,
        )
        print(f"[research-carousel-render] rendering {item.get('briefId')} -> {manifest_path}")
        item["lastRenderAttemptAt"] = utc_now()
        item.pop("lastError", None)
        write_json(queue_path, queue)
        result = subprocess.run(command, check=False)
        item["lastRenderFinishedAt"] = utc_now()
        item["lastRenderReturncode"] = result.returncode
        if result.returncode != 0 or not manifest_path.exists():
            item["status"] = "failed"
            item["lastError"] = (
                f"build_idea_carousel.py exited {result.returncode}"
                if result.returncode != 0
                else f"manifest was not written: {manifest_path}"
            )
            queue["updatedAt"] = utc_now()
            write_json(queue_path, queue)
            return result.returncode or 1
        item["renderedManifestPath"] = str(manifest_path)
        item["renderedAt"] = utc_now()
        item["status"] = "scheduled" if string_value(item.get("scheduledAt")) else "rendered"
        queue["updatedAt"] = utc_now()
        write_json(queue_path, queue)
        rendered += 1
    print(f"[research-carousel-render] rendered {rendered} item(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render queued research carousel briefs before publishing")
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--channel", default="aibrief_jp")
    parser.add_argument("--out-root", type=Path, default=DEFAULT_RENDER_ROOT)
    parser.add_argument("--limit", type=int, default=1, help="Maximum queue rows to render; 0 means all")
    parser.add_argument("--include-future", action="store_true", help="Render scheduled future rows too")
    parser.add_argument("--force", action="store_true", help="Rerender rows even when a usable manifest already exists")
    parser.add_argument("--cover-style", default="aibrief-study")
    parser.add_argument("--cover-template", default="auto")
    parser.add_argument(
        "--localize-copy",
        action="store_true",
        help="Localize reader-facing brief copy to the selected channel language before rendering",
    )
    parser.set_defaults(generate_images_by_default=True)
    parser.add_argument(
        "--generate-images",
        action="store_true",
        help="Allow generated images. This is the default; kept for explicit automation logs.",
    )
    parser.add_argument(
        "--no-generate-images",
        action="store_true",
        help="Disable generated body/cover images and use source/local fallbacks only.",
    )
    parser.add_argument("--no-carousel-music", action="store_true", help="Render cover without muxing carousel music")
    parser.add_argument("--now", help="Override the current time for due-schedule checks, as an ISO timestamp")
    return parser


def main() -> int:
    return render_queue(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
