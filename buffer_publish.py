#!/usr/bin/env python3
"""Create a Buffer draft/queued post from a generated carousel manifest."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fetch_tweet_data import load_env_file
from instagram_publish import (
    DEFAULT_MANIFEST,
    VIDEO_SUFFIXES,
    absolute_slide_path,
    build_media_items,
    parse_media_url_overrides,
    r2_config,
    upload_media_to_r2,
)

ROOT = Path(__file__).resolve().parent
BUFFER_API_URL = "https://api.buffer.com"


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise SystemExit(f"No manifest found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def caption_from_manifest(args: argparse.Namespace, manifest: dict[str, Any]) -> str:
    if args.caption_file:
        return args.caption_file.read_text().strip()
    if args.caption is not None:
        return args.caption.strip()
    context = manifest.get("title_context") if isinstance(manifest.get("title_context"), dict) else {}
    topic = str(context.get("topic") or "").strip()
    source_url = str(manifest.get("source_url") or "").strip()
    parts = [topic] if topic else []
    if source_url:
        parts.extend(["", f"Source: {source_url}"])
    return "\n".join(parts).strip()


def buffer_asset(item: Any) -> dict[str, Any]:
    if item.kind == "video":
        return {"video": {"url": item.public_url}}
    return {"image": {"url": item.public_url}}


def instagram_post_type(items: list[Any]) -> str:
    # Buffer only accepts post, story, or reel for Instagram; multiple image
    # assets on a "post" become the carousel.
    if any(item.kind == "video" for item in items):
        return "reel"
    return "post"


def slide_is_video(slide: dict[str, Any], manifest_path: Path) -> bool:
    if "video" in str(slide.get("type") or "").lower():
        return True
    path = absolute_slide_path(slide.get("path"), manifest_path)
    return path.suffix.lower() in VIDEO_SUFFIXES


def apply_video_strategy(
    manifest: dict[str, Any],
    manifest_path: Path,
    strategy: str,
) -> list[dict[str, Any]]:
    """Buffer collapses Instagram posts that mix video and images into a single
    video (published as a reel), silently dropping the other slides. Rewrite the
    slide list so the payload is something Buffer can actually publish."""
    slides = [s for s in manifest.get("slides") or [] if isinstance(s, dict)]
    if len(slides) <= 1:
        return []
    video_slides = [s for s in slides if slide_is_video(s, manifest_path)]
    if not video_slides:
        return []
    if strategy == "fail":
        raise SystemExit(
            "Buffer does not support Instagram carousels that mix video and images "
            "(their API silently keeps only the video plus the last image). "
            "Options: re-run with --video-strategy poster (swap videos for poster stills, "
            "image-only carousel), --video-strategy reel (publish the first video alone "
            "as a reel), or publish via instagram_publish.py (Meta Graph API), which "
            "supports true mixed-media carousels."
        )
    notes: list[dict[str, Any]] = []
    if strategy == "reel":
        keep = video_slides[0]
        dropped = [s.get("index") for s in slides if s is not keep]
        manifest["slides"] = [keep]
        print(f"[buffer] reel strategy: keeping video slide {keep.get('index')}, dropping slides {dropped}")
        notes.append({"strategy": "reel", "kept_index": keep.get("index"), "dropped_indexes": dropped})
        return notes
    for slide in video_slides:
        poster = slide.get("poster")
        if not isinstance(poster, str) or not poster.strip():
            raise SystemExit(
                f"Slide {slide.get('index')} is a video with no poster image, and Buffer "
                "carousels are image-only. Re-render the build or use --video-strategy reel."
            )
        print(f"[buffer] poster strategy: using poster image for video slide {slide.get('index')}")
        notes.append(
            {
                "strategy": "poster",
                "index": slide.get("index"),
                "video_path": slide.get("path"),
                "poster_path": poster,
            }
        )
        slide["path"] = poster
        slide["type"] = "post"
    return notes


def created_post_or_die(response: dict[str, Any], expected_assets: int) -> dict[str, Any]:
    create = (response.get("data") or {}).get("createPost") or {}
    post = create.get("post")
    if not isinstance(post, dict):
        message = create.get("message") or json.dumps(response, ensure_ascii=False)[:400]
        raise SystemExit(f"Buffer createPost failed: {message}")
    stored = post.get("assets") or []
    if len(stored) != expected_assets:
        raise SystemExit(
            f"Buffer kept {len(stored)} of {expected_assets} assets on post {post.get('id')}; "
            "it silently drops media it cannot publish. Check the draft in Buffer."
        )
    return post


def buffer_input(
    *,
    channel_id: str,
    caption: str,
    items: list[Any],
    mode: str,
) -> dict[str, Any]:
    save_to_draft = mode == "draft"
    share_mode = "addToQueue" if save_to_draft or mode == "queue" else "shareNow"
    return {
        "text": caption,
        "channelId": channel_id,
        "schedulingType": "automatic",
        "mode": share_mode,
        "saveToDraft": save_to_draft,
        "metadata": {
            "instagram": {
                "type": instagram_post_type(items),
                "shouldShareToFeed": True,
            }
        },
        "assets": [buffer_asset(item) for item in items],
    }


def buffer_request(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        BUFFER_API_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "carousel-app/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Buffer API error {exc.code}: {body[:600]}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Buffer API request failed: {exc}") from exc


def create_buffer_post(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    query = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post {
        id
        text
        assets {
          id
          mimeType
          source
        }
      }
    }
    ... on MutationError {
      message
    }
  }
}
""".strip()
    return buffer_request(api_key, query, {"input": payload})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a Buffer draft/queued Instagram carousel post")
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--buffer-api-key", default=env_value("BUFFER_API_KEY"))
    parser.add_argument("--buffer-channel-id", default=env_value("BUFFER_CHANNEL_ID"))
    parser.add_argument(
        "--mode",
        choices=("draft", "queue", "now"),
        default="draft",
        help="draft creates a Buffer draft; queue adds to Buffer queue; now publishes immediately",
    )
    parser.add_argument(
        "--video-strategy",
        choices=("fail", "poster", "reel"),
        default="fail",
        help=(
            "How to handle video slides in multi-slide carousels, which Buffer cannot mix "
            "with images: fail aborts (default; use instagram_publish.py for true mixed "
            "carousels), poster swaps each video for its poster still, reel publishes the "
            "first video alone as a reel"
        ),
    )
    parser.add_argument("--media-base-url", default=env_value("INSTAGRAM_MEDIA_BASE_URL", "IG_MEDIA_BASE_URL"))
    parser.add_argument(
        "--media-url",
        action="append",
        default=[],
        help="Override one slide URL, formatted as INDEX_OR_FILENAME=https://...",
    )
    parser.add_argument("--upload-r2", action="store_true", help="Upload rendered slides to R2 first")
    parser.add_argument("--r2-bucket", default=env_value("R2_BUCKET"))
    parser.add_argument("--r2-key-prefix")
    parser.add_argument("--r2-public-base-url", default=env_value("R2_PUBLIC_BASE_URL"))
    parser.add_argument("--r2-timeout", type=int, default=120)
    parser.add_argument("--caption")
    parser.add_argument("--caption-file", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate and write the Buffer payload only")
    parser.add_argument("--out", type=Path, help="Write report JSON here")
    parser.add_argument("--print-json", action="store_true")
    return parser


def main() -> int:
    load_env_file(ROOT / ".env")
    args = build_parser().parse_args()
    manifest_path = args.manifest.resolve()
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise SystemExit("Manifest JSON must be an object")
    video_strategy_notes = apply_video_strategy(manifest, manifest_path, args.video_strategy)

    media_base_url = args.media_base_url.strip()
    if args.upload_r2 and not media_base_url:
        media_base_url = args.r2_public_base_url.strip()
    items = build_media_items(
        manifest,
        manifest_path,
        media_base_url=media_base_url,
        overrides=parse_media_url_overrides(args.media_url),
        dry_run=args.dry_run,
    )
    uploads: list[dict[str, Any]] = []
    if args.upload_r2:
        uploads = upload_media_to_r2(items, r2_config(args, manifest_path, media_base_url), timeout=args.r2_timeout)

    caption = caption_from_manifest(args, manifest)
    if not args.buffer_channel_id:
        raise SystemExit("BUFFER_CHANNEL_ID or --buffer-channel-id is required")
    payload = buffer_input(channel_id=args.buffer_channel_id, caption=caption, items=items, mode=args.mode)

    response: dict[str, Any] | None = None
    if args.dry_run:
        print(f"[buffer] dry run: prepared {len(items)} asset(s) for Buffer")
    else:
        if not args.buffer_api_key:
            raise SystemExit("BUFFER_API_KEY or --buffer-api-key is required")
        print(f"[buffer] creating {args.mode} post with {len(items)} asset(s)")
        response = create_buffer_post(args.buffer_api_key, payload)

    report = {
        "manifest_path": str(manifest_path),
        "mode": args.mode,
        "buffer_channel_id": args.buffer_channel_id,
        "video_strategy": args.video_strategy,
        "video_strategy_notes": video_strategy_notes,
        "media": [
            {
                "index": item.index,
                "kind": item.kind,
                "local_path": item.local_path,
                "public_url": item.public_url,
                "slide_type": item.slide_type,
            }
            for item in items
        ],
        "uploads": uploads,
        "buffer_payload": payload,
        "buffer_response": response or {},
    }
    report_path = args.out or manifest_path.with_name("buffer_publish.json")
    write_json(report_path, report)
    print(f"[buffer] wrote report -> {report_path}")
    if args.print_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    if response is not None:
        post = created_post_or_die(response, len(items))
        print(f"[buffer] created {args.mode} post {post.get('id')} with {len(items)} asset(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
