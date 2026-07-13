#!/usr/bin/env python3
"""Publish a manifest to the Vibe Coders PH Facebook Page.

Companion to instagram_publish.py, sharing the same manifest contract
({"slides": [{index, type, path}], "facebook_caption"/"instagram_caption"}).
Unlike Instagram, the Facebook Pages API accepts direct file uploads, so no
R2/public URL is needed.

- Image slides -> uploaded unpublished via /{page}/photos, then one feed post
  with attached_media (a Facebook multi-photo post).
- A single video slide -> /{page}/videos with a description.

Credentials (from .env):
  META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH  (system user token)
  FACEBOOK_PAGE_ID                            (optional; auto-discovered from
                                               /me/accounts when absent)

Dry run (default is real publish being BLOCKED unless --publish is passed):
    uv run python facebook_publish.py <manifest.json> --dry-run
Real publish:
    uv run python facebook_publish.py <manifest.json> --publish
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fetch_tweet_data import load_env_file

GRAPH_ROOT = "https://graph.facebook.com"
GRAPH_VERSION = os.environ.get("FACEBOOK_GRAPH_API_VERSION", "v23.0")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = {".mp4", ".mov"}
MAX_PHOTOS = 10


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def graph_url(path: str) -> str:
    return f"{GRAPH_ROOT}/{GRAPH_VERSION}/{path}"


def graph_get(path: str, token: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    q = dict(params or {})
    q["access_token"] = token
    url = graph_url(path) + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "carousel-app/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Graph GET {path} failed ({exc.code}): {detail}") from exc


def graph_post(path: str, token: str, params: dict[str, str],
               file_field: str | None = None, file_path: Path | None = None,
               timeout: int = 600) -> dict[str, Any]:
    params = dict(params)
    params["access_token"] = token
    if file_field and file_path:
        boundary = "----carouselapp" + secrets.token_hex(12)
        body = bytearray()
        for key, value in params.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{key}\"\r\n\r\n{value}\r\n").encode()
        ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"{file_field}\"; filename=\"{file_path.name}\"\r\n"
                 f"Content-Type: {ctype}\r\n\r\n").encode()
        body += file_path.read_bytes()
        body += f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            graph_url(path), data=bytes(body), method="POST",
            headers={"User-Agent": "carousel-app/1.0",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            graph_url(path), data=data, method="POST",
            headers={"User-Agent": "carousel-app/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Graph POST {path} failed ({exc.code}): {detail}") from exc


def resolve_page(token: str, page_id_hint: str) -> tuple[str, str, str]:
    """Return (page_id, page_name, page_access_token)."""
    data = graph_get("me/accounts", token, {"fields": "id,name,access_token"})
    pages = data.get("data") or []
    if not pages:
        raise SystemExit("Token has no managed pages (me/accounts empty)")
    if page_id_hint:
        for p in pages:
            if str(p.get("id")) == page_id_hint:
                return p["id"], p.get("name", "?"), p["access_token"]
        raise SystemExit(f"Page {page_id_hint} not found among managed pages")
    if len(pages) > 1:
        names = ", ".join(f"{p['name']}={p['id']}" for p in pages)
        raise SystemExit(f"Multiple pages managed; set FACEBOOK_PAGE_ID. Options: {names}")
    p = pages[0]
    return p["id"], p.get("name", "?"), p["access_token"]


def read_caption(args: argparse.Namespace, manifest: dict[str, Any]) -> str:
    if args.caption_file:
        return Path(args.caption_file).read_text().strip()
    if args.caption is not None:
        return args.caption.strip()
    for key in ("facebook_caption", "instagram_caption"):
        val = str(manifest.get(key) or "").strip()
        if val:
            return val
    return ""


def collect_items(manifest: dict[str, Any], manifest_path: Path) -> list[tuple[str, Path]]:
    slides = manifest.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SystemExit("Manifest has no slides")
    items: list[tuple[str, Path]] = []
    for slide in slides:
        raw = str(slide.get("path") or "")
        p = Path(raw)
        if not p.is_absolute():
            p = (manifest_path.parent / p).resolve()
        if not p.exists():
            raise SystemExit(f"Slide file missing: {p}")
        suffix = p.suffix.lower()
        if suffix in VIDEO_SUFFIXES or "video" in str(slide.get("type") or "").lower():
            items.append(("video", p))
        elif suffix in IMAGE_SUFFIXES:
            items.append(("image", p))
        else:
            raise SystemExit(f"Unsupported media type: {p}")
    return items


def rupload_video(video_id: str, page_token: str, path: Path) -> None:
    """Binary upload to rupload.facebook.com for the Reels flow."""
    data = path.read_bytes()
    req = urllib.request.Request(
        f"https://rupload.facebook.com/video-upload/{GRAPH_VERSION}/{video_id}",
        data=data, method="POST",
        headers={"Authorization": f"OAuth {page_token}",
                 "offset": "0", "file_size": str(len(data)),
                 "Content-Type": "application/octet-stream",
                 "User-Agent": "carousel-app/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"rupload failed ({exc.code}): {exc.read().decode('utf-8','replace')}") from exc
    if not body.get("success"):
        raise SystemExit(f"rupload did not report success: {body}")


def publish_reel(page_id: str, page_token: str, video_path: Path, caption: str) -> str:
    """Three-phase Facebook Reels publish: start -> binary upload -> finish."""
    start = graph_post(f"{page_id}/video_reels", page_token, {"upload_phase": "start"})
    video_id = str(start["video_id"])
    print(f"[facebook] reel container {video_id}; uploading {video_path.name} ...")
    rupload_video(video_id, page_token, video_path)
    graph_post(f"{page_id}/video_reels", page_token,
               {"video_id": video_id, "upload_phase": "finish",
                "video_state": "PUBLISHED", "description": caption})
    # poll processing status briefly
    import time as _time
    for _ in range(30):
        st = graph_get(video_id, page_token, {"fields": "status"})
        phase = (st.get("status") or {}).get("video_status", "?")
        if phase in ("ready", "upload_complete", "processing"):
            print(f"[facebook] reel status: {phase}")
            if phase == "ready":
                break
        _time.sleep(5)
    return video_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--caption", default=None)
    parser.add_argument("--caption-file", default=None)
    parser.add_argument("--reel", action="store_true",
                        help="publish a single video as a Facebook Reel (/video_reels) instead of a feed video")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--publish", action="store_true",
                        help="actually post (otherwise the script refuses unless --dry-run)")
    args = parser.parse_args()

    load_env_file(Path(__file__).resolve().parent / ".env")
    token = os.environ.get("META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH", "").strip()
    if not token:
        raise SystemExit("META_SYSTEM_USER_ACCESS_TOKEN_VIBECODERSPH is not set")

    manifest = json.loads(args.manifest.read_text())
    caption = read_caption(args, manifest)
    items = collect_items(manifest, args.manifest.resolve())
    kinds = {k for k, _ in items}

    if kinds == {"image"} and len(items) > MAX_PHOTOS:
        raise SystemExit(f"Facebook multi-photo posts max out at {MAX_PHOTOS} images")
    if "video" in kinds and len(items) != 1:
        raise SystemExit("Video manifests must contain exactly one video slide")

    if args.dry_run:
        print(f"[facebook] dry run: {len(items)} item(s), kind={'/'.join(sorted(kinds))}")
        print(f"[facebook] caption ({len(caption)} chars): {caption[:120]}...")
        for k, p in items:
            print(f"  - {k}: {p}")
        return
    if not args.publish:
        raise SystemExit("Refusing to post without --publish (or use --dry-run to validate)")

    page_id, page_name, page_token = resolve_page(
        token, os.environ.get("FACEBOOK_PAGE_ID", "").strip())
    print(f"[facebook] publishing to page: {page_name} ({page_id})")

    report: dict[str, Any] = {"page_id": page_id, "page_name": page_name,
                              "published_at": utc_now(), "items": []}
    if kinds == {"video"}:
        _, video_path = items[0]
        if args.reel:
            video_id = publish_reel(page_id, page_token, video_path, caption)
            print(f"[facebook] REEL id: {video_id}")
            report["items"].append({"kind": "reel", "id": video_id})
        else:
            print(f"[facebook] uploading video {video_path.name} ...")
            result = graph_post(f"{page_id}/videos", page_token,
                                {"description": caption}, "source", video_path)
            print(f"[facebook] video post id: {result.get('id')}")
            report["items"].append({"kind": "video", "id": result.get("id")})
    else:
        media_ids = []
        for idx, (_, photo_path) in enumerate(items, 1):
            print(f"[facebook] uploading photo {idx}/{len(items)} {photo_path.name} ...")
            result = graph_post(f"{page_id}/photos", page_token,
                                {"published": "false"}, "source", photo_path)
            media_ids.append(str(result["id"]))
        params: dict[str, str] = {"message": caption}
        for i, mid in enumerate(media_ids):
            params[f"attached_media[{i}]"] = json.dumps({"media_fbid": mid})
        result = graph_post(f"{page_id}/feed", page_token, params)
        print(f"[facebook] feed post id: {result.get('id')}")
        report["items"] = [{"kind": "image", "id": m} for m in media_ids]
        report["post_id"] = result.get("id")

    out = args.manifest.parent / "facebook_publish.json"
    out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"[facebook] wrote report -> {out}")


if __name__ == "__main__":
    main()
