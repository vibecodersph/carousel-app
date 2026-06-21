#!/usr/bin/env python3
"""Publish a generated carousel manifest to Instagram.

Instagram's publishing API accepts public HTTPS media URLs, not local files.
This script reads the carousel manifest written by build_x_carousel.py, maps
each rendered slide to a public URL, creates Instagram media containers, and
publishes either a single media post or a carousel post.

Dry run:
    uv run python instagram_publish.py out/x_carousel/manifest.json \
      --dry-run --media-base-url https://cdn.example.com/x_carousel

Real publish:
    export INSTAGRAM_USER_ID=178414...
    export INSTAGRAM_ACCESS_TOKEN=...
    export INSTAGRAM_GRAPH_DOMAIN=instagram
    uv run python instagram_publish.py out/x_carousel/manifest.json \
      --media-base-url https://cdn.example.com/x_carousel
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from channel import available_channels, load_channel
from fetch_tweet_data import load_env_file

ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "out" / "x_carousel" / "manifest.json"
DEFAULT_REPORT_NAME = "instagram_publish.json"
DEFAULT_GRAPH_API_VERSION = "v23.0"
DEFAULT_PUBLISH_RETRIES = 2
DEFAULT_PUBLISH_RETRY_DELAY_SECONDS = 60
FACEBOOK_GRAPH_API_ROOT = "https://graph.facebook.com"
INSTAGRAM_GRAPH_API_ROOT = "https://graph.instagram.com"
PLACEHOLDER_MEDIA_BASE_URL = "https://example.com/instagram-media"
MAX_CAROUSEL_ITEMS = 20
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
VIDEO_SUFFIXES = {".mp4", ".mov"}
FINISHED_STATUS_CODES = {"FINISHED", "PUBLISHED"}
WAIT_STATUS_CODES = {"EXPIRED", "ERROR"}


@dataclass
class MediaItem:
    index: int
    kind: str
    local_path: str
    public_url: str
    slide_type: str
    source_url: str


@dataclass
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str
    key_prefix: str


class InstagramContainerWaitError(RuntimeError):
    """Raised when an Instagram media container fails before publish."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def env_int(default: int, *names: str) -> int:
    value = env_value(*names)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_key_suffix(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.upper()).strip("_")


def manifest_channel_id(manifest: dict[str, Any], manifest_path: Path) -> str:
    direct = str(manifest.get("channel_id") or "").strip()
    if direct:
        return direct
    context = manifest.get("title_context") if isinstance(manifest.get("title_context"), dict) else {}
    voice_doc = str(context.get("brand_voice_doc") or "").strip()
    parts = Path(voice_doc).parts
    if "channels" in parts:
        index = parts.index("channels")
        if index + 1 < len(parts):
            return parts[index + 1]
    folder = manifest_path.parent.name
    for channel_id in available_channels():
        if folder == channel_id or folder.startswith(f"{channel_id}_"):
            return channel_id
    account_name = str(manifest.get("account_name") or "").strip()
    for channel_id in available_channels():
        try:
            channel = load_channel(channel_id)
        except Exception:
            continue
        names = {channel.account_name, channel.brand_name, channel.handle.lstrip("@"), channel.handle}
        if account_name in names:
            return channel_id
    return ""


def channel_env_value(channel_id: str, *bases: str) -> str:
    suffix = env_key_suffix(channel_id)
    if not suffix:
        return ""
    names: list[str] = []
    for base in bases:
        names.extend((f"{base}_{suffix}", f"{suffix}_{base}"))
    return env_value(*names)


def channel_instagram_user_id(channel_id: str) -> str:
    value = channel_env_value(channel_id, "INSTAGRAM_USER_ID", "IG_USER_ID")
    if value:
        return value
    if channel_id:
        try:
            channel = load_channel(channel_id)
        except Exception:
            return ""
        publishing = channel.publishing if isinstance(channel.publishing, dict) else {}
        return str(publishing.get("instagram_user_id") or publishing.get("ig_user_id") or "").strip()
    return ""


def channel_instagram_access_token(channel_id: str) -> str:
    return channel_env_value(
        channel_id,
        "META_SYSTEM_USER_ACCESS_TOKEN",
        "INSTAGRAM_ACCESS_TOKEN",
        "IG_ACCESS_TOKEN",
    )


