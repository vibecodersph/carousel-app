#!/usr/bin/env python3
"""Plan and publish channel-aware Instagram reels from a clips folder.

``plan`` scans clip directories for the channel's configured media filename,
writes one Instagram publisher manifest per reel, and assigns publish times.
``run-due`` is intended for cron or another periodic runner; it publishes only
jobs whose scheduled time has arrived and persists their status after each run.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import reel_ledger
from channel import Channel, available_channels, load_channel

ROOT = Path(__file__).resolve().parent
DEFAULT_REEL_OUTPUTS = ROOT.parent / "reel-app" / "outputs"
DEFAULT_OUT = ROOT / "out" / "reel_schedules"
DEFAULT_MEDIA_FILENAME = "reel.mp4"
DEFAULT_TIMEZONE = "Asia/Tokyo"
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_PUBLISH_TIME = "09:00"
SCHEDULE_VERSION = 1

# reel-app multi-channel layout: one clip folder ships a file per channel, where
# the channel and caption language are encoded in the name reel.<lang>.<channel>.mp4
CHANNEL_MEDIA_RE = re.compile(r"^reel\.([A-Za-z]{2,5})\.([A-Za-z0-9_-]+)\.mp4$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Platform registry: the same ledger + scheduler drives both Instagram reels
# and TikTok. A platform selects which channel.json publishing block holds its
# posting slots, which publisher script ``run-due`` shells out to, the report
# filename that carries the published id back, and a separate ledger db so one
# clip can be tracked independently on each platform.
PLATFORMS: dict[str, dict[str, Any]] = {
    "instagram": {
        "settings_key": "instagram_reels",
        "publisher": "instagram_publish.py",
        "report_name": "instagram_publish.json",
        "db_path": reel_ledger.DEFAULT_DB_PATH,
    },
    "tiktok": {
        "settings_key": "tiktok",
        "publisher": "tiktok_publish.py",
        "report_name": "tiktok_publish.json",
        "db_path": ROOT / "state" / "tiktok.db",
    },
}
DEFAULT_PLATFORM = "instagram"


def platform_config(platform: str) -> dict[str, Any]:
    try:
        return PLATFORMS[platform]
    except KeyError:
        raise SystemExit(f"Unknown platform '{platform}'. Choose from {sorted(PLATFORMS)}.")


def resolve_platform(args: argparse.Namespace) -> str:
    return str(getattr(args, "platform", None) or DEFAULT_PLATFORM)


def settings_key_for(platform: str) -> str:
    return str(platform_config(platform)["settings_key"])


def resolve_db(args: argparse.Namespace) -> Path:
    """``--db`` wins; otherwise default to the platform's ledger (reels/tiktok.db)."""
    explicit = getattr(args, "db", None)
    if explicit is not None:
        return explicit
    return platform_config(resolve_platform(args))["db_path"]


# Map legacy schedule.json status vocabulary onto the ledger lifecycle.
LEGACY_STATUS_MAP = {
    "scheduled": reel_ledger.STATUS_SCHEDULED,
    "publishing": reel_ledger.STATUS_PUBLISHING,
    "publish_previewed": reel_ledger.STATUS_PREVIEWED,
    "published": reel_ledger.STATUS_PUBLISHED,
    "publish_failed": reel_ledger.STATUS_FAILED,
}

GENERIC_HASHTAGS = ["#AI", "#GenerativeAI", "#AIDevelopment"]
JAPANESE_HASHTAGS = ["#AIニュース", "#生成AI", "#AI開発", "#エンジニア"]
TOPIC_HASHTAGS = (
    (("claude code",), "#ClaudeCode"),
    (("anthropic", "claude"), "#Anthropic"),
    (("openai", "chatgpt", "codex"), "#OpenAI"),
    (("ai agent", "ai agents", "sub-agent", "sub agent"), "#AIAgent"),
    (("prompt",), "#プロンプト"),
    (("terminal", "command line", "cli"), "#CLI"),
    (("startup", "founder"), "#スタートアップ"),
    (("developer", "engineer", "codebase", "software"), "#ソフトウェア開発"),
)

WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"No JSON file found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse JSON in {path}: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def reel_settings(channel: Channel, settings_key: str = "instagram_reels") -> dict[str, Any]:
    publishing = channel.publishing if isinstance(channel.publishing, dict) else {}
    settings = publishing.get(settings_key)
    return settings if isinstance(settings, dict) else {}


def setting_text(settings: dict[str, Any], key: str, default: str) -> str:
    value = str(settings.get(key) or "").strip()
    return value or default


def timezone_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Unknown timezone: {name}") from exc


def parse_datetime(value: str, timezone_name: str, *, date_clock: str | None = None) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    if DATE_RE.match(normalized):
        clock = parse_clock(date_clock or "00:00")
        return datetime.combine(datetime.fromisoformat(normalized).date(), clock, tzinfo=timezone_for(timezone_name))
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid date/time '{value}'. Use YYYY-MM-DD or ISO 8601, "
            "for example 2026-06-24 or 2026-06-24T09:00:00+09:00."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_for(timezone_name))
    return parsed


def parse_date(value: str) -> date:
    normalized = value.strip()
    if not DATE_RE.match(normalized):
        raise SystemExit(f"Invalid date '{value}'. Use YYYY-MM-DD, for example 2026-06-24.")
    return datetime.fromisoformat(normalized).date()


def scheduler_date_arg(args: argparse.Namespace) -> str | None:
    positional = str(getattr(args, "date", "") or "").strip()
    flagged = str(getattr(args, "start_at", "") or "").strip()
    if positional and flagged:
        raise SystemExit("Use either DATE or --start-at, not both")
    return positional or flagged or None


def has_explicit_time(value: str | None) -> bool:
    if not value:
        return False
    return not DATE_RE.match(value.strip().replace("Z", "+00:00"))


def row_scheduled_date(row: Any) -> date | None:
    try:
        parsed = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_for(DEFAULT_TIMEZONE))
    return parsed.date()


def row_is_publishable(row: Any) -> tuple[bool, str]:
    media_path = Path(str(row["media_path"] or ""))
    manifest_path = Path(str(row["manifest_path"] or ""))
    if not media_path.is_file():
        return False, f"missing media: {media_path}"
    if not manifest_path.is_file():
        return False, f"missing manifest: {manifest_path}"
    return True, ""


def parse_clock(value: str) -> time:
    try:
        parsed = time.fromisoformat(value.strip())
    except ValueError as exc:
        raise SystemExit(f"Invalid publish time '{value}'. Use HH:MM, for example 09:00.") from exc
    return parsed.replace(tzinfo=None)


def next_publish_time(*, now: datetime, timezone_name: str, clock: str) -> datetime:
    tz = timezone_for(timezone_name)
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), parse_clock(clock), tzinfo=tz)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def slot_clocks(settings: dict[str, Any]) -> list[time]:
    raw_slots = settings.get("slots")
    values = raw_slots if isinstance(raw_slots, list) else []
    clocks: list[time] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            clocks.append(parse_clock(text))
    if not clocks:
        clocks.append(parse_clock(setting_text(settings, "publish_time", DEFAULT_PUBLISH_TIME)))
    return sorted(clocks)


def skip_weekdays(settings: dict[str, Any]) -> set[int]:
    raw = settings.get("skip_days")
    values = raw if isinstance(raw, list) else []
    skipped: set[int] = set()
    for value in values:
        text = str(value).strip().lower()
        if not text:
            continue
        if text.isdigit():
            number = int(text)
            if 0 <= number <= 6:
                skipped.add(number)
            continue
        if text in WEEKDAY_ALIASES:
            skipped.add(WEEKDAY_ALIASES[text])
    return skipped


def deterministic_jitter(content_hash: str, jitter_minutes: int) -> int:
    if jitter_minutes <= 0:
        return 0
    window = jitter_minutes * 2 + 1
    digest = hashlib.sha256(content_hash.encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % window) - jitter_minutes


def slot_key(moment: datetime, clock: time) -> str:
    return f"{moment.date().isoformat()}T{clock.strftime('%H:%M')}"


def slot_occupancy_key(moment: datetime, clocks: list[time], jitter_minutes: int) -> str:
    tolerance = max(0, jitter_minutes)
    for clock in clocks:
        base = datetime.combine(moment.date(), clock, tzinfo=moment.tzinfo)
        diff = abs((moment - base).total_seconds()) / 60
        if diff <= tolerance:
            return slot_key(base, clock)
    return moment.replace(second=0, microsecond=0).isoformat()


def occupied_slot_keys(
    rows: list[Any],
    *,
    timezone_name: str,
    clocks: list[time],
    jitter_minutes: int,
) -> set[str]:
    tz = timezone_for(timezone_name)
    occupied: set[str] = set()
    tolerance = max(0, jitter_minutes)
    for row in rows:
        raw = row["scheduled_at"] if isinstance(row, dict) or hasattr(row, "keys") else None
        if not raw:
            continue
        try:
            scheduled = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=tz)
        local = scheduled.astimezone(tz)
        occupied.add(slot_occupancy_key(local, clocks, tolerance))
    return occupied