def resolve_instagram_user_id(explicit: str, manifest: dict[str, Any], manifest_path: Path) -> tuple[str, str]:
    if explicit.strip():
        return explicit.strip(), "cli"
    channel_id = manifest_channel_id(manifest, manifest_path)
    channel_value = channel_instagram_user_id(channel_id)
    if channel_value:
        return channel_value, f"channel:{channel_id}"
    fallback = env_value("INSTAGRAM_USER_ID", "IG_USER_ID")
    return fallback, "env:INSTAGRAM_USER_ID"


def resolve_instagram_access_token(
    explicit: str,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> tuple[str, str]:
    if explicit.strip():
        return explicit.strip(), "cli"
    channel_id = manifest_channel_id(manifest, manifest_path)
    channel_value = channel_instagram_access_token(channel_id)
    if channel_value:
        return channel_value, f"channel:{channel_id}"
    fallback = env_value("META_SYSTEM_USER_ACCESS_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "IG_ACCESS_TOKEN")
    return fallback, "env:META_SYSTEM_USER_ACCESS_TOKEN"


def graph_api_version() -> str:
    return env_value("META_GRAPH_API_VERSION", "INSTAGRAM_GRAPH_API_VERSION") or DEFAULT_GRAPH_API_VERSION


def graph_api_root() -> str:
    explicit = env_value("INSTAGRAM_GRAPH_API_ROOT", "META_GRAPH_API_ROOT")
    if explicit:
        return explicit.rstrip("/")
    domain = env_value("INSTAGRAM_GRAPH_DOMAIN", "META_GRAPH_DOMAIN").lower()
    if domain in {"instagram", "ig", "graph.instagram.com"}:
        return INSTAGRAM_GRAPH_API_ROOT
    return FACEBOOK_GRAPH_API_ROOT


def normalize_graph_version(value: str) -> str:
    value = value.strip()
    if not value:
        return DEFAULT_GRAPH_API_VERSION
    return value if value.startswith("v") else f"v{value}"


def absolute_slide_path(raw_path: object, manifest_path: Path) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise SystemExit("Manifest slide is missing a path")
    path = Path(raw_path)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def parse_media_url_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        key, sep, url = value.partition("=")
        if not sep or not key.strip() or not url.strip():
            raise SystemExit("--media-url must be formatted as INDEX_OR_FILENAME=URL")
        overrides[key.strip()] = url.strip()
    return overrides


def media_public_url(
    *,
    slide: dict[str, Any],
    local_path: Path,
    media_base_url: str,
    overrides: dict[str, str],
) -> str:
    index = str(slide.get("index") or "")
    direct = slide.get("instagram_url") or slide.get("public_url")
    keys = [index, local_path.name, str(local_path)]
    for key in keys:
        if key and key in overrides:
            return overrides[key]
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    if media_base_url:
        return f"{media_base_url.rstrip('/')}/{urllib.parse.quote(local_path.name)}"
    return ""


def public_url_for_key(public_base_url: str, key: str) -> str:
    return f"{public_base_url.rstrip('/')}/{urllib.parse.quote(key, safe='/~')}"


def infer_media_kind(slide: dict[str, Any], local_path: Path) -> str:
    slide_type = str(slide.get("type") or "").lower()
    suffix = local_path.suffix.lower()
    if "video" in slide_type or suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    raise SystemExit(f"Unsupported Instagram media file type: {local_path}")


def validate_public_url(url: str, *, dry_run: bool) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        mode = "Dry run" if dry_run else "Publishing"
        raise SystemExit(f"{mode} needs a public HTTPS media URL, got: {url}")


def build_media_items(
    manifest: dict[str, Any],
    manifest_path: Path,
    *,
    media_base_url: str,
    overrides: dict[str, str],
    dry_run: bool,
) -> list[MediaItem]:
    slides = manifest.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SystemExit("Manifest has no rendered slides to publish")
    if len(slides) > MAX_CAROUSEL_ITEMS:
        raise SystemExit(
            f"Instagram carousels support at most {MAX_CAROUSEL_ITEMS} items; "
            f"this manifest has {len(slides)}"
        )

    if dry_run and not media_base_url and not overrides:
        media_base_url = PLACEHOLDER_MEDIA_BASE_URL
        print(f"[instagram] dry run using placeholder media base URL {media_base_url}")

    items: list[MediaItem] = []
    for raw_slide in slides:
        if not isinstance(raw_slide, dict):
            continue
        local_path = absolute_slide_path(raw_slide.get("path"), manifest_path)
        if not local_path.exists():
            raise SystemExit(f"Rendered slide does not exist: {local_path}")
        public_url = media_public_url(
            slide=raw_slide,
            local_path=local_path,
            media_base_url=media_base_url,
            overrides=overrides,
        )
        if not public_url:
            raise SystemExit(
                "No public URL for slide "
                f"{raw_slide.get('index')}. Pass --media-base-url or --media-url."
            )
        validate_public_url(public_url, dry_run=dry_run)
        items.append(
            MediaItem(
                index=int(raw_slide.get("index") or len(items) + 1),
                kind=infer_media_kind(raw_slide, local_path),
                local_path=str(local_path),
                public_url=public_url,
                slide_type=str(raw_slide.get("type") or ""),
                source_url=str(raw_slide.get("source_url") or ""),
            )
        )
    if not items:
        raise SystemExit("Manifest has no usable slides to publish")
    return items


def default_r2_key_prefix(manifest_path: Path) -> str:
    name = manifest_path.parent.name.strip()
    return name if name and name != "." else "carousel"


def clean_r2_key_prefix(value: str) -> str:
    return value.strip().strip("/")


def r2_key_for_item(item: MediaItem, key_prefix: str) -> str:
    filename = Path(item.local_path).name
    key_prefix = clean_r2_key_prefix(key_prefix)
    return f"{key_prefix}/{filename}" if key_prefix else filename


def r2_config(args: argparse.Namespace, manifest_path: Path, media_base_url: str) -> R2Config:
    public_base_url = args.r2_public_base_url.strip() or media_base_url.strip()
    config = R2Config(
        account_id=env_value("R2_ACCOUNT_ID"),
        access_key_id=env_value("R2_ACCESS_KEY_ID"),
        secret_access_key=env_value("R2_SECRET_ACCESS_KEY"),
        bucket=args.r2_bucket.strip() or env_value("R2_BUCKET"),
        public_base_url=public_base_url,
        key_prefix=clean_r2_key_prefix(
            args.r2_key_prefix
            if args.r2_key_prefix is not None
            else env_value("R2_KEY_PREFIX") or default_r2_key_prefix(manifest_path)
        ),
    )
    missing = [
        name
        for name, value in (
            ("R2_ACCOUNT_ID", config.account_id),
            ("R2_ACCESS_KEY_ID", config.access_key_id),
            ("R2_SECRET_ACCESS_KEY", config.secret_access_key),
            ("R2_BUCKET", config.bucket),
            ("INSTAGRAM_MEDIA_BASE_URL or --r2-public-base-url", config.public_base_url),
        )
        if not value
    ]
    if missing:
        raise SystemExit("Missing R2 upload configuration: " + ", ".join(missing))
    validate_public_url(config.public_base_url, dry_run=False)
    return config


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def r2_signing_key(secret_access_key: str, datestamp: str) -> bytes:
    date_key = hmac_sha256(f"AWS4{secret_access_key}".encode("utf-8"), datestamp)
    region_key = hmac_sha256(date_key, "auto")
    service_key = hmac_sha256(region_key, "s3")
    return hmac_sha256(service_key, "aws4_request")


def r2_put_object(path: Path, key: str, config: R2Config, *, timeout: int) -> dict[str, Any]:
    data = path.read_bytes()
    payload_hash = sha256_hex(data)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    encoded_key = urllib.parse.quote(key, safe="/~")
    host = f"{config.bucket}.{config.account_id}.r2.cloudflarestorage.com"
    canonical_uri = f"/{encoded_key}"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    canonical_request = "\n".join(
        [
            "PUT",
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    credential_scope = f"{datestamp}/auto/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            sha256_hex(canonical_request.encode("utf-8")),
        ]
    )
    signature = hmac.new(
        r2_signing_key(config.secret_access_key, datestamp),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    request = urllib.request.Request(
        f"https://{host}{canonical_uri}",
        data=data,
        method="PUT",
        headers={
            "Authorization": (
                "AWS4-HMAC-SHA256 "
                f"Credential={config.access_key_id}/{credential_scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
            "Content-Type": content_type,
            "User-Agent": "carousel-app/1.0",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"R2 upload failed for {path.name}: HTTP {exc.code} {body[:400]}") from exc
    except OSError as exc:
        raise SystemExit(f"R2 upload failed for {path.name}: {exc}") from exc
    return {
        "key": key,
        "local_path": str(path),
        "public_url": public_url_for_key(config.public_base_url, key),
        "status": status,
        "bytes": len(data),
        "content_type": content_type,
    }


def upload_media_to_r2(
    items: list[MediaItem],
    config: R2Config,
    *,
    timeout: int,
) -> list[dict[str, Any]]:
    uploads: list[dict[str, Any]] = []
    for item in items:
        local_path = Path(item.local_path)
        key = r2_key_for_item(item, config.key_prefix)
        print(f"[r2] uploading slide {item.index} -> {config.bucket}/{key}")
        result = r2_put_object(local_path, key, config, timeout=timeout)
        item.public_url = str(result["public_url"])
        uploads.append(result)
    return uploads


def read_caption(args: argparse.Namespace, manifest: dict[str, Any]) -> str:
    if args.caption_file:
        return args.caption_file.read_text().strip()
    if args.caption is not None:
        return args.caption.strip()
    context = manifest.get("title_context") if isinstance(manifest.get("title_context"), dict) else {}
    manifest_caption = str(manifest.get("instagram_caption") or "").strip()
    if manifest_caption:
        return manifest_caption
    context_caption = str(context.get("instagram_caption") or "").strip()
    if context_caption:
        return context_caption
    topic = str(context.get("topic") or "").strip()
    source_url = str(manifest.get("source_url") or "").strip()
    parts = [topic] if topic else []
    if source_url:
        parts.extend(["", f"Source: {source_url}"])
    return "\n".join(parts).strip()


def media_create_params(
    item: MediaItem,
    *,
    caption: str,
    carousel_item: bool,
    single_video_media_type: str,
) -> dict[str, str]:
    params: dict[str, str] = {}
    if item.kind == "image":
        params["image_url"] = item.public_url
    elif item.kind == "video":
        params["media_type"] = "VIDEO" if carousel_item else single_video_media_type
        params["video_url"] = item.public_url
    else:
        raise SystemExit(f"Unsupported media kind: {item.kind}")
    if carousel_item:
        params["is_carousel_item"] = "true"
    elif caption:
        params["caption"] = caption
    return params


def graph_request(
    path: str,
    *,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
    params: dict[str, str] | None = None,
    method: str = "POST",
    timeout: int = 90,
) -> dict[str, Any]:
    params = dict(params or {})
    params["access_token"] = access_token
    encoded = urllib.parse.urlencode(params).encode("utf-8")
    url = f"{graph_api_root.rstrip('/')}/{graph_version}/{path.lstrip('/')}"
    data = encoded
    if method.upper() == "GET":
        url = f"{url}?{encoded.decode('utf-8')}"
        data = None
    request = urllib.request.Request(
        url,
        data=data,
        method=method.upper(),
        headers={"User-Agent": "carousel-app/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"error": {"message": body[:500]}}
        error = payload.get("error") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else body[:500]
        raise SystemExit(f"Instagram Graph API error {exc.code}: {message}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Instagram Graph API request failed: {exc}") from exc


def container_status(
    container_id: str,
    *,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> dict[str, Any]:
    return graph_request(
        container_id,
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        method="GET",
        params={"fields": "status_code,status"},
        timeout=30,
    )


def wait_for_container(
    container_id: str,
    *,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
    timeout_seconds: int,
    interval_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = container_status(
            container_id,
            access_token=access_token,
            graph_version=graph_version,
            graph_api_root=graph_api_root,
        )
        status_code = str(last.get("status_code") or "")
        status = str(last.get("status") or "")
        if status_code in FINISHED_STATUS_CODES:
            return last
        if status_code in WAIT_STATUS_CODES:
            raise InstagramContainerWaitError(
                f"Instagram media container {container_id} failed: "
                f"{status_code} {status}".strip()
            )
        print(f"[instagram] container {container_id} status={status_code or 'pending'}")
        time.sleep(interval_seconds)
    raise InstagramContainerWaitError(f"Timed out waiting for Instagram media container {container_id}: {last}")


def create_container(
    instagram_user_id: str,
    params: dict[str, str],
    *,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> str:
    response = graph_request(
        f"{instagram_user_id}/media",
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params=params,
        method="POST",
    )
    container_id = str(response.get("id") or "")
    if not container_id:
        raise SystemExit(f"Instagram did not return a media container id: {response}")
    return container_id


def publish_container(
    instagram_user_id: str,
    creation_id: str,
    *,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> dict[str, Any]:
    response = graph_request(
        f"{instagram_user_id}/media_publish",
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={"creation_id": creation_id},
        method="POST",
    )
    if not response.get("id"):
        raise SystemExit(f"Instagram did not return a published media id: {response}")
    return response


def fetch_permalink(
    media_id: str,
    *,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> dict[str, Any]:
    return graph_request(
        media_id,
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={"fields": "permalink,media_type,media_product_type"},
        method="GET",
        timeout=30,
    )


def api_steps(items: list[MediaItem], caption: str, *, single_video_media_type: str) -> list[dict[str, Any]]:
    if len(items) == 1:
        return [
            {
                "action": "create_media_container",
                "params": media_create_params(
                    items[0],
                    caption=caption,
                    carousel_item=False,
                    single_video_media_type=single_video_media_type,
                ),
            },
            {"action": "publish_media_container", "creation_id": "<container_id>"},
        ]
    return [
        *[
            {
                "action": "create_carousel_item_container",
                "slide_index": item.index,
                "params": media_create_params(
                    item,
                    caption=caption,
                    carousel_item=True,
                    single_video_media_type=single_video_media_type,
                ),
            }
            for item in items
        ],
        {
            "action": "create_carousel_container",
            "params": {
                "media_type": "CAROUSEL",
                "children": "<child_container_ids>",
                **({"caption": caption} if caption else {}),
            },
        },
        {"action": "publish_carousel_container", "creation_id": "<carousel_container_id>"},
    ]


def publish_to_instagram(
    items: list[MediaItem],
    *,
    caption: str,
    instagram_user_id: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
    wait_timeout: int,
    wait_interval: int,
    single_video_media_type: str,
) -> dict[str, Any]:
    child_ids: list[str] = []
    item_results: list[dict[str, Any]] = []
    is_carousel = len(items) > 1

    for item in items:
        params = media_create_params(
            item,
            caption=caption,
            carousel_item=is_carousel,
            single_video_media_type=single_video_media_type,
        )
        print(f"[instagram] creating {'carousel item' if is_carousel else 'media'} container for slide {item.index}")
        container_id = create_container(
            instagram_user_id,
            params,
            access_token=access_token,
            graph_version=graph_version,
            graph_api_root=graph_api_root,
        )
        status: dict[str, Any] = {}
        if item.kind == "video":
            status = wait_for_container(
                container_id,
                access_token=access_token,
                graph_version=graph_version,
                graph_api_root=graph_api_root,
                timeout_seconds=wait_timeout,
                interval_seconds=wait_interval,
            )
        child_ids.append(container_id)
        item_results.append({"slide_index": item.index, "container_id": container_id, "status": status})

    if is_carousel:
        params = {"media_type": "CAROUSEL", "children": ",".join(child_ids)}
        if caption:
            params["caption"] = caption
        print(f"[instagram] creating carousel container with {len(child_ids)} child item(s)")
        publish_container_id = create_container(
            instagram_user_id,
            params,
            access_token=access_token,
            graph_version=graph_version,
            graph_api_root=graph_api_root,
        )
        parent_status = wait_for_container(
            publish_container_id,
            access_token=access_token,
            graph_version=graph_version,
            graph_api_root=graph_api_root,
            timeout_seconds=wait_timeout,
            interval_seconds=wait_interval,
        )
    else:
        publish_container_id = child_ids[0]
        parent_status = item_results[0].get("status") or {}

    print(f"[instagram] publishing container {publish_container_id}")
    published = publish_container(
        instagram_user_id,
        publish_container_id,
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
    )
    media_id = str(published.get("id") or "")
    permalink: dict[str, Any] = {}
    if media_id:
        try:
            permalink = fetch_permalink(
                media_id,
                access_token=access_token,
                graph_version=graph_version,
                graph_api_root=graph_api_root,
            )
        except SystemExit as exc:
            print(f"[instagram] published, but permalink lookup failed: {exc}")
    return {
        "child_containers": item_results,
        "publish_container_id": publish_container_id,
        "publish_container_status": parent_status,
        "published": published,
        "permalink": permalink,
    }


def publish_to_instagram_with_retries(
    items: list[MediaItem],
    *,
    caption: str,
    instagram_user_id: str,
    access_token: str,
    graph_version: str,
    graph_api_root: str,
    wait_timeout: int,
    wait_interval: int,
    single_video_media_type: str,
    publish_retries: int,
    publish_retry_delay: int,
) -> dict[str, Any]:
    attempts = max(0, publish_retries) + 1
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            print(f"[instagram] retry attempt {attempt}/{attempts} with fresh media containers")
        try:
            return publish_to_instagram(
                items,
                caption=caption,
                instagram_user_id=instagram_user_id,
                access_token=access_token,
                graph_version=graph_version,
                graph_api_root=graph_api_root,
                wait_timeout=wait_timeout,
                wait_interval=wait_interval,
                single_video_media_type=single_video_media_type,
            )
        except InstagramContainerWaitError as exc:
            if attempt >= attempts:
                raise SystemExit(str(exc)) from exc
            print(f"[instagram] pre-publish container attempt {attempt}/{attempts} failed: {exc}")
            if publish_retry_delay > 0:
                print(f"[instagram] retrying in {publish_retry_delay}s")
                time.sleep(publish_retry_delay)
    raise SystemExit("Instagram publish retry loop ended unexpectedly")


def build_report(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    items: list[MediaItem],
    caption: str,
    dry_run: bool,
    graph_version: str,
    graph_api_root: str,
    instagram_user_id: str,
    instagram_user_id_source: str,
    access_token_source: str,
    single_video_media_type: str,
    uploads: list[dict[str, Any]] | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "created_at": utc_now(),
        "dry_run": dry_run,
        "manifest_path": str(manifest_path),
        "source_url": manifest.get("source_url"),
        "account_name": manifest.get("account_name"),
        "instagram_user_id": instagram_user_id,
        "instagram_user_id_source": instagram_user_id_source,
        "access_token_source": access_token_source,
        "graph_api_version": graph_version,
        "graph_api_root": graph_api_root,
        "caption": caption,
        "media": [asdict(item) for item in items],
        "uploads": uploads or [],
        "api_steps": api_steps(items, caption, single_video_media_type=single_video_media_type),
        "result": result or {},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Publish a carousel manifest to Instagram")
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--media-base-url",
        default=env_value("INSTAGRAM_MEDIA_BASE_URL", "IG_MEDIA_BASE_URL"),
        help="Public HTTPS base URL that contains the rendered slide files",
    )
    parser.add_argument(
        "--media-url",
        action="append",
        default=[],
        help="Override one slide URL, formatted as INDEX_OR_FILENAME=https://...",
    )
    parser.add_argument(
        "--upload-r2",
        action="store_true",
        help="Upload rendered slides to Cloudflare R2 before building Instagram URLs",
    )
    parser.add_argument(
        "--r2-bucket",
        default=env_value("R2_BUCKET"),
        help="R2 bucket name for --upload-r2",
    )
    parser.add_argument(
        "--r2-key-prefix",
        help="Object key prefix for R2 uploads (default: manifest folder name)",
    )
    parser.add_argument(
        "--r2-public-base-url",
        default=env_value("R2_PUBLIC_BASE_URL"),
        help="Public R2 base URL. Defaults to --media-base-url / INSTAGRAM_MEDIA_BASE_URL.",
    )
    parser.add_argument("--r2-timeout", type=int, default=120)
    parser.add_argument(
        "--caption",
        help="Instagram caption. Defaults to manifest instagram_caption, then topic plus source URL.",
    )
    parser.add_argument("--caption-file", type=Path, help="Read the Instagram caption from a text file")
    parser.add_argument(
        "--instagram-user-id",
        default="",
        help=(
            "Instagram professional account ID. Defaults to the manifest channel's "
            "publishing.instagram_user_id, then INSTAGRAM_USER_ID / IG_USER_ID."
        ),
    )
    parser.add_argument(
        "--access-token",
        default="",
        help=(
            "Instagram Graph API access token. Defaults to channel-specific "
            "META_SYSTEM_USER_ACCESS_TOKEN_<CHANNEL>, then global "
            "META_SYSTEM_USER_ACCESS_TOKEN / INSTAGRAM_ACCESS_TOKEN / IG_ACCESS_TOKEN."
        ),
    )
    parser.add_argument("--graph-api-version", default=graph_api_version())
    parser.add_argument(
        "--graph-api-root",
        default=graph_api_root(),
        help=(
            "Graph API root. Use https://graph.instagram.com for App Dashboard "
            "Instagram business login tokens; defaults from INSTAGRAM_GRAPH_DOMAIN."
        ),
    )
    parser.add_argument(
        "--single-video-media-type",
        choices=("VIDEO", "REELS"),
        default=env_value("INSTAGRAM_SINGLE_VIDEO_MEDIA_TYPE") or "VIDEO",
        help="media_type for a one-item video publish",
    )
    parser.add_argument("--wait-timeout", type=int, default=600)
    parser.add_argument("--wait-interval", type=int, default=10)
    parser.add_argument(
        "--publish-retries",
        type=int,
        default=env_int(DEFAULT_PUBLISH_RETRIES, "INSTAGRAM_PUBLISH_RETRIES", "IG_PUBLISH_RETRIES"),
        help=(
            "Retry failed pre-publish Instagram media container processing with fresh "
            f"containers. Default: {DEFAULT_PUBLISH_RETRIES}"
        ),
    )
    parser.add_argument(
        "--publish-retry-delay",
        type=int,
        default=env_int(
            DEFAULT_PUBLISH_RETRY_DELAY_SECONDS,
            "INSTAGRAM_PUBLISH_RETRY_DELAY",
            "IG_PUBLISH_RETRY_DELAY",
        ),
        help=(
            "Seconds to wait between pre-publish retry attempts. "
            f"Default: {DEFAULT_PUBLISH_RETRY_DELAY_SECONDS}"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and write a publish plan only")
    parser.add_argument("--out", type=Path, help=f"Write report JSON here (default: {DEFAULT_REPORT_NAME})")
    parser.add_argument("--print-json", action="store_true", help="Print the report JSON to stdout")
    return parser


def main() -> int:
    load_env_file(ROOT / ".env")
    args = build_parser().parse_args()
    manifest_path = args.manifest.resolve()
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise SystemExit("Manifest JSON must be an object")
    instagram_user_id, instagram_user_id_source = resolve_instagram_user_id(
        args.instagram_user_id,
        manifest,
        manifest_path,
    )
    access_token, access_token_source = resolve_instagram_access_token(
        args.access_token,
        manifest,
        manifest_path,
    )

    graph_version = normalize_graph_version(args.graph_api_version)
    graph_root = args.graph_api_root.rstrip("/")
    media_base_url = args.media_base_url.strip()
    if args.upload_r2 and not media_base_url:
        media_base_url = args.r2_public_base_url.strip()
    media_items = build_media_items(
        manifest,
        manifest_path,
        media_base_url=media_base_url,
        overrides=parse_media_url_overrides(args.media_url),
        dry_run=args.dry_run,
    )
    uploads: list[dict[str, Any]] = []
    if args.upload_r2:
        uploads = upload_media_to_r2(
            media_items,
            r2_config(args, manifest_path, media_base_url),
            timeout=args.r2_timeout,
        )
    caption = read_caption(args, manifest)

    result: dict[str, Any] | None = None
    if args.dry_run:
        print(f"[instagram] dry run: validated {len(media_items)} media item(s)")
    else:
        if not instagram_user_id:
            raise SystemExit("INSTAGRAM_USER_ID or --instagram-user-id is required to publish")
        if not access_token:
            raise SystemExit("INSTAGRAM_ACCESS_TOKEN or --access-token is required to publish")
        result = publish_to_instagram_with_retries(
            media_items,
            caption=caption,
            instagram_user_id=instagram_user_id,
            access_token=access_token,
            graph_version=graph_version,
            graph_api_root=graph_root,
            wait_timeout=args.wait_timeout,
            wait_interval=args.wait_interval,
            single_video_media_type=args.single_video_media_type,
            publish_retries=args.publish_retries,
            publish_retry_delay=args.publish_retry_delay,
        )

    report = build_report(
        manifest_path=manifest_path,
        manifest=manifest,
        items=media_items,
        caption=caption,
        dry_run=args.dry_run,
        graph_version=graph_version,
        graph_api_root=graph_root,
        instagram_user_id=instagram_user_id,
        instagram_user_id_source=instagram_user_id_source,
        access_token_source=access_token_source,
        single_video_media_type=args.single_video_media_type,
        uploads=uploads,
        result=result,
    )
    report_path = args.out or manifest_path.with_name(DEFAULT_REPORT_NAME)
    write_json(report_path, report)
    print(f"[instagram] wrote report -> {report_path}")
    if args.print_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