def row_value(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def parse_row_datetime(value: Any, timezone_name: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_for(timezone_name))
    return parsed


def reflow_blocking_rows(rows: list[Any], *, start_at: datetime, timezone_name: str) -> list[Any]:
    """Rows that should still occupy slots while laying out a queue reflow."""
    blockers: list[Any] = []
    for row in rows:
        if str(row_value(row, "status") or "") == reel_ledger.STATUS_PUBLISHED:
            published_at = parse_row_datetime(row_value(row, "published_at"), timezone_name)
            if published_at is not None and published_at < start_at:
                continue
        blockers.append(row)
    return blockers


def next_open_slots(
    *,
    channel: Channel,
    start_at: datetime,
    existing_rows: list[Any],
    count: int,
    jitter_override: int | None = None,
    content_hashes: list[str] | None = None,
    settings_key: str = "instagram_reels",
    include_start_at: bool = False,
) -> list[datetime]:
    if count <= 0:
        return []
    settings = reel_settings(channel, settings_key)
    timezone_name = setting_text(settings, "timezone", DEFAULT_TIMEZONE)
    tz = timezone_for(timezone_name)
    local_start = start_at.astimezone(tz)
    clocks = slot_clocks(settings)
    skipped = skip_weekdays(settings)
    configured_jitter = int(settings.get("jitter_minutes") or 0)
    jitter_minutes = configured_jitter if jitter_override is None else jitter_override
    occupied = occupied_slot_keys(
        existing_rows,
        timezone_name=timezone_name,
        clocks=clocks,
        jitter_minutes=jitter_minutes,
    )
    hashes = content_hashes or [str(i) for i in range(count)]
    slots: list[datetime] = []
    if include_start_at and local_start.weekday() not in skipped:
        manual = local_start.replace(microsecond=0)
        key = slot_occupancy_key(manual, clocks, jitter_minutes)
        if key not in occupied:
            slots.append(manual)
            occupied.add(key)
    day = local_start.date()
    while len(slots) < count:
        if day.weekday() not in skipped:
            for clock in clocks:
                base = datetime.combine(day, clock, tzinfo=tz)
                if base < local_start:
                    continue
                key = slot_key(base, clock)
                if key in occupied:
                    continue
                offset = deterministic_jitter(hashes[len(slots)], jitter_minutes)
                planned = base + timedelta(minutes=offset)
                slots.append(planned.replace(microsecond=0))
                occupied.add(key)
                if len(slots) >= count:
                    break
        day += timedelta(days=1)
    return slots


def safe_job_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", value).strip("-._")
    return cleaned or "reel"


def load_notes(clip_dir: Path) -> tuple[dict[str, Any], Path | None]:
    notes_path = clip_dir / "notes.json"
    if not notes_path.exists():
        return {}, None
    data = read_json(notes_path)
    if not isinstance(data, dict):
        raise SystemExit(f"Clip notes must be a JSON object: {notes_path}")
    return data, notes_path


def load_source_metadata(clips_dir: Path) -> dict[str, Any]:
    metadata_path = clips_dir.expanduser().resolve().parent / "metadata.json"
    if not metadata_path.exists():
        return {}
    data = read_json(metadata_path)
    return data if isinstance(data, dict) else {}


def source_metadata_value(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = re.sub(r"\s+", " ", str(metadata.get(key) or "")).strip()
        if value:
            return value
    return ""


def clip_sort_key(item: tuple[Path, dict[str, Any], Path | None]) -> tuple[int, str]:
    clip_dir, notes, _ = item
    try:
        index = int(notes.get("index"))
    except (TypeError, ValueError):
        match = re.match(r"(\d+)", clip_dir.name)
        index = int(match.group(1)) if match else 1_000_000
    return index, clip_dir.name


def discover_clips(clips_dir: Path, media_filename: str) -> list[tuple[Path, dict[str, Any], Path | None]]:
    clips_dir = clips_dir.expanduser().resolve()
    if not clips_dir.is_dir():
        raise SystemExit(f"Clips folder does not exist: {clips_dir}")
    discovered: list[tuple[Path, dict[str, Any], Path | None]] = []
    for media_path in clips_dir.rglob(media_filename):
        if media_path.is_file():
            notes, notes_path = load_notes(media_path.parent)
            discovered.append((media_path.parent, notes, notes_path))
    discovered.sort(key=clip_sort_key)
    if not discovered:
        raise SystemExit(f"No '{media_filename}' files found under {clips_dir}")
    return discovered


def parse_channel_media(name: str) -> tuple[str, str] | None:
    """Pull (lang, channel_id) out of a reel.<lang>.<channel>.mp4 filename."""
    match = CHANNEL_MEDIA_RE.match(name)
    if not match:
        return None
    return match.group(1), match.group(2)


def load_one_liners(clip_dir: Path) -> dict[str, Any]:
    """Per-language localized hooks (e.g. {"ja": "..."}); empty when absent."""
    path = clip_dir / "one_liners.json"
    if not path.exists():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def routed_title(lang: str, notes: dict[str, Any], one_liners: dict[str, Any]) -> str:
    """Caption title for a variant: localized hook for non-English, else one_liner.

    The JP hook lives in one_liners.json (not notes.json), so prefer it; fall
    back to notes.one_liner_translated, then the English notes.one_liner.
    """
    if lang and lang != "en":
        localized = re.sub(r"\s+", " ", str(one_liners.get(lang) or "")).strip()
        if localized:
            return localized
        translated = note_text(notes, "one_liner_translated")
        if translated:
            return translated
    return note_text(notes, "one_liner")


def discover_channel_clips(
    clips_dir: Path,
) -> list[tuple[Path, str, str, Path, dict[str, Any], Path | None]]:
    """Find every reel.<lang>.<channel>.mp4 variant under a clips folder.

    Returns (clip_dir, lang, channel_id, media_path, notes, notes_path) tuples,
    fanning one multi-channel clip folder out into one entry per channel.
    """
    clips_dir = clips_dir.expanduser().resolve()
    if not clips_dir.is_dir():
        raise SystemExit(f"Clips folder does not exist: {clips_dir}")
    discovered: list[tuple[Path, str, str, Path, dict[str, Any], Path | None]] = []
    for media_path in sorted(clips_dir.rglob("reel.*.mp4")):
        if not media_path.is_file():
            continue
        parsed = parse_channel_media(media_path.name)
        if parsed is None:
            continue
        lang, channel_id = parsed
        notes, notes_path = load_notes(media_path.parent)
        discovered.append((media_path.parent, lang, channel_id, media_path, notes, notes_path))
    return discovered


def note_text(notes: dict[str, Any], key: str) -> str:
    return re.sub(r"\s+", " ", str(notes.get(key) or "")).strip()


def clip_title(channel: Channel, clip_dir: Path, notes: dict[str, Any]) -> str:
    translated = note_text(notes, "one_liner_translated")
    original = note_text(notes, "one_liner")
    if channel.language_name.lower().startswith("japanese") and translated:
        return translated
    if original:
        return original
    if translated:
        return translated
    return re.sub(r"^\d+[-_]*", "", clip_dir.name).replace("-", " ").strip().capitalize()


def configured_hashtags(channel: Channel, settings: dict[str, Any]) -> list[str]:
    configured = settings.get("hashtags")
    if isinstance(configured, list):
        values = [str(value).strip() for value in configured]
        valid = [value for value in values if value.startswith("#") and not re.search(r"\s", value)]
        if valid:
            return valid
    if channel.language_name.lower().startswith("japanese"):
        return list(JAPANESE_HASHTAGS)
    return list(GENERIC_HASHTAGS)


def caption_hashtags(channel: Channel, notes: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    hashtags = configured_hashtags(channel, settings)
    searchable = " ".join(
        note_text(notes, key).lower()
        for key in ("one_liner", "one_liner_translated", "reason", "source_chapter", "transcript")
    )
    for needles, hashtag in TOPIC_HASHTAGS:
        if any(needle in searchable for needle in needles):
            hashtags.append(hashtag)
    deduplicated: list[str] = []
    seen: set[str] = set()
    for hashtag in hashtags:
        normalized = hashtag.casefold()
        if normalized not in seen:
            deduplicated.append(hashtag)
            seen.add(normalized)
    return deduplicated[:8]


def build_caption(
    channel: Channel,
    clip_dir: Path,
    notes: dict[str, Any],
    *,
    source_url: str = "",
    title_override: str | None = None,
    settings_key: str = "instagram_reels",
) -> tuple[str, list[str]]:
    settings = reel_settings(channel, settings_key)
    title = title_override or clip_title(channel, clip_dir, notes)
    hashtags = caption_hashtags(channel, notes, settings)
    if channel.language_name.lower().startswith("japanese"):
        context = setting_text(
            settings,
            "caption_context",
            "AI開発の現場で何が起きているのか、短いクリップで紹介します。",
        )
        cta = setting_text(settings, "caption_cta", "気になったら保存して、あとで見返してください。")
    else:
        context = setting_text(
            settings,
            "caption_context",
            "A short look at what this means for people building with AI.",
        )
        cta = setting_text(settings, "caption_cta", "Save this reel to revisit later.")
    blocks = [title, context, cta, " ".join(hashtags)]
    if source_url:
        blocks.append(f"Source: {source_url}")
    caption = "\n\n".join(blocks)
    return caption.strip(), hashtags


def channel_manifest(channel: Channel) -> dict[str, str]:
    return {
        "id": channel.id,
        "account_name": channel.account_name,
        "brand_name": channel.brand_name,
        "handle": channel.handle,
        "language_name": channel.language_name,
        "audience": channel.audience,
        "voice_doc": channel.voice_doc_rel,
    }


def make_manifest(
    *,
    channel: Channel,
    clip_dir: Path,
    media_path: Path,
    notes: dict[str, Any],
    notes_path: Path | None,
    source_metadata: dict[str, Any],
    scheduled_at: datetime,
    caption: str,
    hashtags: list[str],
    title_override: str | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    title = title_override or clip_title(channel, clip_dir, notes)
    source_url = source_metadata_value(source_metadata, "webpage_url", "original_url", "url")
    manifest = {
        "source_type": "scheduled_reel",
        "channel_id": channel.id,
        "channel": channel_manifest(channel),
        "account_name": channel.account_name,
        "clip_id": clip_dir.name,
        "clip_dir": str(clip_dir),
        "notes_path": str(notes_path) if notes_path else "",
        "source_url": source_url,
        "source_title": source_metadata_value(source_metadata, "title"),
        "source_uploader": source_metadata_value(source_metadata, "uploader", "channel"),
        "scheduled_at": scheduled_at.replace(microsecond=0).isoformat(),
        "topic": title,
        "description": title,
        "hashtags": hashtags,
        "instagram_caption": caption,
        "slides": [
            {
                "index": 1,
                "type": "video",
                "path": str(media_path.resolve()),
                "source_url": source_url,
            }
        ],
    }
    if content_hash:
        manifest["reel_ledger"] = {
            "content_hash": content_hash,
            "channel_id": channel.id,
        }
    return manifest


def default_plan_dir(channel_id: str, now: datetime) -> Path:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUT / f"{channel_id}_{stamp}"


def create_schedule(
    *,
    clips_dir: Path,
    channel: Channel,
    start_at: datetime,
    interval_hours: float,
    timezone_name: str,
    media_filename: str,
    out_dir: Path,
    limit: int | None = None,
    created_at: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    if interval_hours <= 0:
        raise SystemExit("--interval-hours must be greater than zero")
    clips = discover_clips(clips_dir, media_filename)
    source_metadata = load_source_metadata(clips_dir)
    source_url = source_metadata_value(source_metadata, "webpage_url", "original_url", "url")
    if limit is not None:
        if limit <= 0:
            raise SystemExit("--limit must be greater than zero")
        clips = clips[:limit]

    out_dir = out_dir.expanduser().resolve()
    schedule_path = out_dir / "schedule.json"
    if schedule_path.exists():
        raise SystemExit(f"Schedule already exists: {schedule_path}")

    jobs: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for offset, (clip_dir, notes, notes_path) in enumerate(clips):
        base_id = safe_job_id(clip_dir.name)
        job_id = base_id
        suffix = 2
        while job_id in used_ids:
            job_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(job_id)

        scheduled_at = start_at + timedelta(hours=interval_hours * offset)
        media_path = clip_dir / media_filename
        caption, hashtags = build_caption(channel, clip_dir, notes, source_url=source_url)
        job_dir = out_dir / "jobs" / job_id
        manifest_path = job_dir / "manifest.json"
        caption_path = job_dir / "caption.txt"
        report_path = job_dir / "instagram_publish.json"
        caption_path.parent.mkdir(parents=True, exist_ok=True)
        caption_path.write_text(caption + "\n", encoding="utf-8")
        write_json(
            manifest_path,
            make_manifest(
                channel=channel,
                clip_dir=clip_dir,
                media_path=media_path,
                notes=notes,
                notes_path=notes_path,
                source_metadata=source_metadata,
                scheduled_at=scheduled_at,
                caption=caption,
                hashtags=hashtags,
            ),
        )
        jobs.append(
            {
                "id": job_id,
                "status": "scheduled",
                "scheduled_at": scheduled_at.replace(microsecond=0).isoformat(),
                "clip_dir": str(clip_dir),
                "media_path": str(media_path.resolve()),
                "notes_path": str(notes_path) if notes_path else "",
                "manifest_path": str(manifest_path),
                "caption_path": str(caption_path),
                "publish_report_path": str(report_path),
            }
        )

    schedule = {
        "version": SCHEDULE_VERSION,
        "created_at": created_at or utc_now(),
        "channel_id": channel.id,
        "platform": "instagram",
        "clips_dir": str(clips_dir.expanduser().resolve()),
        "source_url": source_url,
        "timezone": timezone_name,
        "media_filename": media_filename,
        "interval_hours": interval_hours,
        "job_count": len(jobs),
        "jobs": jobs,
    }
    write_json(schedule_path, schedule)
    return schedule_path, schedule


def report_permalink(report_path: Path) -> str:
    if not report_path.exists():
        return ""
    report = read_json(report_path)
    if not isinstance(report, dict):
        return ""
    result = report.get("result") if isinstance(report.get("result"), dict) else {}
    permalink = result.get("permalink") if isinstance(result.get("permalink"), dict) else {}
    return str(permalink.get("permalink") or "")


def report_publish_identity(report_path: Path) -> tuple[str, str]:
    """(media_id, permalink) from a publish report; ("", "") when unavailable.

    The published Instagram media id is the key a later ``sync-insights`` needs.
    Dry-run reports carry an empty ``result``, so both come back blank.
    """
    if not report_path.exists():
        return "", ""
    report = read_json(report_path)
    if not isinstance(report, dict):
        return "", ""
    result = report.get("result") if isinstance(report.get("result"), dict) else {}
    published = result.get("published") if isinstance(result.get("published"), dict) else {}
    permalink = result.get("permalink") if isinstance(result.get("permalink"), dict) else {}
    return str(published.get("id") or ""), str(permalink.get("permalink") or "")


def content_hash_for_media(channel_id: str, media_path: Path) -> str:
    if media_path.is_file():
        return reel_ledger.hash_file(media_path)
    return reel_ledger.hash_text(f"{channel_id}:{media_path}")


def manifest_title(manifest_path: Path) -> str:
    if not manifest_path.exists():
        return ""
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        return ""
    for key in ("topic", "description"):
        value = re.sub(r"\s+", " ", str(data.get(key) or "")).strip()
        if value:
            return value
    caption = str(data.get("instagram_caption") or "").strip()
    if caption:
        return re.sub(r"\s+", " ", caption.splitlines()[0]).strip()
    return ""


def upsert_schedule_job_in_ledger(
    conn: Any,
    *,
    schedule: dict[str, Any],
    schedule_path: Path,
    job: dict[str, Any],
) -> tuple[str, str]:
    channel_id = str(schedule.get("channel_id") or job.get("channel_id") or "")
    media_path = Path(str(job.get("media_path") or ""))
    content_hash = str(job.get("content_hash") or "") or content_hash_for_media(channel_id, media_path)
    parsed = parse_channel_media(media_path.name)
    report_media_id, report_link = report_publish_identity(
        Path(str(job.get("publish_report_path") or ""))
    )
    clips_dir = str(schedule.get("clips_dir") or "")
    source_video = Path(clips_dir).parent.name if clips_dir else schedule_path.parent.name
    title = (
        str(job.get("topic") or job.get("description") or "").strip()
        or manifest_title(Path(str(job.get("manifest_path") or "")))
        or None
    )
    reel_ledger.upsert_imported(
        conn,
        content_hash=content_hash,
        channel_id=channel_id,
        lang=parsed[0] if parsed else None,
        clip_dir=str(job.get("clip_dir") or media_path.parent),
        media_path=media_path,
        source_video=source_video,
        title=title,
        status=LEGACY_STATUS_MAP.get(str(job.get("status")), reel_ledger.STATUS_NEW),
        scheduled_at=str(job.get("scheduled_at") or "") or None,
        published_at=str(job.get("published_at") or "") or None,
        media_id=report_media_id or None,
        permalink=str(job.get("permalink") or "") or report_link or None,
        manifest_path=str(job.get("manifest_path") or "") or None,
    )
    job["content_hash"] = content_hash
    job["channel_id"] = channel_id
    return content_hash, channel_id


def publisher_command(
    job: dict[str, Any],
    *,
    channel_id: str,
    schedule_id: str,
    dry_run: bool,
    upload_r2: bool,
    media_base_url: str,
    r2_bucket: str,
    r2_public_base_url: str,
    platform: str = "instagram",
    tiktok_mode: str = "inbox",
    tiktok_source: str = "file",
    tiktok_privacy: str = "SELF_ONLY",
) -> list[str]:
    config = platform_config(platform)
    command = [sys.executable, str(ROOT / str(config["publisher"])), str(job["manifest_path"])]
    if platform == "tiktok":
        command.extend(
            [
                "--mode", tiktok_mode,
                "--source", tiktok_source,
                "--privacy-level", tiktok_privacy,
                "--out", str(job["publish_report_path"]),
            ]
        )
    else:
        command.extend(
            ["--single-video-media-type", "REELS", "--out", str(job["publish_report_path"])]
        )
    if dry_run:
        command.append("--dry-run")
    if upload_r2:
        command.extend(
            [
                "--upload-r2",
                "--r2-key-prefix",
                f"reels/{safe_job_id(channel_id)}/{safe_job_id(schedule_id)}/{safe_job_id(str(job['id']))}",
            ]
        )
    # tiktok_publish.py has no --media-base-url; it only pulls from R2 it uploaded.
    if media_base_url and platform != "tiktok":
        command.extend(["--media-base-url", media_base_url])
    if r2_bucket:
        command.extend(["--r2-bucket", r2_bucket])
    if r2_public_base_url:
        command.extend(["--r2-public-base-url", r2_public_base_url])
    return command


def run_due_jobs(
    schedule_path: Path,
    *,
    now: datetime,
    dry_run: bool,
    include_future: bool,
    retry_failed: bool,
    limit: int | None,
    upload_r2: bool,
    media_base_url: str,
    r2_bucket: str,
    r2_public_base_url: str,
    db_path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    schedule_path = schedule_path.expanduser().resolve()
    schedule = read_json(schedule_path)
    if not isinstance(schedule, dict) or not isinstance(schedule.get("jobs"), list):
        raise SystemExit(f"Invalid reel schedule: {schedule_path}")
    timezone_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    local_now = now.astimezone(timezone_for(timezone_name))
    allowed_statuses = {"scheduled", "publish_previewed"}
    if retry_failed:
        allowed_statuses.add("publish_failed")
    processed = 0
    failures = 0
    schedule_id = schedule_path.parent.name

    for job in schedule["jobs"]:
        if not isinstance(job, dict) or str(job.get("status")) not in allowed_statuses:
            continue
        scheduled_at = parse_datetime(str(job.get("scheduled_at") or ""), timezone_name)
        if not include_future and scheduled_at > local_now:
            continue
        if limit is not None and processed >= limit:
            break

        command = publisher_command(
            job,
            channel_id=str(schedule.get("channel_id") or "channel"),
            schedule_id=schedule_id,
            dry_run=dry_run,
            upload_r2=upload_r2,
            media_base_url=media_base_url,
            r2_bucket=r2_bucket,
            r2_public_base_url=r2_public_base_url,
        )
        job["status"] = "publishing"
        job["publish_started_at"] = utc_now()
        job.pop("failure", None)
        write_json(schedule_path, schedule)
        ledger_identity: tuple[str, str] | None = None
        if db_path is not None:
            with reel_ledger.connect(db_path) as conn:
                ledger_identity = upsert_schedule_job_in_ledger(
                    conn,
                    schedule=schedule,
                    schedule_path=schedule_path,
                    job=job,
                )
                reel_ledger.set_status(
                    conn,
                    ledger_identity[0],
                    ledger_identity[1],
                    reel_ledger.STATUS_PUBLISHING,
                    last_error=None,
                )
        print(f"[reel-scheduler] {'previewing' if dry_run else 'publishing'} {job.get('id')}")
        result = subprocess.run(command, check=False)
        processed += 1
        job["publish_finished_at"] = utc_now()
        job["publish_returncode"] = result.returncode
        if result.returncode == 0:
            if dry_run:
                job["status"] = "publish_previewed"
                job["previewed_at"] = utc_now()
            else:
                job["status"] = "published"
                job["published_at"] = utc_now()
                media_id, permalink = report_publish_identity(Path(str(job["publish_report_path"])))
                if media_id:
                    job["media_id"] = media_id
                if permalink:
                    job["permalink"] = permalink
        else:
            failures += 1
            job["status"] = "publish_failed"
            job["failure"] = f"instagram_publish.py exited {result.returncode}"
        if ledger_identity is not None and db_path is not None:
            with reel_ledger.connect(db_path) as conn:
                content_hash, channel_id = ledger_identity
                if result.returncode == 0 and dry_run:
                    reel_ledger.set_status(
                        conn,
                        content_hash,
                        channel_id,
                        reel_ledger.STATUS_PREVIEWED,
                        last_error=None,
                    )
                elif result.returncode == 0:
                    media_id = str(job.get("media_id") or "")
                    permalink = str(job.get("permalink") or "")
                    reel_ledger.set_status(
                        conn,
                        content_hash,
                        channel_id,
                        reel_ledger.STATUS_PUBLISHED,
                        published_at=str(job.get("published_at") or utc_now()),
                        media_id=media_id or None,
                        permalink=permalink or None,
                        last_error=None,
                    )
                else:
                    reel_ledger.set_status(
                        conn,
                        content_hash,
                        channel_id,
                        reel_ledger.STATUS_FAILED,
                        last_error=str(job.get("failure") or "publish failed"),
                    )
        write_json(schedule_path, schedule)

    schedule["last_run_at"] = utc_now()
    schedule["last_run_dry_run"] = dry_run
    schedule["last_run_processed"] = processed
    schedule["last_run_failures"] = failures
    write_json(schedule_path, schedule)
    print(f"[reel-scheduler] processed={processed} failures={failures}")
    return (1 if failures else 0), schedule


def ledger_job_id(row: Any) -> str:
    source = str(row["source_video"] or Path(str(row["clip_dir"])).name)
    return safe_job_id(f"{source}-{row['channel_id']}-{str(row['content_hash'])[:12]}")


def ledger_report_path(row: Any, platform: str = "instagram") -> Path:
    manifest_path = Path(str(row["manifest_path"] or ""))
    return manifest_path.with_name(str(platform_config(platform)["report_name"]))


def write_ledger_manifest(
    *,
    row: Any,
    channel: Channel,
    scheduled_at: datetime,
    out_dir: Path,
    settings_key: str = "instagram_reels",
) -> tuple[Path, str, str]:
    clip_dir = Path(str(row["clip_dir"]))
    media_path = Path(str(row["media_path"]))
    notes, notes_path = load_notes(clip_dir)
    source_metadata = load_source_metadata(clip_dir.parent)
    source_url = source_metadata_value(source_metadata, "webpage_url", "original_url", "url")
    lang = str(row["lang"] or "")
    title = str(row["title"] or "").strip() or routed_title(lang, notes, load_one_liners(clip_dir))
    caption, hashtags = build_caption(
        channel,
        clip_dir,
        notes,
        source_url=source_url,
        title_override=title,
        settings_key=settings_key,
    )
    stamp = scheduled_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job_dir = out_dir.expanduser().resolve() / channel.id / f"{stamp}_{ledger_job_id(row)}"
    manifest_path = job_dir / "manifest.json"
    caption_path = job_dir / "caption.txt"
    caption_path.parent.mkdir(parents=True, exist_ok=True)
    caption_path.write_text(caption + "\n", encoding="utf-8")
    write_json(
        manifest_path,
        make_manifest(
            channel=channel,
            clip_dir=clip_dir,
            media_path=media_path,
            notes=notes,
            notes_path=notes_path,
            source_metadata=source_metadata,
            scheduled_at=scheduled_at,
            caption=caption,
            hashtags=hashtags,
            title_override=title,
            content_hash=str(row["content_hash"]),
        ),
    )
    return manifest_path, caption, title


def round_robin_sources(rows: list[Any]) -> list[Any]:
    """Interleave new rows by source_video while preserving clip order per source."""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        source = str(row["source_video"] or "")
        grouped.setdefault(source, []).append(row)
    ordered_sources = sorted(grouped)
    interleaved: list[Any] = []
    while any(grouped[source] for source in ordered_sources):
        for source in ordered_sources:
            if grouped[source]:
                interleaved.append(grouped[source].pop(0))
    return interleaved


def row_source_video(row: Any) -> str:
    return str(row_value(row, "source_video") or "")


def row_chronological_key(row: Any, timezone_name: str = DEFAULT_TIMEZONE) -> tuple[int, float]:
    scheduled = parse_row_datetime(row_value(row, "scheduled_at"), timezone_name)
    if scheduled is None:
        return (1, 0.0)
    return (0, scheduled.astimezone(timezone.utc).timestamp())


def row_is_after(row: Any, boundary: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> bool:
    scheduled = parse_row_datetime(row_value(row, "scheduled_at"), timezone_name)
    return scheduled is not None and scheduled.astimezone(timezone.utc) > boundary.astimezone(timezone.utc)


def row_is_at_or_before(row: Any, boundary: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> bool:
    scheduled = parse_row_datetime(row_value(row, "scheduled_at"), timezone_name)
    return scheduled is not None and scheduled.astimezone(timezone.utc) <= boundary.astimezone(timezone.utc)


def source_order_from(rows: list[Any]) -> list[str]:
    ordered: list[str] = []
    for row in rows:
        source = row_source_video(row)
        if source and source not in ordered:
            ordered.append(source)
    return ordered


def next_source_after(source_order: list[str], last_source: str | None) -> str:
    if not source_order:
        return ""
    if last_source in source_order:
        return source_order[(source_order.index(str(last_source)) + 1) % len(source_order)]
    return source_order[0]


def pop_alternating_row(
    pools: dict[str, dict[str, list[Any]]],
    *,
    channel_id: str,
    desired_source: str,
    source_order: list[str],
    last_source: str | None,
) -> Any:
    channel_pools = pools[channel_id]
    if desired_source and channel_pools.get(desired_source):
        return channel_pools[desired_source].pop(0)
    for source in source_order:
        if source != last_source and channel_pools.get(source):
            return channel_pools[source].pop(0)
    for source in source_order:
        if channel_pools.get(source):
            return channel_pools[source].pop(0)
    for bucket in channel_pools.values():
        if bucket:
            return bucket.pop(0)
    raise RuntimeError(f"No queued rows left for channel {channel_id}")


def update_manifest_scheduled_at(manifest_path: Path, scheduled_at: datetime) -> None:
    if not manifest_path.is_file():
        return
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        return
    data["scheduled_at"] = scheduled_at.replace(microsecond=0).isoformat()
    write_json(manifest_path, data)


def mark_manifest_unscheduled(manifest_path: Path) -> None:
    if not manifest_path.is_file():
        return
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        return
    data.pop("scheduled_at", None)
    data["schedule_status"] = reel_ledger.STATUS_SKIPPED
    write_json(manifest_path, data)


def unschedule_queued_reel(
    *,
    db_path: Path,
    content_hash: str,
    channel_id: str,
    reason: str = "removed from queue UI",
) -> tuple[bool, str]:
    content_hash = content_hash.strip()
    channel_id = channel_id.strip()
    if not content_hash or not channel_id:
        return False, "Missing reel identity"
    queued_statuses = [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED]
    with reel_ledger.connect(db_path) as conn:
        row = reel_ledger.get_reel(conn, content_hash, channel_id)
        if row is None:
            return False, "That reel is no longer in the ledger"
        if str(row["status"]) not in queued_statuses:
            return False, f"Cannot remove a reel with status '{row['status']}'"
        cursor = conn.execute(
            "UPDATE reels SET status=?, scheduled_at=NULL, last_error=?, updated_at=? "
            "WHERE content_hash=? AND channel_id=? AND status IN (?, ?)",
            (
                reel_ledger.STATUS_SKIPPED,
                reason,
                utc_now(),
                content_hash,
                channel_id,
                reel_ledger.STATUS_SCHEDULED,
                reel_ledger.STATUS_PREVIEWED,
            ),
        )
        if cursor.rowcount != 1:
            return False, "That reel was already claimed or changed"
        mark_manifest_unscheduled(Path(str(row["manifest_path"] or "")))
        return True, f"Removed '{row['title'] or row['clip_dir']}' from the schedule"


def refill_queue_from_now(
    *,
    db_path: Path,
    channel_filter: str | None,
    settings_key: str,
    jitter_minutes: int | None = None,
) -> tuple[dict[str, int], int, str]:
    start_at = datetime.now(timezone_for(DEFAULT_TIMEZONE)).replace(microsecond=0).isoformat()
    reflowed = reflow_queue_rows(
        db_path=db_path,
        channel_filter=channel_filter,
        start_at_text=start_at,
        jitter_minutes=jitter_minutes,
        settings_key=settings_key,
        apply=True,
        include_start_at_slot=False,
    )
    alternated = alternate_source_queue_rows(
        db_path=db_path,
        after_text=start_at,
        channel_filter=channel_filter,
        apply=True,
    )
    return reflowed, alternated, start_at


def queue_row_query(row: Any) -> str:
    return urlencode(
        {
            "content_hash": str(row["content_hash"]),
            "channel_id": str(row["channel_id"]),
        }
    )


def render_queue_ui_html(
    *,
    rows: list[Any],
    counts: dict[str, dict[str, int]],
    db_path: Path,
    message: str = "",
    error: str = "",
) -> str:
    order = [
        reel_ledger.STATUS_SCHEDULED,
        reel_ledger.STATUS_PREVIEWED,
        reel_ledger.STATUS_PUBLISHING,
        reel_ledger.STATUS_PUBLISHED,
        reel_ledger.STATUS_SKIPPED,
        reel_ledger.STATUS_FAILED,
    ]
    count_rows = []
    for channel_id in sorted(counts):
        per = counts[channel_id]
        total = sum(per.values())
        chips = "".join(
            f"<span>{html.escape(name)} {int(per[name])}</span>"
            for name in order
            if per.get(name)
        )
        count_rows.append(
            f"<div class=\"count-row\"><strong>{html.escape(channel_id)}</strong>"
            f"<span>total {total}</span>{chips}</div>"
        )
    queue_rows = []
    for row in rows:
        query = queue_row_query(row)
        title = str(row["title"] or row["clip_dir"] or "")
        media_path = Path(str(row["media_path"] or ""))
        media_cell = (
            f"<video src=\"/media?{query}\" preload=\"metadata\" controls></video>"
            if media_path.is_file()
            else "<span class=\"missing\">missing media</span>"
        )
        queue_rows.append(
            "<tr>"
            f"<td class=\"media\">{media_cell}</td>"
            f"<td><div class=\"when\">{html.escape(str(row['scheduled_at'] or ''))}</div>"
            f"<div class=\"muted\">{html.escape(str(row['channel_id'] or ''))}</div></td>"
            f"<td><div class=\"title\">{html.escape(title)}</div>"
            f"<div class=\"muted\">{html.escape(str(row['source_video'] or ''))}</div></td>"
            f"<td><span class=\"status\">{html.escape(str(row['status'] or ''))}</span></td>"
            "<td>"
            "<form method=\"post\" action=\"/unschedule\" "
            "onsubmit=\"return confirm('Remove this post from the schedule?');\">"
            f"<input type=\"hidden\" name=\"content_hash\" value=\"{html.escape(str(row['content_hash']))}\">"
            f"<input type=\"hidden\" name=\"channel_id\" value=\"{html.escape(str(row['channel_id']))}\">"
            "<button type=\"submit\">Remove</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    body = "".join(queue_rows) or '<tr><td colspan="5" class="empty">No queued reels</td></tr>'
    message_html = f"<div class=\"notice ok\">{html.escape(message)}</div>" if message else ""
    error_html = f"<div class=\"notice error\">{html.escape(error)}</div>" if error else ""
    count_html = "".join(count_rows) or "<div class=\"count-row\">No ledger rows</div>"
    refill_form = (
        "<form method=\"post\" action=\"/refill\" "
        "onsubmit=\"return confirm('Refill empty slots and reshuffle the unpublished queue from now?');\">"
        "<button class=\"refill\" type=\"submit\">Refill Queue</button>"
        "</form>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reel Queue</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f6f1;
      --fg: #171717;
      --muted: #687076;
      --line: #d8d3c7;
      --surface: #ffffff;
      --accent: #176b57;
      --danger: #a93527;
      --danger-hover: #85271f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--fg);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: rgba(247, 246, 241, 0.94);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(10px);
    }}
    .wrap {{ width: min(1280px, calc(100vw - 32px)); margin: 0 auto; }}
    .bar {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: center;
      padding: 14px 0;
    }}
    h1 {{ margin: 0; font-size: 20px; letter-spacing: 0; }}
    .db {{ color: var(--muted); font-size: 12px; overflow-wrap: anywhere; text-align: right; }}
    .header-actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .counts {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 14px 0 4px;
    }}
    .count-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
    }}
    .count-row span, .status {{
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .notice {{
      margin: 12px 0 0;
      border-radius: 6px;
      padding: 10px 12px;
      background: var(--surface);
      border: 1px solid var(--line);
    }}
    .notice.ok {{ border-color: rgba(23, 107, 87, 0.45); color: var(--accent); }}
    .notice.error {{ border-color: rgba(169, 53, 39, 0.45); color: var(--danger); }}
    main {{ padding: 18px 0 32px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--surface);
      border: 1px solid var(--line);
    }}
    th, td {{
      padding: 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: middle;
      text-align: left;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0;
      background: #fbfaf7;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    video {{
      width: 150px;
      aspect-ratio: 9 / 16;
      display: block;
      background: #101010;
      border-radius: 4px;
    }}
    .media {{ width: 170px; }}
    .when {{ font-weight: 650; white-space: nowrap; }}
    .title {{ max-width: 560px; font-weight: 650; }}
    .muted {{ color: var(--muted); font-size: 12px; margin-top: 3px; overflow-wrap: anywhere; }}
    .missing, .empty {{ color: var(--muted); }}
    button {{
      border: 0;
      border-radius: 6px;
      background: var(--danger);
      color: white;
      padding: 8px 11px;
      font-weight: 700;
      cursor: pointer;
      min-width: 76px;
    }}
    button:hover {{ background: var(--danger-hover); }}
    button.refill {{ background: var(--accent); }}
    button.refill:hover {{ background: #125543; }}
    @media (max-width: 760px) {{
      .bar {{ align-items: flex-start; flex-direction: column; }}
      .db {{ text-align: left; }}
      .header-actions {{ justify-content: flex-start; }}
      table, thead, tbody, tr, th, td {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--line); padding: 10px; }}
      td {{ border: 0; padding: 6px 0; }}
      .media {{ width: auto; }}
      video {{ width: min(180px, 100%); }}
      .when {{ white-space: normal; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="bar">
        <h1>Reel Queue</h1>
        <div class="header-actions">
          {refill_form}
          <div class="db">{html.escape(str(db_path))}</div>
        </div>
      </div>
    </div>
  </header>
  <main class="wrap">
    {message_html}
    {error_html}
    <section class="counts">{count_html}</section>
    <table>
      <thead>
        <tr><th>Preview</th><th>Scheduled</th><th>Post</th><th>Status</th><th>Action</th></tr>
      </thead>
      <tbody>{body}</tbody>
    </table>
  </main>
</body>
</html>"""


def _first_query_value(values: dict[str, list[str]], key: str) -> str:
    return values.get(key, [""])[0]


def stream_http_body(output: Any, handle: Any, length: int, *, chunk_size: int = 64 * 1024) -> bool:
    remaining = length
    try:
        while remaining:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            output.write(chunk)
            remaining -= len(chunk)
    except CLIENT_DISCONNECT_ERRORS:
        return False
    return True


def make_queue_ui_handler(
    *,
    db_path: Path,
    channel_filter: str | None,
    limit: int | None,
    settings_key: str,
    platform: str,
    report_out: Path,
) -> type[BaseHTTPRequestHandler]:
    class QueueUIHandler(BaseHTTPRequestHandler):
        server_version = "ReelQueueUI/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_bytes(
            self,
            payload: bytes,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            content_type: str = "text/html; charset=utf-8",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def send_html(self, text: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_bytes(text.encode("utf-8"), status=status)

        def redirect_home(self, **params: str) -> None:
            location = "/" + (("?" + urlencode(params)) if params else "")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                params = parse_qs(parsed.query)
                with reel_ledger.connect(db_path) as conn:
                    counts = reel_ledger.status_counts(conn, channel_filter)
                    rows = reel_ledger.upcoming(conn, channel_filter, limit=limit)
                self.send_html(
                    render_queue_ui_html(
                        rows=rows,
                        counts=counts,
                        db_path=db_path,
                        message=_first_query_value(params, "message"),
                        error=_first_query_value(params, "error"),
                    )
                )
                return
            if parsed.path == "/media":
                try:
                    self.serve_media(parse_qs(parsed.query))
                except CLIENT_DISCONNECT_ERRORS:
                    return
                return
            self.send_html("Not found", status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8")
            params = parse_qs(body)
            if parsed.path == "/unschedule":
                ok, message = unschedule_queued_reel(
                    db_path=db_path,
                    content_hash=_first_query_value(params, "content_hash"),
                    channel_id=_first_query_value(params, "channel_id"),
                )
                if ok:
                    self.redirect_home(message=message)
                else:
                    self.redirect_home(error=message)
                return
            if parsed.path == "/refill":
                reflowed, alternated, start_at = refill_queue_from_now(
                    db_path=db_path,
                    channel_filter=channel_filter,
                    settings_key=settings_key,
                )
                report_command(
                    argparse.Namespace(
                        db=db_path,
                        platform=platform,
                        channel=channel_filter,
                        limit=0,
                        out=report_out,
                    )
                )
                moved = sum(reflowed.values())
                self.redirect_home(
                    message=(
                        f"Refilled from {start_at}: moved {moved} queued rows, "
                        f"re-alternated {alternated} rows"
                    )
                )
                return
            self.send_html("Not found", status=HTTPStatus.NOT_FOUND)

        def serve_media(self, params: dict[str, list[str]]) -> None:
            content_hash = _first_query_value(params, "content_hash")
            channel_id = _first_query_value(params, "channel_id")
            with reel_ledger.connect(db_path) as conn:
                row = reel_ledger.get_reel(conn, content_hash, channel_id)
            if row is None:
                self.send_html("Media not found", status=HTTPStatus.NOT_FOUND)
                return
            path = Path(str(row["media_path"] or ""))
            if not path.is_file():
                self.send_html("Media file missing", status=HTTPStatus.NOT_FOUND)
                return
            file_size = path.stat().st_size
            start = 0
            end = file_size - 1
            status = HTTPStatus.OK
            range_header = self.headers.get("Range", "")
            if range_header.startswith("bytes="):
                status = HTTPStatus.PARTIAL_CONTENT
                value = range_header.removeprefix("bytes=").split(",", 1)[0]
                raw_start, _, raw_end = value.partition("-")
                if raw_start:
                    start = int(raw_start)
                if raw_end:
                    end = min(int(raw_end), end)
            if start < 0 or start >= file_size or end < start:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.end_headers()
            with path.open("rb") as handle:
                handle.seek(start)
                stream_http_body(self.wfile, handle, length)

    return QueueUIHandler


def plan_ledger_rows(
    *,
    db_path: Path,
    clips_dir: Path,
    out_dir: Path,
    channel_filter: str | None,
    start_at_text: str | None,
    limit_per_channel: int | None,
    jitter_minutes: int | None,
    scan_first: bool,
    settings_key: str = "instagram_reels",
) -> dict[str, int]:
    if scan_first:
        scan_command(argparse.Namespace(clips_dir=clips_dir, db=db_path))
    planned: dict[str, int] = {}
    with reel_ledger.connect(db_path) as conn:
        rows = reel_ledger.new_reels(conn, channel_filter)
        by_channel: dict[str, list[Any]] = {}
        for row in rows:
            by_channel.setdefault(str(row["channel_id"]), []).append(row)
        for channel_id in sorted(by_channel):
            channel = load_channel(channel_id)
            settings = reel_settings(channel, settings_key)
            timezone_name = setting_text(settings, "timezone", DEFAULT_TIMEZONE)
            start_at = (
                parse_datetime(start_at_text, timezone_name)
                if start_at_text
                else datetime.now(timezone.utc)
            )
            channel_rows = round_robin_sources(by_channel[channel_id])
            if limit_per_channel is not None:
                channel_rows = channel_rows[:limit_per_channel]
            existing = reel_ledger.rows_with_schedule(conn, channel_id)
            slots = next_open_slots(
                channel=channel,
                start_at=start_at,
                existing_rows=existing,
                count=len(channel_rows),
                jitter_override=jitter_minutes,
                content_hashes=[str(row["content_hash"]) for row in channel_rows],
                settings_key=settings_key,
            )
            for row, scheduled_at in zip(channel_rows, slots):
                manifest_path, caption, title = write_ledger_manifest(
                    row=row,
                    channel=channel,
                    scheduled_at=scheduled_at,
                    out_dir=out_dir,
                    settings_key=settings_key,
                )
                reel_ledger.set_status(
                    conn,
                    str(row["content_hash"]),
                    channel_id,
                    reel_ledger.STATUS_SCHEDULED,
                    scheduled_at=scheduled_at.isoformat(),
                    manifest_path=str(manifest_path),
                    caption=caption,
                    title=title,
                    last_error=None,
                )
                planned[channel_id] = planned.get(channel_id, 0) + 1
    return planned


def reflow_queue_rows(
    *,
    db_path: Path,
    channel_filter: str | None,
    start_at_text: str | None,
    jitter_minutes: int | None,
    settings_key: str,
    apply: bool,
    include_start_at_slot: bool | None = None,
) -> dict[str, int]:
    """Reassign queued rows to slots without touching published history."""
    queue_statuses = [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED]
    placeholders = ",".join("?" for _ in queue_statuses)
    counts: dict[str, int] = {}
    previews: list[tuple[str, str, str, str, str]] = []
    with reel_ledger.connect(db_path) as conn:
        query = (
            "SELECT * FROM reels WHERE status IN (" + placeholders + ") "
            "AND scheduled_at IS NOT NULL"
            + (" AND channel_id=?" if channel_filter else "")
            + " ORDER BY channel_id, scheduled_at, source_video, clip_dir, lang, content_hash"
        )
        params: list[Any] = list(queue_statuses)
        if channel_filter:
            params.append(channel_filter)
        rows = conn.execute(query, params).fetchall()
        by_channel: dict[str, list[Any]] = {}
        for row in rows:
            by_channel.setdefault(str(row["channel_id"]), []).append(row)
        for channel_id in sorted(by_channel):
            channel = load_channel(channel_id)
            settings = reel_settings(channel, settings_key)
            timezone_name = setting_text(settings, "timezone", DEFAULT_TIMEZONE)
            start_at = (
                parse_datetime(start_at_text, timezone_name)
                if start_at_text
                else datetime.now(timezone.utc)
            )
            channel_rows = round_robin_sources(by_channel[channel_id])
            queue_hashes = {str(row["content_hash"]) for row in channel_rows}
            existing = [
                row
                for row in reel_ledger.rows_with_schedule(conn, channel_id)
                if str(row["content_hash"]) not in queue_hashes
            ]
            existing = reflow_blocking_rows(
                existing,
                start_at=start_at,
                timezone_name=timezone_name,
            )
            slots = next_open_slots(
                channel=channel,
                start_at=start_at,
                existing_rows=existing,
                count=len(channel_rows),
                jitter_override=jitter_minutes,
                content_hashes=[str(row["content_hash"]) for row in channel_rows],
                settings_key=settings_key,
                include_start_at=(
                    has_explicit_time(start_at_text)
                    if include_start_at_slot is None
                    else include_start_at_slot
                ),
            )
            for row, scheduled_at in zip(channel_rows, slots):
                old_at = str(row["scheduled_at"] or "")
                new_at = scheduled_at.isoformat()
                counts[channel_id] = counts.get(channel_id, 0) + 1
                previews.append(
                    (
                        channel_id,
                        old_at,
                        new_at,
                        str(row["status"] or ""),
                        str(row["title"] or row["clip_dir"] or "")[:80],
                    )
                )
                if not apply:
                    continue
                conn.execute(
                    "UPDATE reels SET scheduled_at=?, updated_at=? "
                    "WHERE content_hash=? AND channel_id=?",
                    (new_at, utc_now(), row["content_hash"], channel_id),
                )
                update_manifest_scheduled_at(Path(str(row["manifest_path"] or "")), scheduled_at)
    for channel_id, old_at, new_at, status, title in previews[:20]:
        verb = "move" if old_at != new_at else "keep"
        print(f"[reel-scheduler] {verb} {channel_id:<14} {status:<16} {old_at} -> {new_at}  {title}")
    if len(previews) > 20:
        print(f"[reel-scheduler] ... {len(previews) - 20} more queued rows")
    action = "reflowed" if apply else "would reflow"
    for channel_id in sorted(counts):
        print(f"[reel-scheduler] {action} {counts[channel_id]} queued {channel_id} row(s)")
    if not counts:
        print("[reel-scheduler] no queued rows to reflow")
    if not apply:
        print("[reel-scheduler] dry run only; rerun with --apply to update the ledger")
    return counts


def alternate_source_queue_rows(
    *,
    db_path: Path,
    after_text: str | None,
    channel_filter: str | None,
    apply: bool,
) -> int:
    """Shuffle queued content into existing slots so source_video alternates globally."""
    boundary = (
        parse_datetime(after_text, DEFAULT_TIMEZONE)
        if after_text
        else datetime.now(timezone.utc)
    )
    queue_statuses = [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED]
    queued_placeholders = ",".join("?" for _ in queue_statuses)
    scheduled_statuses = [
        reel_ledger.STATUS_SCHEDULED,
        reel_ledger.STATUS_PREVIEWED,
        reel_ledger.STATUS_PUBLISHING,
        reel_ledger.STATUS_PUBLISHED,
    ]
    scheduled_placeholders = ",".join("?" for _ in scheduled_statuses)
    previews: list[tuple[str, str, str, str, str, str, str]] = []
    with reel_ledger.connect(db_path) as conn:
        queued_query = (
            "SELECT * FROM reels WHERE status IN (" + queued_placeholders + ") "
            "AND scheduled_at IS NOT NULL"
            + (" AND channel_id=?" if channel_filter else "")
        )
        queued_params: list[Any] = list(queue_statuses)
        if channel_filter:
            queued_params.append(channel_filter)
        queued_rows = [
            row
            for row in conn.execute(queued_query, queued_params).fetchall()
            if row_is_after(row, boundary)
        ]
        queued_rows.sort(key=row_chronological_key)
        if not queued_rows:
            print("[reel-scheduler] no queued rows after the requested boundary")
            return 0

        history_query = (
            "SELECT * FROM reels WHERE status IN (" + scheduled_placeholders + ") "
            "AND scheduled_at IS NOT NULL"
            + (" AND channel_id=?" if channel_filter else "")
        )
        history_params: list[Any] = list(scheduled_statuses)
        if channel_filter:
            history_params.append(channel_filter)
        history_rows = [
            row
            for row in conn.execute(history_query, history_params).fetchall()
            if row_is_at_or_before(row, boundary)
        ]
        history_rows.sort(key=row_chronological_key)
        last_source = row_source_video(history_rows[-1]) if history_rows else None
        source_order = source_order_from(queued_rows)
        if last_source and last_source not in source_order:
            source_order.append(last_source)
        if len(source_order) < 2:
            print("[reel-scheduler] fewer than two source videos remain in the queued rows")
            return 0

        pools: dict[str, dict[str, list[Any]]] = {}
        for row in queued_rows:
            channel_id = str(row["channel_id"])
            source = row_source_video(row)
            pools.setdefault(channel_id, {}).setdefault(source, []).append(row)

        assignments: list[tuple[Any, Any, str]] = []
        for slot in queued_rows:
            channel_id = str(slot["channel_id"])
            desired_source = next_source_after(source_order, last_source)
            selected = pop_alternating_row(
                pools,
                channel_id=channel_id,
                desired_source=desired_source,
                source_order=source_order,
                last_source=last_source,
            )
            assignments.append((slot, selected, desired_source))
            last_source = row_source_video(selected)

        for slot, selected, desired_source in assignments:
            channel_id = str(selected["channel_id"])
            old_at = str(selected["scheduled_at"] or "")
            new_at = str(slot["scheduled_at"] or "")
            previews.append(
                (
                    channel_id,
                    old_at,
                    new_at,
                    row_source_video(selected),
                    desired_source,
                    str(selected["status"] or ""),
                    str(selected["title"] or selected["clip_dir"] or "")[:70],
                )
            )
            if not apply:
                continue
            scheduled_at = parse_row_datetime(new_at, DEFAULT_TIMEZONE)
            if scheduled_at is None:
                continue
            conn.execute(
                "UPDATE reels SET scheduled_at=?, updated_at=? WHERE content_hash=? AND channel_id=?",
                (new_at, utc_now(), selected["content_hash"], channel_id),
            )
            update_manifest_scheduled_at(Path(str(selected["manifest_path"] or "")), scheduled_at)

    for channel_id, old_at, new_at, source, desired_source, status, title in previews[:24]:
        verb = "move" if old_at != new_at else "keep"
        desired_note = f", wanted {desired_source}" if desired_source and desired_source != source else ""
        print(
            f"[reel-scheduler] {verb} {channel_id:<14} {status:<16} "
            f"{source}{desired_note}: {old_at} -> {new_at}  {title}"
        )
    if len(previews) > 24:
        print(f"[reel-scheduler] ... {len(previews) - 24} more queued rows")
    action = "alternated" if apply else "would alternate"
    print(f"[reel-scheduler] {action} {len(previews)} queued row(s) after {boundary.isoformat()}")
    if not apply:
        print("[reel-scheduler] dry run only; rerun with --apply to update the ledger")
    return len(previews)


def resolve_tiktok_publish_opts(
    platform: str,
    channel_id: str,
    *,
    tiktok_mode: str | None,
    tiktok_source: str | None,
    tiktok_privacy: str | None,
) -> dict[str, str]:
    """Per-channel TikTok posting options to splat into ``publisher_command``.

    CLI flags win; otherwise read the channel's ``publishing.tiktok`` block;
    otherwise fall back to unaudited-friendly defaults (inbox/file/SELF_ONLY).
    Returns ``{}`` for non-tiktok platforms so it can be splatted unconditionally.
    """
    if platform != "tiktok":
        return {}
    try:
        settings = reel_settings(load_channel(channel_id), "tiktok")
    except Exception:
        settings = {}
    return {
        "tiktok_mode": tiktok_mode or str(settings.get("mode") or "inbox"),
        "tiktok_source": tiktok_source or str(settings.get("source") or "file"),
        "tiktok_privacy": tiktok_privacy or str(settings.get("privacy_level") or "SELF_ONLY"),
    }


def run_due_ledger(
    *,
    db_path: Path,
    now: datetime,
    channel_id: str | None,
    scheduled_date: date | None,
    dry_run: bool,
    include_future: bool,
    retry_failed: bool,
    limit: int | None,
    upload_r2: bool,
    media_base_url: str,
    r2_bucket: str,
    r2_public_base_url: str,
    platform: str = "instagram",
    tiktok_mode: str | None = None,
    tiktok_source: str | None = None,
    tiktok_privacy: str | None = None,
) -> int:
    with reel_ledger.connect(db_path) as conn:
        rows = reel_ledger.due_reels(
            conn,
            now=now,
            channel_id=channel_id,
            include_future=include_future,
            retry_failed=retry_failed,
            limit=limit,
        )
    if scheduled_date is not None:
        rows = [row for row in rows if row_scheduled_date(row) == scheduled_date]
        if limit is not None:
            rows = rows[:limit]
    processed = 0
    failures = 0
    for row in rows:
        content_hash = str(row["content_hash"])
        row_channel_id = str(row["channel_id"])
        job = {
            "id": ledger_job_id(row),
            "manifest_path": str(row["manifest_path"]),
            "publish_report_path": str(ledger_report_path(row, platform)),
        }
        publishable, reason = row_is_publishable(row)
        if not publishable:
            failures += 1
            with reel_ledger.connect(db_path) as conn:
                reel_ledger.set_status(
                    conn,
                    content_hash,
                    row_channel_id,
                    reel_ledger.STATUS_FAILED,
                    last_error=reason,
                )
            print(f"[reel-scheduler] skipped {job['id']}: {reason}")
            continue
        tiktok_opts = resolve_tiktok_publish_opts(
            platform,
            row_channel_id,
            tiktok_mode=tiktok_mode,
            tiktok_source=tiktok_source,
            tiktok_privacy=tiktok_privacy,
        )
        command = publisher_command(
            job,
            channel_id=row_channel_id,
            schedule_id="ledger",
            dry_run=dry_run,
            upload_r2=upload_r2,
            media_base_url=media_base_url,
            r2_bucket=r2_bucket,
            r2_public_base_url=r2_public_base_url,
            platform=platform,
            **tiktok_opts,
        )
        with reel_ledger.connect(db_path) as conn:
            claimed = reel_ledger.claim_for_publish(
                conn,
                content_hash,
                row_channel_id,
                retry_failed=retry_failed,
            )
        if not claimed:
            print(f"[reel-scheduler] skipped already-claimed {job['id']}")
            continue
        print(f"[reel-scheduler] {'previewing' if dry_run else 'publishing'} {job['id']}")
        result = subprocess.run(command, check=False)
        processed += 1
        if result.returncode == 0:
            with reel_ledger.connect(db_path) as conn:
                if dry_run:
                    reel_ledger.set_status(
                        conn,
                        content_hash,
                        row_channel_id,
                        reel_ledger.STATUS_PREVIEWED,
                        last_error=None,
                    )
                else:
                    media_id, permalink = report_publish_identity(ledger_report_path(row, platform))
                    reel_ledger.set_status(
                        conn,
                        content_hash,
                        row_channel_id,
                        reel_ledger.STATUS_PUBLISHED,
                        published_at=utc_now(),
                        media_id=media_id or None,
                        permalink=permalink or None,
                        last_error=None,
                    )
        else:
            failures += 1
            with reel_ledger.connect(db_path) as conn:
                reel_ledger.set_status(
                    conn,
                    content_hash,
                    row_channel_id,
                    reel_ledger.STATUS_FAILED,
                    last_error=f"instagram_publish.py exited {result.returncode}",
                )
    print(f"[reel-scheduler] processed={processed} failures={failures}")
    return 1 if failures else 0


def parse_insight_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    data = payload.get("data") if isinstance(payload.get("data"), list) else []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        values = item.get("values") if isinstance(item.get("values"), list) else []
        if not name or not values:
            continue
        latest = values[-1] if isinstance(values[-1], dict) else {}
        value = latest.get("value") if isinstance(latest, dict) else None
        if isinstance(value, (int, float)):
            metrics[name] = int(value)
    return metrics


def fetch_insights(
    *,
    media_id: str,
    metrics: list[str],
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> dict[str, Any]:
    import instagram_publish

    return instagram_publish.graph_request(
        f"{media_id}/insights",
        access_token=access_token,
        graph_version=graph_version,
        graph_api_root=graph_api_root,
        params={"metric": ",".join(metrics)},
        method="GET",
        timeout=30,
    )


def render_report_html(
    *,
    counts: dict[str, dict[str, int]],
    upcoming: list[Any],
    published: list[Any],
    insight_rows: list[Any],
    db_path: Path,
) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    order = [
        reel_ledger.STATUS_NEW,
        reel_ledger.STATUS_SCHEDULED,
        reel_ledger.STATUS_PUBLISHING,
        reel_ledger.STATUS_PREVIEWED,
        reel_ledger.STATUS_PUBLISHED,
        reel_ledger.STATUS_FAILED,
        reel_ledger.STATUS_SKIPPED,
    ]
    count_rows = []
    for channel_id in sorted(counts):
        cells = "".join(f"<td>{counts[channel_id].get(status, 0)}</td>" for status in order)
        count_rows.append(f"<tr><th>{esc(channel_id)}</th>{cells}</tr>")
    upcoming_rows = [
        "<tr>"
        f"<td>{esc(row['scheduled_at'])}</td>"
        f"<td>{esc(row['channel_id'])}</td>"
        f"<td>{esc(row['status'])}</td>"
        f"<td>{esc(row['title'] or row['clip_dir'])}</td>"
        "</tr>"
        for row in upcoming
    ]
    published_rows = [
        "<tr>"
        f"<td>{esc(row['published_at'])}</td>"
        f"<td>{esc(row['channel_id'])}</td>"
        f"<td><a href=\"{esc(row['permalink'])}\">{esc(row['permalink'] or row['media_id'])}</a></td>"
        "</tr>"
        for row in published
    ]
    insight_html = [
        "<tr>"
        f"<td>{esc(row['published_at'])}</td>"
        f"<td>{esc(row['channel_id'])}</td>"
        f"<td>{esc(row['title'])}</td>"
        f"<td>{esc(row['views'])}</td>"
        f"<td>{esc(row['reach'])}</td>"
        f"<td>{esc(row['saved'])}</td>"
        f"<td>{esc(row['total_interactions'])}</td>"
        f"<td>{esc(row['captured_at'])}</td>"
        "</tr>"
        for row in insight_rows
    ]
    status_headers = "".join(f"<th>{esc(status)}</th>" for status in order)
    count_body = "".join(count_rows) or '<tr><td colspan="8">No rows</td></tr>'
    upcoming_body = "".join(upcoming_rows) or '<tr><td colspan="4">No queued reels</td></tr>'
    published_body = "".join(published_rows) or '<tr><td colspan="3">No published reels</td></tr>'
    insight_body = "".join(insight_html) or '<tr><td colspan="8">No insight snapshots</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reel Ledger Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #151515; background: #fafafa; }}
    h1 {{ font-size: 28px; margin: 0 0 8px; }}
    h2 {{ font-size: 18px; margin: 32px 0 10px; }}
    .meta {{ color: #666; margin-bottom: 24px; }}
    table {{ border-collapse: collapse; width: 100%; background: white; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f0f0f0; }}
    a {{ color: #145cc7; }}
  </style>
</head>
<body>
  <h1>Reel Ledger Report</h1>
  <div class="meta">Generated {esc(utc_now())} from {esc(db_path)}</div>
  <h2>Status Counts</h2>
  <table>
    <thead><tr><th>Channel</th>{status_headers}</tr></thead>
    <tbody>{count_body}</tbody>
  </table>
  <h2>Upcoming Queue</h2>
  <table>
    <thead><tr><th>Scheduled</th><th>Channel</th><th>Status</th><th>Title</th></tr></thead>
    <tbody>{upcoming_body}</tbody>
  </table>
  <h2>Recently Published</h2>
  <table>
    <thead><tr><th>Published</th><th>Channel</th><th>Permalink</th></tr></thead>
    <tbody>{published_body}</tbody>
  </table>
  <h2>Latest Insights</h2>
  <table>
    <thead><tr><th>Published</th><th>Channel</th><th>Title</th><th>Views</th><th>Reach</th><th>Saved</th><th>Interactions</th><th>Captured</th></tr></thead>
    <tbody>{insight_body}</tbody>
  </table>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule reels from a clips folder and publish due jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Create a channel-aware reel upload schedule")
    plan.add_argument("clips_dir", type=Path, help="Folder containing one subfolder per clip")
    plan.add_argument("date", nargs="?", help="Optional first publish date (YYYY-MM-DD) or ISO datetime")
    plan.add_argument("--channel", default=os.environ.get("CAROUSEL_CHANNEL"), help="Publishing channel id")
    plan.add_argument("--start-at", help="First publish date/time; accepts YYYY-MM-DD or ISO 8601")
    plan.add_argument("--timezone", help="IANA timezone; defaults to channel publishing config")
    plan.add_argument("--interval-hours", type=float, help="Hours between reels; defaults to channel config")
    plan.add_argument("--media-filename", help="Filename selected inside each clip folder")
    plan.add_argument("--out-dir", type=Path, help="Schedule output directory")
    plan.add_argument("--limit", type=int, help="Schedule only the first N clips")

    run_due = subparsers.add_parser("run-due", help="Publish reels whose scheduled time has arrived")
    run_due.add_argument("schedule", nargs="?", type=Path, help="Optional legacy schedule.json path")
    run_due.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Publishing platform (default: instagram)"
    )
    run_due.add_argument("--tiktok-mode", choices=("inbox", "direct"), default=None, help="TikTok: inbox draft (default) or direct post")
    run_due.add_argument("--tiktok-source", choices=("file", "pull"), default=None, help="TikTok upload: file (default) or R2 pull")
    run_due.add_argument("--tiktok-privacy", default=None, help="TikTok direct-post privacy (default: SELF_ONLY)")
    run_due.add_argument("--now", help="Override the current time in ISO 8601 (useful for operations/tests)")
    run_due.add_argument("--channel", help="Ledger mode: limit to one channel id")
    run_due.add_argument("--date", help="Ledger mode: process rows scheduled on YYYY-MM-DD")
    run_due.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    run_due.add_argument("--dry-run", action="store_true", help="Preview through instagram_publish.py")
    run_due.add_argument("--all", action="store_true", help="Include future jobs")
    run_due.add_argument("--retry-failed", action="store_true", help="Retry jobs in publish_failed state")
    run_due.add_argument("--limit", type=int, help="Maximum jobs to process in this run")
    run_due.add_argument("--upload-r2", action="store_true", help="Upload each reel to Cloudflare R2")
    run_due.add_argument(
        "--media-base-url",
        default=os.environ.get("INSTAGRAM_MEDIA_BASE_URL", ""),
        help="Public HTTPS base URL for media already hosted elsewhere",
    )
    run_due.add_argument("--r2-bucket", default=os.environ.get("R2_BUCKET", ""))
    run_due.add_argument("--r2-public-base-url", default=os.environ.get("R2_PUBLIC_BASE_URL", ""))

    scan = subparsers.add_parser(
        "scan", help="Discover reel.<lang>.<channel>.mp4 variants into the ledger"
    )
    scan.add_argument("clips_dir", type=Path, help="reel-app clips folder (multi-channel)")
    scan.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Ledger to scan into (default: instagram)"
    )
    scan.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")

    plan_ledger = subparsers.add_parser(
        "plan-ledger", help="Scan multi-channel clips and assign new ledger rows to per-channel slots"
    )
    plan_ledger.add_argument("clips_dir", type=Path, help="reel-app clips folder (multi-channel)")
    plan_ledger.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Publishing platform (default: instagram)"
    )
    plan_ledger.add_argument("date", nargs="?", help="Optional first eligible date (YYYY-MM-DD) or ISO datetime")
    plan_ledger.add_argument("--channel", help="Limit planning to one channel id")
    plan_ledger.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    plan_ledger.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT / "ledger",
        help="Folder for generated per-reel manifests",
    )
    plan_ledger.add_argument("--start-at", help="First eligible date/time; accepts YYYY-MM-DD or ISO 8601")
    plan_ledger.add_argument("--limit-per-channel", type=int, help="Maximum new rows to schedule per channel")
    plan_ledger.add_argument("--jitter-minutes", type=int, help="Override channel jitter for this plan")
    plan_ledger.add_argument("--no-scan", action="store_true", help="Plan existing new ledger rows only")

    queue_outputs = subparsers.add_parser(
        "queue-outputs",
        help="Scan reel-app outputs, schedule new rows, and optionally reshuffle the queue",
    )
    queue_outputs.add_argument(
        "outputs_root",
        nargs="?",
        type=Path,
        default=DEFAULT_REEL_OUTPUTS,
        help=f"Folder containing <youtube_id>/clips dirs (default: {DEFAULT_REEL_OUTPUTS})",
    )
    queue_outputs.add_argument(
        "--mode",
        choices=("append", "reshuffle"),
        default="append",
        help="append new rows at the end, or reshuffle the unpublished queue after adding",
    )
    queue_outputs.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Publishing platform (default: instagram)"
    )
    queue_outputs.add_argument("--channel", help="Limit to one channel id")
    queue_outputs.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    queue_outputs.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT / "ledger",
        help="Folder for generated per-reel manifests",
    )
    queue_outputs.add_argument("--start-at", help="First eligible date/time for append or reflow")
    queue_outputs.add_argument("--after", help="Source alternation boundary for reshuffle (default: now)")
    queue_outputs.add_argument("--limit-per-channel", type=int, help="Maximum new rows to schedule per channel")
    queue_outputs.add_argument("--jitter-minutes", type=int, help="Override channel jitter")
    queue_outputs.add_argument("--report-out", type=Path, default=ROOT / "out" / "reel_report.html")
    queue_outputs.add_argument("--no-report", action="store_true", help="Do not refresh the HTML report")

    reflow = subparsers.add_parser(
        "reflow-queue",
        help="Reassign queued scheduled/previewed rows to current per-channel slots without touching published rows",
    )
    reflow.add_argument("date", nargs="?", help="First eligible date (YYYY-MM-DD) or ISO datetime")
    reflow.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Publishing platform (default: instagram)"
    )
    reflow.add_argument("--channel", help="Limit to one channel id")
    reflow.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    reflow.add_argument("--start-at", help="First eligible date/time; accepts YYYY-MM-DD or ISO 8601")
    reflow.add_argument("--jitter-minutes", type=int, help="Override channel jitter for this reflow")
    reflow.add_argument("--apply", action="store_true", help="Actually update queued rows")

    alternate_sources = subparsers.add_parser(
        "alternate-sources",
        help="Reorder queued rows after a timestamp so source videos alternate in the combined queue",
    )
    alternate_sources.add_argument("--after", help="Only reorder queued rows after this ISO datetime")
    alternate_sources.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Publishing platform (default: instagram)"
    )
    alternate_sources.add_argument("--channel", help="Limit to one channel id")
    alternate_sources.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    alternate_sources.add_argument("--apply", action="store_true", help="Actually update queued rows")

    importer = subparsers.add_parser(
        "import-schedules", help="Seed the ledger from existing schedule.json files"
    )
    importer.add_argument("--root", type=Path, default=DEFAULT_OUT, help="Folder of <schedule>/schedule.json dirs")
    importer.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")

    status = subparsers.add_parser(
        "status", help="Summarize ledger counts, upcoming posts, and recent publishes"
    )
    status.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to read (default: instagram)"
    )
    status.add_argument("--channel", help="Limit to one channel id")
    status.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    status.add_argument("--limit", type=int, default=15, help="Max upcoming rows to show")

    queue_ui = subparsers.add_parser(
        "queue-ui",
        help="Serve a local queue review UI with remove buttons for unpublished scheduled posts",
    )
    queue_ui.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to review (default: instagram)"
    )
    queue_ui.add_argument("--channel", help="Limit to one channel id")
    queue_ui.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    queue_ui.add_argument("--host", default="127.0.0.1", help="Host interface to bind")
    queue_ui.add_argument("--port", type=int, default=8765, help="Port to listen on")
    queue_ui.add_argument("--limit", type=int, default=200, help="Max queued rows to show")
    queue_ui.add_argument("--report-out", type=Path, default=ROOT / "out" / "reel_report.html")

    cleanup = subparsers.add_parser(
        "cleanup-missing", help="Dry-run/delete ledger rows whose media file no longer exists"
    )
    cleanup.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to clean (default: instagram)"
    )
    cleanup.add_argument("--channel", help="Limit to one channel id")
    cleanup.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    cleanup.add_argument("--all-statuses", action="store_true", help="Consider every status, not just failed rows")
    cleanup.add_argument("--apply", action="store_true", help="Actually delete missing-media rows")

    sync = subparsers.add_parser("sync-insights", help="Fetch insights for published ledger rows")
    sync.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="instagram (Graph) or tiktok (video.list)"
    )
    sync.add_argument("--channel", help="Limit to one channel id")
    sync.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    sync.add_argument("--limit", type=int, help="Maximum published rows to sync")
    sync.add_argument("--dry-run", action="store_true", help="Print rows that would be synced")
    sync.add_argument(
        "--metrics",
        default="views,reach,likes,comments,saved,shares,total_interactions",
        help="Comma-separated Instagram insight metrics",
    )
    sync.add_argument("--access-token", default="", help="Override Instagram access token")
    sync.add_argument("--graph-api-version", default="")
    sync.add_argument("--graph-api-root", default="")

    report = subparsers.add_parser("report", help="Render a self-contained HTML report from the ledger")
    report.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to render (default: instagram)"
    )
    report.add_argument("--channel", help="Limit to one channel id")
    report.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db, state/reels.db or state/tiktok.db)")
    report.add_argument("--out", type=Path, default=ROOT / "out" / "reel_report.html")
    report.add_argument("--limit", type=int, default=0, help="Rows per report table; 0 means all")
    return parser


def plan_command(args: argparse.Namespace) -> int:
    channel = load_channel(args.channel)
    settings = reel_settings(channel)
    timezone_name = args.timezone or setting_text(settings, "timezone", DEFAULT_TIMEZONE)
    interval_hours = (
        args.interval_hours
        if args.interval_hours is not None
        else float(settings.get("interval_hours") or DEFAULT_INTERVAL_HOURS)
    )
    media_filename = args.media_filename or setting_text(
        settings,
        "media_filename",
        DEFAULT_MEDIA_FILENAME,
    )
    now = datetime.now(timezone.utc)
    requested_start = scheduler_date_arg(args)
    start_at = (
        parse_datetime(
            requested_start,
            timezone_name,
            date_clock=setting_text(settings, "publish_time", DEFAULT_PUBLISH_TIME),
        )
        if requested_start
        else next_publish_time(
            now=now,
            timezone_name=timezone_name,
            clock=setting_text(settings, "publish_time", DEFAULT_PUBLISH_TIME),
        )
    )
    start_at = start_at.astimezone(timezone_for(timezone_name))
    out_dir = args.out_dir or default_plan_dir(channel.id, now)
    schedule_path, schedule = create_schedule(
        clips_dir=args.clips_dir,
        channel=channel,
        start_at=start_at,
        interval_hours=interval_hours,
        timezone_name=timezone_name,
        media_filename=media_filename,
        out_dir=out_dir,
        limit=args.limit,
    )
    print(f"[reel-scheduler] channel={channel.id} jobs={schedule['job_count']}")
    print(f"[reel-scheduler] wrote schedule -> {schedule_path}")
    return 0


def run_due_command(args: argparse.Namespace) -> int:
    platform = resolve_platform(args)
    settings_key = settings_key_for(platform)
    db_path = resolve_db(args)
    if args.schedule:
        if platform != "instagram":
            raise SystemExit("Legacy schedule.json publishing is Instagram-only; use the ledger path for tiktok")
        schedule_path = args.schedule.expanduser().resolve()
        schedule = read_json(schedule_path)
        if not isinstance(schedule, dict):
            raise SystemExit(f"Invalid reel schedule: {schedule_path}")
        timezone_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    elif args.channel:
        timezone_name = setting_text(
            reel_settings(load_channel(args.channel), settings_key), "timezone", DEFAULT_TIMEZONE
        )
    else:
        timezone_name = DEFAULT_TIMEZONE
    now = parse_datetime(args.now, timezone_name) if args.now else datetime.now(timezone.utc)
    scheduled_date = parse_date(args.date) if getattr(args, "date", None) else None
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")
    if args.schedule:
        rc, _ = run_due_jobs(
            schedule_path,
            now=now,
            dry_run=args.dry_run,
            include_future=args.all,
            retry_failed=args.retry_failed,
            limit=args.limit,
            upload_r2=args.upload_r2,
            media_base_url=args.media_base_url.strip(),
            r2_bucket=args.r2_bucket.strip(),
            r2_public_base_url=args.r2_public_base_url.strip(),
            db_path=db_path,
        )
        return rc
    return run_due_ledger(
        db_path=db_path,
        now=now,
        channel_id=args.channel,
        scheduled_date=scheduled_date,
        dry_run=args.dry_run,
        include_future=args.all,
        retry_failed=args.retry_failed,
        limit=args.limit,
        upload_r2=args.upload_r2,
        media_base_url=args.media_base_url.strip(),
        r2_bucket=args.r2_bucket.strip(),
        r2_public_base_url=args.r2_public_base_url.strip(),
        platform=platform,
        tiktok_mode=getattr(args, "tiktok_mode", None),
        tiktok_source=getattr(args, "tiktok_source", None),
        tiktok_privacy=getattr(args, "tiktok_privacy", None),
    )


def plan_ledger_command(args: argparse.Namespace) -> int:
    if args.limit_per_channel is not None and args.limit_per_channel <= 0:
        raise SystemExit("--limit-per-channel must be greater than zero")
    if args.jitter_minutes is not None and args.jitter_minutes < 0:
        raise SystemExit("--jitter-minutes must be zero or greater")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    planned = plan_ledger_rows(
        db_path=db_path,
        clips_dir=args.clips_dir,
        out_dir=args.out_dir,
        channel_filter=args.channel,
        start_at_text=scheduler_date_arg(args),
        limit_per_channel=args.limit_per_channel,
        jitter_minutes=args.jitter_minutes,
        scan_first=not args.no_scan,
        settings_key=settings_key_for(platform),
    )
    if not planned:
        print("[reel-scheduler] no new ledger rows to schedule")
        return 0
    for channel_id in sorted(planned):
        print(f"[reel-scheduler] planned {planned[channel_id]} {channel_id} {platform} reel(s)")
    print(f"[reel-scheduler] manifests: {args.out_dir.expanduser().resolve()}")
    print(f"[reel-scheduler] ledger: {db_path}")
    return 0


def queue_outputs_command(args: argparse.Namespace) -> int:
    if args.jitter_minutes is not None and args.jitter_minutes < 0:
        raise SystemExit("--jitter-minutes must be zero or greater")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    settings_key = settings_key_for(platform)
    clips_dirs = discover_output_clip_dirs(args.outputs_root)
    if not clips_dirs:
        print(f"[reel-scheduler] no output clips folders found in {args.outputs_root}")
        return 0
    print(f"[reel-scheduler] scanning {len(clips_dirs)} output folder(s)")
    for clips_dir in clips_dirs:
        scan_command(argparse.Namespace(clips_dir=clips_dir, db=db_path, platform=platform))

    append_start = args.start_at or latest_scheduled_text(db_path, args.channel)
    planned = plan_ledger_rows(
        db_path=db_path,
        clips_dir=args.outputs_root,
        out_dir=args.out_dir,
        channel_filter=args.channel,
        start_at_text=append_start,
        limit_per_channel=args.limit_per_channel,
        jitter_minutes=args.jitter_minutes,
        scan_first=False,
        settings_key=settings_key,
    )
    if planned:
        for channel_id in sorted(planned):
            print(f"[reel-scheduler] planned {planned[channel_id]} new {channel_id} {platform} reel(s)")
    else:
        print("[reel-scheduler] no new ledger rows to schedule")

    if args.mode == "reshuffle":
        if args.start_at or args.after:
            reflow_start = args.start_at or datetime.now(timezone_for(DEFAULT_TIMEZONE)).replace(microsecond=0).isoformat()
            alternate_after = args.after or reflow_start
            print(f"[reel-scheduler] reshuffling queued rows from {reflow_start}")
            reflow_queue_rows(
                db_path=db_path,
                channel_filter=args.channel,
                start_at_text=reflow_start,
                jitter_minutes=args.jitter_minutes,
                settings_key=settings_key,
                apply=True,
                include_start_at_slot=False,
            )
            alternate_source_queue_rows(
                db_path=db_path,
                after_text=alternate_after,
                channel_filter=args.channel,
                apply=True,
            )
        else:
            reflowed, alternated, start_at = refill_queue_from_now(
                db_path=db_path,
                channel_filter=args.channel,
                settings_key=settings_key,
                jitter_minutes=args.jitter_minutes,
            )
            print(
                f"[reel-scheduler] refilled queue from {start_at}: "
                f"channels={len(reflowed)} alternated={alternated}"
            )

    if not args.no_report:
        report_command(
            argparse.Namespace(
                db=db_path,
                platform=platform,
                channel=args.channel,
                limit=0,
                out=args.report_out,
            )
        )
    print(f"[reel-scheduler] ledger: {db_path}")
    return 0


def reflow_queue_command(args: argparse.Namespace) -> int:
    if args.jitter_minutes is not None and args.jitter_minutes < 0:
        raise SystemExit("--jitter-minutes must be zero or greater")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    reflow_queue_rows(
        db_path=db_path,
        channel_filter=args.channel,
        start_at_text=scheduler_date_arg(args),
        jitter_minutes=args.jitter_minutes,
        settings_key=settings_key_for(platform),
        apply=args.apply,
    )
    print(f"[reel-scheduler] ledger: {db_path}")
    return 0


def alternate_sources_command(args: argparse.Namespace) -> int:
    db_path = resolve_db(args)
    alternate_source_queue_rows(
        db_path=db_path,
        after_text=args.after,
        channel_filter=args.channel,
        apply=args.apply,
    )
    print(f"[reel-scheduler] ledger: {db_path}")
    return 0


def source_video_name(clips_dir: Path) -> str:
    """The <VIDEO_ID> folder (parent of clips/), used to group rows in the ledger."""
    return clips_dir.expanduser().resolve().parent.name


def discover_output_clip_dirs(outputs_root: Path) -> list[Path]:
    root = outputs_root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"Outputs folder does not exist: {root}")
    clips_dirs = [path / "clips" for path in root.iterdir() if (path / "clips").is_dir()]
    return sorted(clips_dirs, key=lambda path: path.parent.name)


def latest_scheduled_text(db_path: Path, channel_filter: str | None = None) -> str | None:
    with reel_ledger.connect(db_path) as conn:
        rows = reel_ledger.rows_with_schedule(conn, channel_filter)
    moments = [
        parsed
        for row in rows
        if (parsed := parse_row_datetime(row["scheduled_at"], DEFAULT_TIMEZONE)) is not None
    ]
    if not moments:
        return None
    latest = max(moments, key=lambda moment: moment.astimezone(timezone.utc))
    return latest.astimezone(timezone_for(DEFAULT_TIMEZONE)).replace(microsecond=0).isoformat()


def scan_command(args: argparse.Namespace) -> int:
    db_path = resolve_db(args)
    discovered = discover_channel_clips(args.clips_dir)
    known = set(available_channels())
    source_video = source_video_name(args.clips_dir)
    inserted = updated = unknown = 0
    with reel_ledger.connect(db_path) as conn:
        for clip_dir, lang, channel_id, media_path, notes, _ in discovered:
            if channel_id not in known:
                unknown += 1
                print(f"[reel-scheduler] skip: no channel config for '{channel_id}' ({media_path.name})")
                continue
            title = routed_title(lang, notes, load_one_liners(clip_dir))
            result = reel_ledger.upsert_discovered(
                conn,
                content_hash=reel_ledger.hash_file(media_path),
                channel_id=channel_id,
                lang=lang,
                clip_dir=clip_dir,
                media_path=media_path,
                source_video=source_video,
                title=title,
            )
            if result == "inserted":
                inserted += 1
            else:
                updated += 1
    print(
        f"[reel-scheduler] scanned {len(discovered)} variants -> "
        f"{inserted} new, {updated} existing, {unknown} unknown-channel"
    )
    print(f"[reel-scheduler] ledger: {db_path}")
    return 0


def import_schedules_command(args: argparse.Namespace) -> int:
    root = args.root.expanduser().resolve()
    schedule_paths = sorted(root.glob("*/schedule.json"))
    if not schedule_paths:
        print(f"[reel-scheduler] no schedule.json under {root}")
        return 0
    counts = {"inserted": 0, "updated": 0, "kept": 0}
    with reel_ledger.connect(args.db) as conn:
        for schedule_path in schedule_paths:
            schedule = read_json(schedule_path)
            if not isinstance(schedule, dict) or not isinstance(schedule.get("jobs"), list):
                continue
            channel_id = str(schedule.get("channel_id") or "")
            clips_dir = str(schedule.get("clips_dir") or "")
            source_video = Path(clips_dir).parent.name if clips_dir else schedule_path.parent.name
            for job in schedule["jobs"]:
                if not isinstance(job, dict):
                    continue
                media_path = Path(str(job.get("media_path") or ""))
                if media_path.is_file():
                    content_hash = reel_ledger.hash_file(media_path)
                else:
                    content_hash = reel_ledger.hash_text(f"{channel_id}:{media_path}")
                parsed = parse_channel_media(media_path.name)
                report_media_id, report_link = report_publish_identity(
                    Path(str(job.get("publish_report_path") or ""))
                )
                result = reel_ledger.upsert_imported(
                    conn,
                    content_hash=content_hash,
                    channel_id=channel_id,
                    lang=parsed[0] if parsed else None,
                    clip_dir=str(job.get("clip_dir") or media_path.parent),
                    media_path=media_path,
                    source_video=source_video,
                    title=(
                        str(job.get("topic") or job.get("description") or "").strip()
                        or manifest_title(Path(str(job.get("manifest_path") or "")))
                        or None
                    ),
                    status=LEGACY_STATUS_MAP.get(str(job.get("status")), reel_ledger.STATUS_NEW),
                    scheduled_at=str(job.get("scheduled_at") or "") or None,
                    published_at=str(job.get("published_at") or "") or None,
                    media_id=report_media_id or None,
                    permalink=str(job.get("permalink") or "") or report_link or None,
                    manifest_path=str(job.get("manifest_path") or "") or None,
                )
                counts[result] = counts.get(result, 0) + 1
    print(
        f"[reel-scheduler] imported {len(schedule_paths)} schedules -> "
        f"{counts['inserted']} new, {counts['updated']} upgraded, {counts['kept']} unchanged"
    )
    print(f"[reel-scheduler] ledger: {args.db}")
    return 0


def status_command(args: argparse.Namespace) -> int:
    db_path = resolve_db(args)
    with reel_ledger.connect(db_path) as conn:
        counts = reel_ledger.status_counts(conn, args.channel)
        upcoming = reel_ledger.upcoming(conn, args.channel, limit=args.limit)
        published = reel_ledger.recent_published(conn, args.channel, limit=5)
    if not counts:
        print(f"[reel-scheduler] ledger is empty: {db_path}")
        print("[reel-scheduler] run 'scan <clips_dir>' or 'import-schedules' first")
        return 0
    order = [
        reel_ledger.STATUS_NEW,
        reel_ledger.STATUS_SCHEDULED,
        reel_ledger.STATUS_PUBLISHING,
        reel_ledger.STATUS_PREVIEWED,
        reel_ledger.STATUS_PUBLISHED,
        reel_ledger.STATUS_FAILED,
        reel_ledger.STATUS_SKIPPED,
    ]
    for channel_id in sorted(counts):
        per = counts[channel_id]
        total = sum(per.values())
        summary = "  ".join(f"{name}={per[name]}" for name in order if per.get(name))
        print(f"[{channel_id}] total={total}  {summary}")
    if upcoming:
        print("\nUpcoming:")
        for row in upcoming:
            label = (row["title"] or row["clip_dir"] or "")[:60]
            print(f"  {row['scheduled_at']}  {row['channel_id']:<14} {row['status']:<16} {label}")
    if published:
        print("\nRecently published:")
        for row in published:
            print(f"  {row['published_at']}  {row['channel_id']:<14} {row['permalink'] or '(no permalink)'}")
    print(f"\nledger: {db_path}")
    return 0


def queue_ui_command(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    handler = make_queue_ui_handler(
        db_path=db_path,
        channel_filter=args.channel,
        limit=args.limit,
        settings_key=settings_key_for(platform),
        platform=platform,
        report_out=args.report_out,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[reel-scheduler] queue UI: {url}", flush=True)
    print(f"[reel-scheduler] ledger: {db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[reel-scheduler] queue UI stopped")
    finally:
        server.server_close()
    return 0


def cleanup_missing_command(args: argparse.Namespace) -> int:
    db_path = resolve_db(args)
    statuses = None if args.all_statuses else {reel_ledger.STATUS_FAILED}
    with reel_ledger.connect(db_path) as conn:
        query = "SELECT content_hash, channel_id, status, scheduled_at, title, media_path FROM reels"
        params: list[Any] = []
        clauses: list[str] = []
        if args.channel:
            clauses.append("channel_id=?")
            params.append(args.channel)
        if statuses is not None:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(sorted(statuses))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        rows = conn.execute(query, params).fetchall()
        missing = [row for row in rows if not Path(str(row["media_path"] or "")).is_file()]
        if not args.apply:
            print(
                f"[reel-scheduler] would delete {len(missing)} missing-media row(s) "
                f"from {db_path}; rerun with --apply"
            )
            for row in missing[:10]:
                print(
                    f"  {row['channel_id']} {row['status']} {row['scheduled_at']} "
                    f"{(row['title'] or row['media_path'] or '')[:80]}"
                )
            if len(missing) > 10:
                print(f"  ... {len(missing) - 10} more")
            return 0
        for row in missing:
            conn.execute(
                "DELETE FROM reels WHERE content_hash=? AND channel_id=?",
                (row["content_hash"], row["channel_id"]),
            )
    print(f"[reel-scheduler] deleted {len(missing)} missing-media row(s) from {db_path}")
    return 0


def sync_insights_command(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")
    db_path = resolve_db(args)
    if resolve_platform(args) == "tiktok":
        return sync_tiktok_insights(args, db_path)
    return sync_instagram_insights(args, db_path)


def sync_instagram_insights(args: argparse.Namespace, db_path: Path) -> int:
    import instagram_publish

    metrics = [part.strip() for part in str(args.metrics or "").split(",") if part.strip()]
    if not metrics:
        raise SystemExit("--metrics must include at least one metric")
    instagram_publish.load_env_file(ROOT / ".env")
    graph_version = instagram_publish.normalize_graph_version(
        args.graph_api_version or instagram_publish.graph_api_version()
    )
    graph_root = (args.graph_api_root or instagram_publish.graph_api_root()).rstrip("/")
    with reel_ledger.connect(db_path) as conn:
        rows = reel_ledger.published_reels_for_insights(conn, args.channel, args.limit)
    if not rows:
        print("[reel-scheduler] no published media ids to sync")
        return 0
    synced = skipped = 0
    for row in rows:
        manifest = {"channel_id": row["channel_id"]}
        access_token, token_source = instagram_publish.resolve_instagram_access_token(
            args.access_token,
            manifest,
            ROOT / "reel_scheduler.py",
        )
        if not access_token:
            skipped += 1
            print(f"[reel-scheduler] skip {row['media_id']}: no access token for {row['channel_id']}")
            continue
        if args.dry_run:
            print(
                f"[reel-scheduler] would sync {row['channel_id']} {row['media_id']} "
                f"via {token_source}"
            )
            synced += 1
            continue
        payload = fetch_insights(
            media_id=str(row["media_id"]),
            metrics=metrics,
            access_token=access_token,
            graph_version=graph_version,
            graph_api_root=graph_root,
        )
        parsed = parse_insight_metrics(payload)
        with reel_ledger.connect(db_path) as conn:
            reel_ledger.record_insight(
                conn,
                content_hash=str(row["content_hash"]),
                channel_id=str(row["channel_id"]),
                media_id=str(row["media_id"]),
                metrics=parsed,
                raw=json.dumps(payload, ensure_ascii=False),
            )
        synced += 1
        print(f"[reel-scheduler] synced {row['channel_id']} {row['media_id']}")
    print(f"[reel-scheduler] insights synced={synced} skipped={skipped}")
    return 0 if skipped == 0 else 1


def sync_tiktok_insights(args: argparse.Namespace, db_path: Path) -> int:
    """Pull TikTok view/like/comment/share counts for published rows.

    Uses ``/v2/video/query/`` (scope ``video.list``). The ledger ``media_id`` is
    the public post id captured at publish time; if a post was still in
    moderation then, its id may be a publish_id and the query won't match — that
    row is skipped until re-published metadata is available.
    """
    import tiktok_publish

    tiktok_publish.load_env_file(ROOT / ".env")
    with reel_ledger.connect(db_path) as conn:
        rows = reel_ledger.published_reels_for_insights(conn, args.channel, args.limit)
    if not rows:
        print("[reel-scheduler] no published TikTok post ids to sync")
        return 0
    synced = skipped = 0
    for row in rows:
        manifest = {"channel_id": row["channel_id"]}
        access_token, token_source = tiktok_publish.resolve_tiktok_access_token(
            args.access_token, manifest, ROOT / "reel_scheduler.py"
        )
        if not access_token:
            skipped += 1
            print(f"[reel-scheduler] skip {row['media_id']}: no TikTok token for {row['channel_id']}")
            continue
        post_id = str(row["media_id"])
        if args.dry_run:
            print(f"[reel-scheduler] would sync {row['channel_id']} {post_id} via {token_source}")
            synced += 1
            continue
        payload = tiktok_publish.query_video_metrics([post_id], access_token=access_token)
        parsed = tiktok_publish.parse_video_metrics(payload, post_id)
        if not parsed:
            skipped += 1
            print(f"[reel-scheduler] skip {post_id}: not found in video.query (still in moderation?)")
            continue
        with reel_ledger.connect(db_path) as conn:
            reel_ledger.record_insight(
                conn,
                content_hash=str(row["content_hash"]),
                channel_id=str(row["channel_id"]),
                media_id=post_id,
                metrics=parsed,
                raw=json.dumps(payload, ensure_ascii=False),
            )
        synced += 1
        print(f"[reel-scheduler] synced {row['channel_id']} {post_id}")
    print(f"[reel-scheduler] insights synced={synced} skipped={skipped}")
    return 0 if skipped == 0 else 1


def report_command(args: argparse.Namespace) -> int:
    if args.limit < 0:
        raise SystemExit("--limit must be zero or greater")
    limit = None if args.limit == 0 else args.limit
    db_path = resolve_db(args)
    with reel_ledger.connect(db_path) as conn:
        counts = reel_ledger.status_counts(conn, args.channel)
        upcoming = reel_ledger.upcoming(conn, args.channel, limit=limit)
        published = reel_ledger.recent_published(conn, args.channel, limit=limit)
        insight_rows = reel_ledger.latest_insight_rows(conn, args.channel, limit=limit)
    args.out.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    html_text = render_report_html(
        counts=counts,
        upcoming=upcoming,
        published=published,
        insight_rows=insight_rows,
        db_path=db_path,
    )
    args.out.expanduser().resolve().write_text(html_text, encoding="utf-8")
    print(f"[reel-scheduler] wrote report -> {args.out.expanduser().resolve()}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "plan":
        return plan_command(args)
    if args.command == "run-due":
        return run_due_command(args)
    if args.command == "plan-ledger":
        return plan_ledger_command(args)
    if args.command == "queue-outputs":
        return queue_outputs_command(args)
    if args.command == "reflow-queue":
        return reflow_queue_command(args)
    if args.command == "alternate-sources":
        return alternate_sources_command(args)
    if args.command == "scan":
        return scan_command(args)
    if args.command == "import-schedules":
        return import_schedules_command(args)
    if args.command == "status":
        return status_command(args)
    if args.command == "queue-ui":
        return queue_ui_command(args)
    if args.command == "cleanup-missing":
        return cleanup_missing_command(args)
    if args.command == "sync-insights":
        return sync_insights_command(args)
    if args.command == "report":
        return report_command(args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
