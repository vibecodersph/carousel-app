#!/usr/bin/env python3
"""Plan and publish channel-aware Instagram reels from a clips folder.

``plan`` scans clip directories for the channel's configured media filename,
writes one Instagram publisher manifest per reel, and assigns publish times.
``run-due`` is intended for cron or another periodic runner; it publishes only
jobs whose scheduled time has arrived and persists their status after each run.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import time as time_module
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
DEFAULT_QUEUE_UI_HOST = "127.0.0.1"
DEFAULT_QUEUE_UI_PORT = 8765
INSIGHT_DNS_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
SCHEDULE_VERSION = 1
DEFAULT_TRIAL_GRADUATION_STRATEGY = "MANUAL"
TRIAL_GRADUATION_STRATEGIES = {"MANUAL", "SS_PERFORMANCE"}


@dataclass(frozen=True)
class PostingSlot:
    clock: time
    trial_reel: bool
    jitter_minutes: int


@dataclass(frozen=True)
class ScheduledSlot:
    scheduled_at: datetime
    trial_reel: bool
    trial_graduation_strategy: str = ""


# reel-app multi-channel layout: one clip folder ships a file per channel, where
# the channel and caption language are encoded in the name reel.<lang>.<channel>.mp4
# ``reelcut`` can emit ``original`` (its documented default) alongside
# localized tokens such as ``en`` and ``ja``.  Accept a bounded language token
# rather than silently dropping the default-rendered variant during discovery.
CHANNEL_MEDIA_RE = re.compile(r"^reel\.([A-Za-z][A-Za-z0-9_-]{1,15})\.([A-Za-z0-9_-]+)\.mp4$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Platform registry: the same ledger + scheduler drives Instagram, TikTok, and
# explicit Facebook lanes. A platform selects which channel.json publishing block holds its
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
    "facebook": {
        "settings_key": "facebook_reels",
        "publisher": "facebook_publish.py",
        "report_name": "facebook_publish.json",
        "db_path": ROOT / "state" / "facebook.db",
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
PROFILE_HASHTAGS = {
    "ph-impeachment-news": ["#VPSara", "#SaraDuterte", "#Impeachment", "#Philippines", "#News", "#VibeCodersPH"],
}
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


def trial_slot_clocks(settings: dict[str, Any]) -> list[time]:
    raw_slots = settings.get("trial_slots")
    values = raw_slots if isinstance(raw_slots, list) else []
    clocks: list[time] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            clocks.append(parse_clock(text))
    return sorted(clocks)


def trial_graduation_strategy(settings: dict[str, Any]) -> str:
    strategy = str(
        settings.get("trial_graduation_strategy")
        or DEFAULT_TRIAL_GRADUATION_STRATEGY
    ).strip().upper()
    if strategy not in TRIAL_GRADUATION_STRATEGIES:
        raise SystemExit(
            "Invalid trial_graduation_strategy "
            f"{strategy!r}; choose from {sorted(TRIAL_GRADUATION_STRATEGIES)}"
        )
    return strategy


def posting_slots(settings: dict[str, Any], *, jitter_override: int | None = None) -> list[PostingSlot]:
    configured_jitter = int(settings.get("jitter_minutes") or 0)
    regular_jitter = configured_jitter if jitter_override is None else jitter_override
    trial_jitter = int(settings.get("trial_jitter_minutes") or 0)
    slots = [
        PostingSlot(clock=clock, trial_reel=False, jitter_minutes=regular_jitter)
        for clock in slot_clocks(settings)
    ]
    slots.extend(
        PostingSlot(clock=clock, trial_reel=True, jitter_minutes=trial_jitter)
        for clock in trial_slot_clocks(settings)
    )
    return sorted(slots, key=lambda slot: (slot.clock.hour, slot.clock.minute, slot.trial_reel))


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


def posting_slot_key(moment: datetime, slot: PostingSlot) -> str:
    kind = "trial" if slot.trial_reel else "regular"
    return f"{slot_key(moment, slot.clock)}#{kind}"


def slot_occupancy_key(moment: datetime, slots: list[PostingSlot], *, trial_reel: bool) -> str:
    for slot in slots:
        if slot.trial_reel != trial_reel:
            continue
        tolerance = max(0, slot.jitter_minutes)
        base = datetime.combine(moment.date(), slot.clock, tzinfo=moment.tzinfo)
        diff = abs((moment - base).total_seconds()) / 60
        if diff <= tolerance:
            return posting_slot_key(base, slot)
    kind = "trial" if trial_reel else "regular"
    return f"{moment.replace(second=0, microsecond=0).isoformat()}#{kind}"


def occupied_slot_keys(
    rows: list[Any],
    *,
    timezone_name: str,
    slots: list[PostingSlot],
) -> set[str]:
    tz = timezone_for(timezone_name)
    occupied: set[str] = set()
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
        occupied.add(slot_occupancy_key(local, slots, trial_reel=row_trial_enabled(row)))
    return occupied


def scheduled_at_matches_clock(
    scheduled_at: datetime,
    *,
    clock: time,
    timezone_name: str,
    tolerance_minutes: int,
) -> bool:
    tz = timezone_for(timezone_name)
    local = scheduled_at.astimezone(tz)
    base = datetime.combine(local.date(), clock, tzinfo=tz)
    diff = abs((local - base).total_seconds()) / 60
    return diff <= max(0, tolerance_minutes)


def trial_publish_for_slot(
    channel: Channel,
    scheduled_at: datetime,
    *,
    settings_key: str = "instagram_reels",
) -> tuple[bool, str]:
    settings = reel_settings(channel, settings_key)
    clocks = trial_slot_clocks(settings)
    if not clocks:
        return False, ""
    timezone_name = setting_text(settings, "timezone", DEFAULT_TIMEZONE)
    jitter_minutes = int(settings.get("trial_jitter_minutes") or 0)
    enabled = any(
        scheduled_at_matches_clock(
            scheduled_at,
            clock=clock,
            timezone_name=timezone_name,
            tolerance_minutes=jitter_minutes,
        )
        for clock in clocks
    )
    return (enabled, trial_graduation_strategy(settings) if enabled else "")


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


def next_open_slot_assignments(
    *,
    channel: Channel,
    start_at: datetime,
    existing_rows: list[Any],
    count: int,
    jitter_override: int | None = None,
    content_hashes: list[str] | None = None,
    settings_key: str = "instagram_reels",
    include_start_at: bool = False,
) -> list[ScheduledSlot]:
    if count <= 0:
        return []
    settings = reel_settings(channel, settings_key)
    timezone_name = setting_text(settings, "timezone", DEFAULT_TIMEZONE)
    tz = timezone_for(timezone_name)
    local_start = start_at.astimezone(tz)
    slots = posting_slots(settings, jitter_override=jitter_override)
    skipped = skip_weekdays(settings)
    occupied = occupied_slot_keys(
        existing_rows,
        timezone_name=timezone_name,
        slots=slots,
    )
    hashes = content_hashes or [str(i) for i in range(count)]
    assignments: list[ScheduledSlot] = []
    if include_start_at and local_start.weekday() not in skipped:
        manual = local_start.replace(microsecond=0)
        key = slot_occupancy_key(manual, slots, trial_reel=False)
        if key not in occupied:
            assignments.append(ScheduledSlot(scheduled_at=manual, trial_reel=False))
            occupied.add(key)
    day = local_start.date()
    strategy = trial_graduation_strategy(settings)
    while len(assignments) < count:
        if day.weekday() not in skipped:
            for slot in slots:
                base = datetime.combine(day, slot.clock, tzinfo=tz)
                if base < local_start:
                    continue
                key = posting_slot_key(base, slot)
                if key in occupied:
                    continue
                offset = deterministic_jitter(hashes[len(assignments)], slot.jitter_minutes)
                planned = base + timedelta(minutes=offset)
                assignments.append(
                    ScheduledSlot(
                        scheduled_at=planned.replace(microsecond=0),
                        trial_reel=slot.trial_reel,
                        trial_graduation_strategy=strategy if slot.trial_reel else "",
                    )
                )
                occupied.add(key)
                if len(assignments) >= count:
                    break
        day += timedelta(days=1)
    return assignments


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
    return [
        assignment.scheduled_at
        for assignment in next_open_slot_assignments(
            channel=channel,
            start_at=start_at,
            existing_rows=existing_rows,
            count=count,
            jitter_override=jitter_override,
            content_hashes=content_hashes,
            settings_key=settings_key,
            include_start_at=include_start_at,
        )
    ]


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
    """Load localized hooks from both the flat and schema-v2 formats.

    Older files store ``{"ja": "..."}``; newer files store the selected hook
    under ``{"languages": {"ja": {"text": "..."}}}``. Normalize both to
    the flat shape expected by the title-routing code.
    """
    path = clip_dir / "one_liners.json"
    if not path.exists():
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        return {}
    languages = data.get("languages")
    if not isinstance(languages, dict):
        return data
    normalized: dict[str, Any] = {}
    for lang, value in languages.items():
        if isinstance(value, dict):
            text = value.get("text")
        else:
            text = value
        if text:
            normalized[str(lang)] = text
    return normalized


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


def selection_profile_for_clip(clip_dir: Path) -> str:
    source_root = clip_dir.parent.parent if clip_dir.parent.name == "clips" else clip_dir.parent
    candidates_path = source_root / "candidates.json"
    if not candidates_path.is_file():
        return ""
    try:
        data = json.loads(candidates_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return re.sub(r"\s+", " ", str(data.get("selection_profile") or "")).strip()


def profile_hashtags(clip_dir: Path) -> list[str]:
    return list(PROFILE_HASHTAGS.get(selection_profile_for_clip(clip_dir), []))


def caption_hashtags(channel: Channel, clip_dir: Path, notes: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    profile_tags = profile_hashtags(clip_dir)
    hashtags = profile_tags if profile_tags else configured_hashtags(channel, settings)
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
    hashtags = caption_hashtags(channel, clip_dir, notes, settings)
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
    settings_key: str = "instagram_reels",
    trial_reel: bool = False,
    trial_graduation_strategy: str = "",
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
    if trial_reel:
        manifest["instagram_trial_reel"] = {
            "enabled": True,
            "graduation_strategy": trial_graduation_strategy or DEFAULT_TRIAL_GRADUATION_STRATEGY,
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
    elif platform == "facebook":
        command.extend(["--out", str(job["publish_report_path"])])
    else:
        command.extend(
            ["--single-video-media-type", "REELS", "--out", str(job["publish_report_path"])]
        )
    if dry_run:
        command.append("--dry-run")
    if upload_r2 and platform in {"instagram", "tiktok"}:
        command.extend(
            [
                "--upload-r2",
                "--r2-key-prefix",
                f"reels/{safe_job_id(channel_id)}/{safe_job_id(schedule_id)}/{safe_job_id(str(job['id']))}",
            ]
        )
    # tiktok_publish.py has no --media-base-url; it only pulls from R2 it uploaded.
    # facebook_publish.py uploads the local MP4 bytes directly.
    if media_base_url and platform == "instagram":
        command.extend(["--media-base-url", media_base_url])
    if r2_bucket and platform in {"instagram", "tiktok"}:
        command.extend(["--r2-bucket", r2_bucket])
    if r2_public_base_url and platform in {"instagram", "tiktok"}:
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
    trial_reel: bool = False,
    trial_graduation_strategy: str = "",
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
            settings_key=settings_key,
            trial_reel=trial_reel,
            trial_graduation_strategy=trial_graduation_strategy,
        ),
    )
    return manifest_path, caption, title


def round_robin_sources(rows: list[Any]) -> list[Any]:
    """Interleave rows by newest source first, preserving clip order per source."""
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        source = str(row["source_video"] or "")
        grouped.setdefault(source, []).append(row)
    ordered_sources = source_order_from(rows)
    ordered_sources.extend(source for source in grouped if source not in ordered_sources)
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


def source_order_from(rows: list[Any]) -> list[str]:
    """Return source folders newest-first, with stable ordering for tied scans."""
    first_seen: dict[str, int] = {}
    latest_created: dict[str, float] = {}
    for index, row in enumerate(rows):
        source = row_source_video(row)
        if not source:
            continue
        first_seen.setdefault(source, index)
        created = parse_row_datetime(row_value(row, "created_at"), DEFAULT_TIMEZONE)
        created_timestamp = (
            created.astimezone(timezone.utc).timestamp()
            if created is not None
            else float("-inf")
        )
        latest_created[source] = max(latest_created.get(source, float("-inf")), created_timestamp)
    return sorted(
        first_seen,
        key=lambda source: (-latest_created[source], first_seen[source], source),
    )


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


def source_round_robin_assignments(
    queued_rows: list[Any],
) -> tuple[list[tuple[Any, Any, str]], dict[str, list[str]]]:
    """Assign each channel's slots one row per source, newest sources first."""
    pools: dict[str, dict[str, list[Any]]] = {}
    rows_by_channel: dict[str, list[Any]] = {}
    for row in queued_rows:
        channel_id = str(row["channel_id"])
        source = row_source_video(row)
        pools.setdefault(channel_id, {}).setdefault(source, []).append(row)
        rows_by_channel.setdefault(channel_id, []).append(row)

    source_orders = {
        channel_id: source_order_from(channel_rows)
        for channel_id, channel_rows in rows_by_channel.items()
    }
    last_source_by_channel: dict[str, str] = {}
    assignments: list[tuple[Any, Any, str]] = []
    for slot in queued_rows:
        channel_id = str(slot["channel_id"])
        source_order = source_orders[channel_id]
        last_source = last_source_by_channel.get(channel_id)
        desired_source = next_source_after(source_order, last_source)
        selected = pop_alternating_row(
            pools,
            channel_id=channel_id,
            desired_source=desired_source,
            source_order=source_order,
            last_source=last_source,
        )
        assignments.append((slot, selected, desired_source))
        last_source_by_channel[channel_id] = row_source_video(selected)
    return assignments, source_orders


def update_manifest_scheduled_at(
    manifest_path: Path,
    scheduled_at: datetime,
    *,
    trial_reel: bool = False,
    trial_graduation_strategy: str = "",
) -> None:
    if not manifest_path.is_file():
        return
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        return
    data["scheduled_at"] = scheduled_at.replace(microsecond=0).isoformat()
    if trial_reel:
        data["instagram_trial_reel"] = {
            "enabled": True,
            "graduation_strategy": trial_graduation_strategy or DEFAULT_TRIAL_GRADUATION_STRATEGY,
        }
    else:
        data.pop("instagram_trial_reel", None)
    write_json(manifest_path, data)


def mark_manifest_unscheduled(manifest_path: Path) -> None:
    if not manifest_path.is_file():
        return
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        return
    data.pop("scheduled_at", None)
    data.pop("instagram_trial_reel", None)
    data["schedule_status"] = reel_ledger.STATUS_SKIPPED
    write_json(manifest_path, data)


def rebuilt_caption_for_row(row: Any, settings_key: str) -> tuple[str, list[str], str]:
    channel = load_channel(str(row["channel_id"]))
    clip_dir = Path(str(row["clip_dir"]))
    notes, _ = load_notes(clip_dir)
    manifest_path = Path(str(row["manifest_path"] or ""))
    manifest_data = read_optional_json(manifest_path) if str(row["manifest_path"] or "").strip() else None
    manifest = manifest_data if isinstance(manifest_data, dict) else {}
    source_metadata = load_source_metadata(clip_dir.parent)
    source_url = manifest_source_url(manifest) or source_metadata_value(
        source_metadata,
        "webpage_url",
        "original_url",
        "url",
    )
    lang = str(row["lang"] or "")
    title = (
        str(row["title"] or "").strip()
        or str(manifest.get("topic") or manifest.get("description") or "").strip()
        or routed_title(lang, notes, load_one_liners(clip_dir))
    )
    caption, hashtags = build_caption(
        channel,
        clip_dir,
        notes,
        source_url=source_url,
        title_override=title,
        settings_key=settings_key,
    )
    return caption, hashtags, title


def update_manifest_caption(manifest_path: Path, caption: str, hashtags: list[str], title: str) -> bool:
    if not manifest_path.is_file():
        return False
    data = read_json(manifest_path)
    if not isinstance(data, dict):
        return False
    data["instagram_caption"] = caption
    data["hashtags"] = hashtags
    if title:
        data["topic"] = title
        data["description"] = title
    write_json(manifest_path, data)
    (manifest_path.parent / "caption.txt").write_text(caption + "\n", encoding="utf-8")
    return True


def caption_refresh_excerpt(caption: str, max_chars: int = 220) -> str:
    compact = re.sub(r"\s+", " ", caption).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def caption_refresh_hash(caption: str) -> str:
    return hashlib.sha256(caption.encode("utf-8")).hexdigest()


def manifest_hashtags(manifest: dict[str, Any]) -> list[str]:
    values = manifest.get("hashtags")
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if str(value).strip()]


def caption_refresh_record(
    row: Any,
    old_caption: str,
    new_caption: str,
    *,
    has_manifest: bool,
    manifest: dict[str, Any],
    new_hashtags: list[str],
) -> dict[str, Any]:
    manifest_path = Path(str(row["manifest_path"] or ""))
    caption_path = manifest_path.parent / "caption.txt" if str(row["manifest_path"] or "").strip() else Path("")
    media_path = Path(str(row["media_path"] or ""))
    ledger_caption = str(row["caption"] or "").strip()
    manifest_caption = str(manifest.get("instagram_caption") or "").strip()
    warnings: list[str] = []
    if not has_manifest:
        warnings.append("missing_manifest")
    if str(row["media_path"] or "").strip() and not media_path.is_file():
        warnings.append("missing_media")
    if ledger_caption and manifest_caption and ledger_caption != manifest_caption:
        warnings.append("ledger_manifest_caption_mismatch")
    return {
        "content_hash": str(row["content_hash"] or ""),
        "channel_id": str(row["channel_id"] or ""),
        "status": str(row["status"] or ""),
        "scheduled_at": str(row["scheduled_at"] or ""),
        "source_video": str(row["source_video"] or ""),
        "title": str(row["title"] or ""),
        "media_path": str(row["media_path"] or ""),
        "caption_path": str(caption_path) if caption_path != Path("") else "",
        "manifest_path": str(row["manifest_path"] or ""),
        "has_manifest": has_manifest,
        "has_media": media_path.is_file() if str(row["media_path"] or "").strip() else False,
        "source_url": manifest_source_url(manifest),
        "old_hashtags": manifest_hashtags(manifest),
        "new_hashtags": new_hashtags,
        "old_caption_hash": caption_refresh_hash(old_caption),
        "new_caption_hash": caption_refresh_hash(new_caption),
        "old_caption_length": len(old_caption),
        "new_caption_length": len(new_caption),
        "ledger_manifest_caption_mismatch": bool(
            ledger_caption and manifest_caption and ledger_caption != manifest_caption
        ),
        "warnings": warnings,
        "old_caption": old_caption,
        "new_caption": new_caption,
        "old_excerpt": caption_refresh_excerpt(old_caption),
        "new_excerpt": caption_refresh_excerpt(new_caption),
    }


def caption_refresh_status_counts(conn: Any, channel_filter: str | None) -> dict[str, int]:
    query = "SELECT status, COUNT(*) AS n FROM reels"
    params: list[Any] = []
    if channel_filter:
        query += " WHERE channel_id=?"
        params.append(channel_filter)
    query += " GROUP BY status ORDER BY status"
    return {str(row["status"]): int(row["n"]) for row in conn.execute(query, params).fetchall()}


def caption_refresh_queue_fingerprint(conn: Any, channel_filter: str | None) -> dict[str, Any]:
    statuses = [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED]
    placeholders = ",".join("?" for _ in statuses)
    query = (
        "SELECT content_hash, channel_id, status, scheduled_at, updated_at "
        "FROM reels WHERE status IN (" + placeholders + ")"
    )
    params: list[Any] = list(statuses)
    if channel_filter:
        query += " AND channel_id=?"
        params.append(channel_filter)
    query += " ORDER BY channel_id, scheduled_at, content_hash"
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(query, params).fetchall():
        count += 1
        digest.update(
            json.dumps(
                [
                    str(row["content_hash"] or ""),
                    str(row["channel_id"] or ""),
                    str(row["status"] or ""),
                    str(row["scheduled_at"] or ""),
                    str(row["updated_at"] or ""),
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return {"count": count, "sha256": digest.hexdigest()}


def connect_ledger_readonly(db_path: Path) -> sqlite3.Connection:
    path = db_path.expanduser().resolve()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def render_caption_refresh_markdown(export: dict[str, Any]) -> str:
    rows = export.get("items") if isinstance(export.get("items"), list) else []
    lines = [
        "# Queued Caption Refresh Preview",
        "",
        f"- Generated: {markdown_cell(export.get('generated_at'))}",
        f"- Mode: {markdown_cell(export.get('mode'))}",
        f"- Platform: {markdown_cell(export.get('platform'))}",
        f"- Settings: {markdown_cell(export.get('settings_key'))}",
        f"- Channel filter: {markdown_cell(export.get('channel_filter'))}",
        f"- DB: {markdown_cell(export.get('db_path'))}",
        f"- Statuses: {markdown_cell(', '.join(export.get('statuses') or []))}",
        f"- Inspected rows: {markdown_number(export.get('inspected_count'))}",
        f"- Changed queued captions: {len(rows)}",
        f"- Unchanged rows: {markdown_number(export.get('unchanged_count'))}",
        f"- Status counts before: {markdown_cell(json.dumps(export.get('status_counts_before') or {}, sort_keys=True))}",
        f"- Status counts after: {markdown_cell(json.dumps(export.get('status_counts_after') or {}, sort_keys=True))}",
        f"- Fingerprint before: {markdown_cell((export.get('fingerprint_before') or {}).get('sha256'))}",
        f"- Fingerprint after: {markdown_cell((export.get('fingerprint_after') or {}).get('sha256'))}",
        f"- Publisher subprocess invoked: {markdown_cell(str(export.get('publisher_subprocess_invoked')))}",
        "",
        "| # | Scheduled | Channel | Status | Source | Title | Old CTA/context | New CTA/context |",
        "|---:|---|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {index} | {scheduled} | {channel} | {status} | {source} | {title} | {old} | {new} |".format(
                index=index,
                scheduled=markdown_cell(item.get("scheduled_at")),
                channel=markdown_cell(item.get("channel_id")),
                status=markdown_cell(item.get("status")),
                source=markdown_cell(item.get("source_video")),
                title=markdown_cell(item.get("title")),
                old=markdown_cell(item.get("old_excerpt")),
                new=markdown_cell(item.get("new_excerpt")),
            )
        )
    return "\n".join(lines) + "\n"


def write_caption_refresh_preview(
    out_path: Path,
    records: list[dict[str, Any]],
    *,
    apply: bool,
    metadata: dict[str, Any] | None = None,
) -> None:
    export = {
        "generated_at": utc_now(),
        "mode": "apply" if apply else "dry-run",
        "changed_count": len(records),
        "items": records,
    }
    if metadata:
        export.update(metadata)
    path = out_path.expanduser().resolve()
    if path.suffix.lower() == ".json":
        write_json(path, export)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_caption_refresh_markdown(export), encoding="utf-8")


def refresh_queued_captions(
    *,
    db_path: Path,
    channel_filter: str | None,
    settings_key: str,
    apply: bool,
    limit: int | None = None,
    preview_out: Path | None = None,
    platform: str = DEFAULT_PLATFORM,
) -> int:
    statuses = [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED]
    placeholders = ",".join("?" for _ in statuses)
    query = "SELECT * FROM reels WHERE status IN (" + placeholders + ") AND scheduled_at IS NOT NULL"
    params: list[Any] = list(statuses)
    if channel_filter:
        query += " AND channel_id=?"
        params.append(channel_filter)
    query += " ORDER BY channel_id, scheduled_at, source_video, clip_dir, lang, content_hash"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    previews: list[tuple[Any, str, str, bool]] = []
    preview_records: list[dict[str, Any]] = []
    status_counts_before: dict[str, int] = {}
    status_counts_after: dict[str, int] = {}
    fingerprint_before: dict[str, Any] = {}
    fingerprint_after: dict[str, Any] = {}
    inspected_count = 0

    def inspect_and_optionally_update(conn: Any) -> None:
        nonlocal inspected_count, status_counts_before, status_counts_after
        nonlocal fingerprint_before, fingerprint_after
        status_counts_before = caption_refresh_status_counts(conn, channel_filter)
        fingerprint_before = caption_refresh_queue_fingerprint(conn, channel_filter)
        rows = conn.execute(query, params).fetchall()
        inspected_count = len(rows)
        for row in rows:
            new_caption, hashtags, title = rebuilt_caption_for_row(row, settings_key)
            manifest_path = Path(str(row["manifest_path"] or ""))
            manifest_data = read_optional_json(manifest_path) if str(row["manifest_path"] or "").strip() else None
            manifest = manifest_data if isinstance(manifest_data, dict) else {}
            ledger_caption = str(row["caption"] or "").strip()
            manifest_caption = str(manifest.get("instagram_caption") or "").strip()
            old_caption = ledger_caption or manifest_caption
            changed = ledger_caption != new_caption or manifest_caption != new_caption
            if not changed:
                continue
            previews.append((row, old_caption, new_caption, manifest_path.is_file()))
            preview_records.append(
                caption_refresh_record(
                    row,
                    old_caption,
                    new_caption,
                    has_manifest=manifest_path.is_file(),
                    manifest=manifest,
                    new_hashtags=hashtags,
                )
            )
            if not apply:
                continue
            conn.execute(
                "UPDATE reels SET caption=?, updated_at=? WHERE content_hash=? AND channel_id=?",
                (new_caption, utc_now(), row["content_hash"], row["channel_id"]),
            )
            update_manifest_caption(manifest_path, new_caption, hashtags, title)
        status_counts_after = caption_refresh_status_counts(conn, channel_filter)
        fingerprint_after = caption_refresh_queue_fingerprint(conn, channel_filter)

    if apply:
        with reel_ledger.connect(db_path) as conn:
            inspect_and_optionally_update(conn)
    else:
        conn = connect_ledger_readonly(db_path)
        try:
            inspect_and_optionally_update(conn)
        finally:
            conn.close()

    for row, old_caption, new_caption, has_manifest in previews[:24]:
        old_line = re.sub(r"\s+", " ", old_caption).strip()[:90]
        new_line = re.sub(r"\s+", " ", new_caption).strip()[:90]
        manifest_note = "" if has_manifest else " (missing manifest)"
        print(
            f"[reel-scheduler] refresh {row['channel_id']:<14} {row['status']:<16} "
            f"{row['scheduled_at']}{manifest_note}"
        )
        print(f"  old: {old_line}")
        print(f"  new: {new_line}")
    if len(previews) > 24:
        print(f"[reel-scheduler] ... {len(previews) - 24} more queued rows")
    if preview_out is not None:
        write_caption_refresh_preview(
            preview_out,
            preview_records,
            apply=apply,
            metadata={
                "platform": platform,
                "settings_key": settings_key,
                "channel_filter": channel_filter or "",
                "db_path": str(db_path.expanduser().resolve()),
                "statuses": statuses,
                "limit": limit,
                "inspected_count": inspected_count,
                "unchanged_count": inspected_count - len(preview_records),
                "status_counts_before": status_counts_before,
                "status_counts_after": status_counts_after,
                "fingerprint_before": fingerprint_before,
                "fingerprint_after": fingerprint_after,
                "publisher_subprocess_invoked": False,
            },
        )
        print(f"[reel-scheduler] wrote caption refresh preview -> {preview_out.expanduser().resolve()}")
    action = "refreshed" if apply else "would refresh"
    print(f"[reel-scheduler] {action} {len(previews)} queued caption(s)")
    if not apply:
        print("[reel-scheduler] dry run only; rerun with --apply to update the ledger and manifests")
    return len(previews)


QUEUE_AUDIT_KEYWORDS = (
    ("Claude", ("claude", "クロード")),
    ("Anthropic", ("anthropic",)),
    ("OpenAI", ("openai", "chatgpt", "gpt")),
    ("AI agent", ("aiエージェント", "エージェント", "agent")),
    ("Claude Code", ("claude code",)),
)


def queue_audit_rows(
    conn: Any,
    *,
    channel_filter: str | None,
    limit: int | None,
) -> list[Any]:
    statuses = [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED]
    placeholders = ",".join("?" for _ in statuses)
    query = "SELECT * FROM reels WHERE status IN (" + placeholders + ") AND scheduled_at IS NOT NULL"
    params: list[Any] = list(statuses)
    if channel_filter:
        query += " AND channel_id=?"
        params.append(channel_filter)
    query += " ORDER BY channel_id, scheduled_at, source_video, clip_dir, lang, content_hash"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def queue_audit_keyword_counts(rows: list[Any]) -> dict[str, int]:
    counts = {label: 0 for label, _ in QUEUE_AUDIT_KEYWORDS}
    for row in rows:
        text = f"{row['title'] or ''}\n{row['caption'] or ''}".lower()
        for label, needles in QUEUE_AUDIT_KEYWORDS:
            if any(needle in text for needle in needles):
                counts[label] += 1
    return counts


def queue_audit_source_counts(rows: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        source = str(row["source_video"] or "(missing)")
        grouped.setdefault(source, []).append(row)
    out: list[dict[str, Any]] = []
    total = len(rows)
    for source, source_rows in grouped.items():
        scheduled = [str(row["scheduled_at"] or "") for row in source_rows]
        out.append(
            {
                "source_video": source,
                "count": len(source_rows),
                "share": round(len(source_rows) / total, 4) if total else 0,
                "first_scheduled_at": min(scheduled) if scheduled else "",
                "last_scheduled_at": max(scheduled) if scheduled else "",
            }
        )
    return sorted(out, key=lambda item: (-int(item["count"]), str(item["source_video"])))


def queue_audit_day_counts(rows: list[Any]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in rows:
        day = str(row["scheduled_at"] or "")[:10]
        if day:
            counts[day] = counts.get(day, 0) + 1
    return [{"date": day, "count": counts[day]} for day in sorted(counts)]


def queue_audit_longest_source_run(rows: list[Any]) -> dict[str, Any]:
    longest_source = ""
    longest_count = 0
    current_source = ""
    current_count = 0
    for row in rows:
        source = str(row["source_video"] or "(missing)")
        if source == current_source:
            current_count += 1
        else:
            current_source = source
            current_count = 1
        if current_count > longest_count:
            longest_source = current_source
            longest_count = current_count
    return {"source_video": longest_source, "count": longest_count}


def alternate_source_preview_record(
    *,
    row: Any,
    old_at: str,
    new_at: str,
    source: str,
    desired_source: str,
) -> dict[str, Any]:
    return {
        "content_hash": str(row["content_hash"] or ""),
        "channel_id": str(row["channel_id"] or ""),
        "status": str(row["status"] or ""),
        "source_video": source,
        "desired_source": desired_source,
        "scheduled_at_before": old_at,
        "scheduled_at_after": new_at,
        "would_move": old_at != new_at,
        "title": str(row["title"] or row["clip_dir"] or ""),
    }


def render_alternate_source_preview_markdown(export: dict[str, Any]) -> str:
    rows = export.get("items") if isinstance(export.get("items"), list) else []
    moves = sum(1 for item in rows if isinstance(item, dict) and item.get("would_move"))
    lines = [
        "# Alternate Source Preview",
        "",
        f"- Generated: {markdown_cell(export.get('generated_at'))}",
        f"- Mode: {markdown_cell(export.get('mode'))}",
        f"- Channel filter: {markdown_cell(export.get('channel_filter'))}",
        f"- Boundary: {markdown_cell(export.get('boundary'))}",
        f"- Rows inspected: {markdown_number(len(rows))}",
        f"- Rows that would move: {markdown_number(moves)}",
        f"- Fingerprint before: {markdown_cell((export.get('fingerprint_before') or {}).get('sha256'))}",
        f"- Fingerprint after: {markdown_cell((export.get('fingerprint_after') or {}).get('sha256'))}",
        "",
        "| # | Action | Before | After | Source | Desired | Title |",
        "|---:|---|---|---|---|---|---|",
    ]
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {index} | {action} | {before} | {after} | {source} | {desired} | {title} |".format(
                index=index,
                action="move" if item.get("would_move") else "keep",
                before=markdown_cell(item.get("scheduled_at_before")),
                after=markdown_cell(item.get("scheduled_at_after")),
                source=markdown_cell(item.get("source_video")),
                desired=markdown_cell(item.get("desired_source")),
                title=markdown_cell(item.get("title")),
            )
        )
    return "\n".join(lines) + "\n"


def write_alternate_source_preview(out_path: Path, export: dict[str, Any]) -> None:
    path = out_path.expanduser().resolve()
    if path.suffix.lower() == ".json":
        write_json(path, export)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_alternate_source_preview_markdown(export), encoding="utf-8")


def build_queue_growth_audit(
    *,
    db_path: Path,
    channel_filter: str | None,
    platform: str,
    limit: int | None,
) -> dict[str, Any]:
    conn = connect_ledger_readonly(db_path)
    try:
        rows = queue_audit_rows(conn, channel_filter=channel_filter, limit=limit)
        status_counts = caption_refresh_status_counts(conn, channel_filter)
        fingerprint = caption_refresh_queue_fingerprint(conn, channel_filter)
    finally:
        conn.close()
    total = len(rows)
    scheduled_values = [str(row["scheduled_at"] or "") for row in rows if str(row["scheduled_at"] or "")]
    source_counts = queue_audit_source_counts(rows)
    keyword_counts = queue_audit_keyword_counts(rows)
    follow_cta_count = sum(1 for row in rows if "フォロー" in str(row["caption"] or ""))
    old_save_cta_count = sum(1 for row in rows if "気になったら保存して" in str(row["caption"] or ""))
    generic_context_count = sum(1 for row in rows if "AI開発の現場で何が起きているのか" in str(row["caption"] or ""))
    top_source = source_counts[0] if source_counts else {}
    warnings: list[str] = []
    if total and int(top_source.get("count") or 0) / total >= 0.35:
        warnings.append(
            f"Top source {top_source.get('source_video')} is {int(top_source.get('count') or 0)}/{total} queued posts."
        )
    if len(source_counts) < 6 and total >= 30:
        warnings.append(f"Only {len(source_counts)} source videos feed {total} queued posts.")
    claude_total = max(keyword_counts.get("Claude", 0), keyword_counts.get("Claude Code", 0))
    if total and claude_total / total >= 0.3:
        warnings.append(f"Claude-related titles/captions appear in {claude_total}/{total} queued posts.")

    return {
        "generated_at": utc_now(),
        "mode": "read-only",
        "platform": platform,
        "channel_filter": channel_filter or "",
        "db_path": str(db_path.expanduser().resolve()),
        "limit": limit,
        "statuses": [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED],
        "queued_count": total,
        "first_scheduled_at": min(scheduled_values) if scheduled_values else "",
        "last_scheduled_at": max(scheduled_values) if scheduled_values else "",
        "status_counts": status_counts,
        "queue_fingerprint": fingerprint,
        "source_counts": source_counts,
        "keyword_counts": keyword_counts,
        "day_counts": queue_audit_day_counts(rows),
        "longest_same_source_run": queue_audit_longest_source_run(rows),
        "cta": {
            "follow_cta_count": follow_cta_count,
            "old_save_cta_count": old_save_cta_count,
            "generic_context_count": generic_context_count,
        },
        "warnings": warnings,
        "sample_upcoming": [
            {
                "scheduled_at": str(row["scheduled_at"] or ""),
                "source_video": str(row["source_video"] or ""),
                "title": str(row["title"] or ""),
            }
            for row in rows[:12]
        ],
    }


def render_queue_growth_audit_markdown(audit: dict[str, Any]) -> str:
    source_counts = audit.get("source_counts") if isinstance(audit.get("source_counts"), list) else []
    keyword_counts = audit.get("keyword_counts") if isinstance(audit.get("keyword_counts"), dict) else {}
    day_counts = audit.get("day_counts") if isinstance(audit.get("day_counts"), list) else []
    warnings = audit.get("warnings") if isinstance(audit.get("warnings"), list) else []
    samples = audit.get("sample_upcoming") if isinstance(audit.get("sample_upcoming"), list) else []
    cta = audit.get("cta") if isinstance(audit.get("cta"), dict) else {}
    longest = audit.get("longest_same_source_run") if isinstance(audit.get("longest_same_source_run"), dict) else {}
    lines = [
        "# Queued Reel Growth Audit",
        "",
        f"- Generated: {markdown_cell(audit.get('generated_at'))}",
        f"- Mode: {markdown_cell(audit.get('mode'))}",
        f"- Platform: {markdown_cell(audit.get('platform'))}",
        f"- Channel filter: {markdown_cell(audit.get('channel_filter'))}",
        f"- DB: {markdown_cell(audit.get('db_path'))}",
        f"- Queued rows: {markdown_number(audit.get('queued_count'))}",
        f"- Window: {markdown_cell(audit.get('first_scheduled_at'))} to {markdown_cell(audit.get('last_scheduled_at'))}",
        f"- Status counts: {markdown_cell(json.dumps(audit.get('status_counts') or {}, sort_keys=True))}",
        f"- Queue fingerprint: {markdown_cell((audit.get('queue_fingerprint') or {}).get('sha256'))}",
        "",
        "## Warnings",
    ]
    if warnings:
        lines.extend(f"- {markdown_cell(warning)}" for warning in warnings)
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Caption Markers",
            "",
            "| Follow CTA | Old save CTA | Generic context |",
            "|---:|---:|---:|",
            "| {follow} | {save} | {generic} |".format(
                follow=markdown_number(cta.get("follow_cta_count")),
                save=markdown_number(cta.get("old_save_cta_count")),
                generic=markdown_number(cta.get("generic_context_count")),
            ),
            "",
            "## Source Concentration",
            "",
            "| Source | Count | Share | First scheduled | Last scheduled |",
            "|---|---:|---:|---|---|",
        ]
    )
    for item in source_counts:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {source} | {count} | {share:.1f}% | {first} | {last} |".format(
                source=markdown_cell(item.get("source_video")),
                count=int(item.get("count") or 0),
                share=float(item.get("share") or 0) * 100,
                first=markdown_cell(item.get("first_scheduled_at")),
                last=markdown_cell(item.get("last_scheduled_at")),
            )
        )
    lines.extend(
        [
            "",
            f"Longest same-source run: {markdown_cell(longest.get('source_video'))} x {markdown_number(longest.get('count'))}",
            "",
            "## Topic Signals",
            "",
            "| Signal | Queued posts |",
            "|---|---:|",
        ]
    )
    for label in sorted(keyword_counts):
        lines.append(f"| {markdown_cell(label)} | {markdown_number(keyword_counts[label])} |")
    lines.extend(["", "## Daily Cadence", "", "| Date | Posts |", "|---|---:|"])
    for item in day_counts:
        if not isinstance(item, dict):
            continue
        lines.append(f"| {markdown_cell(item.get('date'))} | {markdown_number(item.get('count'))} |")
    lines.extend(["", "## Upcoming Sample", "", "| Scheduled | Source | Title |", "|---|---|---|"])
    for item in samples:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {scheduled} | {source} | {title} |".format(
                scheduled=markdown_cell(item.get("scheduled_at")),
                source=markdown_cell(item.get("source_video")),
                title=markdown_cell(item.get("title")),
            )
        )
    return "\n".join(lines) + "\n"


def write_queue_growth_audit(out_path: Path, audit: dict[str, Any]) -> None:
    path = out_path.expanduser().resolve()
    if path.suffix.lower() == ".json":
        write_json(path, audit)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_queue_growth_audit_markdown(audit), encoding="utf-8")


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
            "UPDATE reels SET status=?, scheduled_at=NULL, trial_reel=0, "
            "trial_graduation_strategy=NULL, last_error=?, updated_at=? "
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
    now: datetime | None = None,
) -> tuple[dict[str, int], int, str]:
    current = now or datetime.now(timezone_for(DEFAULT_TIMEZONE))
    start_at = current.astimezone(timezone_for(DEFAULT_TIMEZONE)).replace(microsecond=0).isoformat()
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
        settings_key=settings_key,
    )
    return reflowed, alternated, start_at


def queue_append_start_text(
    *,
    db_path: Path,
    channel_filter: str | None,
    now: datetime | None = None,
) -> str:
    """Start appends after the future queue, never after old published history."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone_for(DEFAULT_TIMEZONE))
    boundary = current.astimezone(timezone.utc)
    statuses = [
        reel_ledger.STATUS_SCHEDULED,
        reel_ledger.STATUS_PREVIEWED,
        reel_ledger.STATUS_PUBLISHING,
    ]
    placeholders = ",".join("?" for _ in statuses)
    query = (
        "SELECT scheduled_at FROM reels WHERE status IN (" + placeholders + ") "
        "AND scheduled_at IS NOT NULL"
        + (" AND channel_id=?" if channel_filter else "")
    )
    params: list[Any] = list(statuses)
    if channel_filter:
        params.append(channel_filter)
    with reel_ledger.connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    future_moments: list[datetime] = []
    for row in rows:
        parsed = parse_row_datetime(row["scheduled_at"], DEFAULT_TIMEZONE)
        if parsed is not None and parsed.astimezone(timezone.utc) >= boundary:
            future_moments.append(parsed)
    start = max(future_moments, key=lambda moment: moment.astimezone(timezone.utc)) if future_moments else current
    return start.astimezone(timezone_for(DEFAULT_TIMEZONE)).replace(microsecond=0).isoformat()


def scan_and_plan_outputs(
    *,
    db_path: Path,
    outputs_root: Path,
    out_dir: Path,
    channel_filter: str | None,
    platform: str,
    settings_key: str,
    limit_per_channel: int | None,
    jitter_minutes: int | None,
    start_at_text: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    clips_dirs = discover_output_clip_dirs(outputs_root)
    if not clips_dirs:
        return {"clips_dirs": 0, "planned": {}, "append_start": start_at_text or ""}
    for clips_dir in clips_dirs:
        scan_command(argparse.Namespace(clips_dir=clips_dir, db=db_path, platform=platform))

    append_start = start_at_text or queue_append_start_text(
        db_path=db_path,
        channel_filter=channel_filter,
        now=now,
    )
    planned = plan_ledger_rows(
        db_path=db_path,
        clips_dir=outputs_root,
        out_dir=out_dir,
        channel_filter=channel_filter,
        start_at_text=append_start,
        limit_per_channel=limit_per_channel,
        jitter_minutes=jitter_minutes,
        scan_first=False,
        settings_key=settings_key,
    )
    return {
        "clips_dirs": len(clips_dirs),
        "planned": planned,
        "append_start": append_start,
    }


def scan_and_reshuffle_outputs(
    *,
    db_path: Path,
    outputs_root: Path,
    out_dir: Path,
    channel_filter: str | None,
    platform: str,
    settings_key: str,
    limit_per_channel: int | None,
    jitter_minutes: int | None,
    start_at_text: str | None = None,
    after_text: str | None = None,
    only_if_planned: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the queue-outputs refill pipeline used by both CLI and queue UI."""
    planned_result = scan_and_plan_outputs(
        db_path=db_path,
        outputs_root=outputs_root,
        out_dir=out_dir,
        channel_filter=channel_filter,
        platform=platform,
        settings_key=settings_key,
        limit_per_channel=limit_per_channel,
        jitter_minutes=jitter_minutes,
        start_at_text=start_at_text,
        now=now,
    )
    if planned_result["clips_dirs"] == 0:
        return {
            "clips_dirs": 0,
            "planned": {},
            "append_start": planned_result["append_start"],
            "reflowed": {},
            "alternated": 0,
            "start_at": start_at_text or "",
        }

    if only_if_planned and not planned_result["planned"]:
        return {
            "clips_dirs": planned_result["clips_dirs"],
            "planned": planned_result["planned"],
            "append_start": planned_result["append_start"],
            "reflowed": {},
            "alternated": 0,
            "start_at": start_at_text or planned_result["append_start"],
        }

    if start_at_text or after_text:
        current = now or datetime.now(timezone_for(DEFAULT_TIMEZONE))
        reflow_start = (
            start_at_text
            or current.astimezone(timezone_for(DEFAULT_TIMEZONE)).replace(microsecond=0).isoformat()
        )
        alternate_after = after_text or reflow_start
        reflowed = reflow_queue_rows(
            db_path=db_path,
            channel_filter=channel_filter,
            start_at_text=reflow_start,
            jitter_minutes=jitter_minutes,
            settings_key=settings_key,
            apply=True,
            include_start_at_slot=False,
        )
        alternated = alternate_source_queue_rows(
            db_path=db_path,
            after_text=alternate_after,
            channel_filter=channel_filter,
            apply=True,
            settings_key=settings_key,
        )
        reshuffle_start = reflow_start
    else:
        reflowed, alternated, reshuffle_start = refill_queue_from_now(
            db_path=db_path,
            channel_filter=channel_filter,
            settings_key=settings_key,
            jitter_minutes=jitter_minutes,
            now=now,
        )

    return {
        "clips_dirs": planned_result["clips_dirs"],
        "planned": planned_result["planned"],
        "append_start": planned_result["append_start"],
        "reflowed": reflowed,
        "alternated": alternated,
        "start_at": reshuffle_start,
    }


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
    reshuffle_form = (
        "<form method=\"post\" action=\"/reshuffle\" "
        "onsubmit=\"return confirm('Scan reel-app outputs, fill open slots, and reshuffle the unpublished queue from now?');\">"
        "<button class=\"reshuffle\" type=\"submit\">Reshuffle Queue</button>"
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
    button.reshuffle {{ background: var(--accent); }}
    button.reshuffle:hover {{ background: #125543; }}
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
          {reshuffle_form}
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
    outputs_root: Path = DEFAULT_REEL_OUTPUTS,
    out_dir: Path = DEFAULT_OUT / "ledger",
    limit_per_channel: int | None = None,
    jitter_minutes: int | None = None,
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

        def redirect_report(self, **params: str) -> None:
            location = "/report" + (("?" + urlencode(params)) if params else "")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()

        def report_parts(self) -> tuple[dict[str, dict[str, int]], list[Any], list[Any], list[Any]]:
            return load_report_data(
                db_path=db_path,
                channel_filter=channel_filter,
                limit=limit,
            )

        def report_payload(self) -> dict[str, Any]:
            _, _, _, insight_rows = self.report_parts()
            return build_insights_export(
                insight_rows=insight_rows,
                db_path=db_path,
                platform=platform,
                channel_filter=channel_filter,
            )

        def refresh_report_file(self) -> None:
            counts, upcoming, published, insight_rows = self.report_parts()
            json_out = report_json_path(report_out)
            md_out = report_markdown_path(report_out)
            export = build_insights_export(
                insight_rows=insight_rows,
                db_path=db_path,
                platform=platform,
                channel_filter=channel_filter,
            )
            write_json(json_out, export)
            write_insights_markdown(export=export, out_path=md_out)
            report_out.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            report_out.expanduser().resolve().write_text(
                render_report_html(
                    counts=counts,
                    upcoming=upcoming,
                    published=published,
                    insight_rows=insight_rows,
                    db_path=db_path,
                    platform=platform,
                    sync_action_url=DEFAULT_REPORT_SYNC_ACTION_URL,
                    insights_json_href=report_href(json_out, report_out),
                    insights_markdown_href=report_href(md_out, report_out),
                ),
                encoding="utf-8",
            )

        def serve_report(self, params: dict[str, list[str]]) -> None:
            counts, upcoming, published, insight_rows = self.report_parts()
            export = build_insights_export(
                insight_rows=insight_rows,
                db_path=db_path,
                platform=platform,
                channel_filter=channel_filter,
            )
            write_json(report_json_path(report_out), export)
            write_insights_markdown(export=export, out_path=report_markdown_path(report_out))
            self.send_html(
                render_report_html(
                    counts=counts,
                    upcoming=upcoming,
                    published=published,
                    insight_rows=insight_rows,
                    db_path=db_path,
                    platform=platform,
                    sync_action_url="/sync-insights",
                    insights_json_href="/insights.json",
                    insights_markdown_href="/insights.md",
                    message=_first_query_value(params, "message"),
                    error=_first_query_value(params, "error"),
                )
            )

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
            if parsed.path == "/report":
                self.serve_report(parse_qs(parsed.query))
                return
            if parsed.path == "/insights.json":
                payload = json.dumps(self.report_payload(), indent=2, ensure_ascii=False) + "\n"
                self.send_bytes(payload.encode("utf-8"), content_type="application/json; charset=utf-8")
                return
            if parsed.path == "/insights.md":
                payload = render_insights_markdown(self.report_payload())
                self.send_bytes(payload.encode("utf-8"), content_type="text/markdown; charset=utf-8")
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
            if parsed.path == "/reshuffle":
                try:
                    result = scan_and_reshuffle_outputs(
                        db_path=db_path,
                        outputs_root=outputs_root,
                        out_dir=out_dir,
                        channel_filter=channel_filter,
                        platform=platform,
                        settings_key=settings_key,
                        limit_per_channel=limit_per_channel,
                        jitter_minutes=jitter_minutes,
                    )
                except SystemExit as exc:
                    self.redirect_home(error=str(exc))
                    return
                report_command(
                    argparse.Namespace(
                        db=db_path,
                        platform=platform,
                        channel=channel_filter,
                        limit=0,
                        out=report_out,
                    )
                )
                planned = sum(int(value) for value in result["planned"].values())
                reflowed_count = sum(int(value) for value in result["reflowed"].values())
                self.redirect_home(
                    message=(
                        f"Scanned {result['clips_dirs']} output folders, planned {planned} new rows, "
                        f"reshuffled from {result['start_at']}: processed {reflowed_count} queued rows, "
                        f"re-alternated {result['alternated']} rows"
                    )
                )
                return
            if parsed.path == "/sync-insights":
                try:
                    rc = sync_insights_command(
                        argparse.Namespace(
                            platform=platform,
                            channel=channel_filter,
                            db=db_path,
                            limit=None,
                            dry_run=False,
                            metrics=",".join(INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS),
                            access_token="",
                            graph_api_version="",
                            graph_api_root="",
                        )
                    )
                except SystemExit as exc:
                    self.redirect_report(error=str(exc))
                    return
                self.refresh_report_file()
                if rc == 0:
                    self.redirect_report(message="Insights updated from the platform API")
                else:
                    self.redirect_report(
                        message="Updated available insights; skipped inaccessible rows, see server log"
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
            assignments = next_open_slot_assignments(
                channel=channel,
                start_at=start_at,
                existing_rows=existing,
                count=len(channel_rows),
                jitter_override=jitter_minutes,
                content_hashes=[str(row["content_hash"]) for row in channel_rows],
                settings_key=settings_key,
            )
            for row, assignment in zip(channel_rows, assignments):
                scheduled_at = assignment.scheduled_at
                manifest_path, caption, title = write_ledger_manifest(
                    row=row,
                    channel=channel,
                    scheduled_at=scheduled_at,
                    out_dir=out_dir,
                    settings_key=settings_key,
                    trial_reel=assignment.trial_reel,
                    trial_graduation_strategy=assignment.trial_graduation_strategy,
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
                    trial_reel=1 if assignment.trial_reel else 0,
                    trial_graduation_strategy=assignment.trial_graduation_strategy or None,
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
            eligible_rows = [
                row
                for row in by_channel[channel_id]
                if (parse_row_datetime(row["scheduled_at"], timezone_name) or start_at) >= start_at
            ]
            channel_rows = round_robin_sources(eligible_rows)
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
            assignments = next_open_slot_assignments(
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
            for row, assignment in zip(channel_rows, assignments):
                scheduled_at = assignment.scheduled_at
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
                    "UPDATE reels SET scheduled_at=?, trial_reel=?, "
                    "trial_graduation_strategy=?, updated_at=? "
                    "WHERE content_hash=? AND channel_id=?",
                    (
                        new_at,
                        1 if assignment.trial_reel else 0,
                        assignment.trial_graduation_strategy or None,
                        utc_now(),
                        row["content_hash"],
                        channel_id,
                    ),
                )
                update_manifest_scheduled_at(
                    Path(str(row["manifest_path"] or "")),
                    scheduled_at,
                    trial_reel=assignment.trial_reel,
                    trial_graduation_strategy=assignment.trial_graduation_strategy,
                )
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
    preview_out: Path | None = None,
    settings_key: str = "instagram_reels",
) -> int:
    """Shuffle queued content into existing slots so source_video alternates globally."""
    boundary = (
        parse_datetime(after_text, DEFAULT_TIMEZONE)
        if after_text
        else datetime.now(timezone.utc)
    )
    queue_statuses = [reel_ledger.STATUS_SCHEDULED, reel_ledger.STATUS_PREVIEWED]
    queued_placeholders = ",".join("?" for _ in queue_statuses)
    previews: list[tuple[str, str, str, str, str, str, str]] = []
    preview_records: list[dict[str, Any]] = []
    fingerprint_before: dict[str, Any] = {}
    fingerprint_after: dict[str, Any] = {}

    def write_preview_if_requested() -> None:
        if preview_out is None:
            return
        export = {
            "generated_at": utc_now(),
            "mode": "apply" if apply else "dry-run",
            "channel_filter": channel_filter or "",
            "boundary": boundary.isoformat(),
            "fingerprint_before": fingerprint_before,
            "fingerprint_after": fingerprint_after,
            "items": preview_records,
        }
        write_alternate_source_preview(preview_out, export)
        print(f"[reel-scheduler] wrote alternate source preview -> {preview_out.expanduser().resolve()}")

    if apply:
        with reel_ledger.connect(db_path) as conn:
            fingerprint_before = caption_refresh_queue_fingerprint(conn, channel_filter)
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
                write_preview_if_requested()
                return 0

            assignments, source_orders = source_round_robin_assignments(queued_rows)
            if not any(len(source_order) >= 2 for source_order in source_orders.values()):
                print("[reel-scheduler] fewer than two source videos remain in the queued rows")
                write_preview_if_requested()
                return 0

            for slot, selected, desired_source in assignments:
                channel_id = str(selected["channel_id"])
                old_at = str(selected["scheduled_at"] or "")
                new_at = str(slot["scheduled_at"] or "")
                source = row_source_video(selected)
                previews.append(
                    (
                        channel_id,
                        old_at,
                        new_at,
                        source,
                        desired_source,
                        str(selected["status"] or ""),
                        str(selected["title"] or selected["clip_dir"] or "")[:70],
                    )
                )
                preview_records.append(
                    alternate_source_preview_record(
                        row=selected,
                        old_at=old_at,
                        new_at=new_at,
                        source=source,
                        desired_source=desired_source,
                    )
                )
                scheduled_at = parse_row_datetime(new_at, DEFAULT_TIMEZONE)
                if scheduled_at is None:
                    continue
                slot_trial_reel = row_trial_enabled(slot)
                slot_trial_strategy = row_trial_strategy(slot)
                conn.execute(
                    "UPDATE reels SET scheduled_at=?, trial_reel=?, "
                    "trial_graduation_strategy=?, updated_at=? "
                    "WHERE content_hash=? AND channel_id=?",
                    (
                        new_at,
                        1 if slot_trial_reel else 0,
                        slot_trial_strategy or None,
                        utc_now(),
                        selected["content_hash"],
                        channel_id,
                    ),
                )
                update_manifest_scheduled_at(
                    Path(str(selected["manifest_path"] or "")),
                    scheduled_at,
                    trial_reel=slot_trial_reel,
                    trial_graduation_strategy=slot_trial_strategy,
                )
            fingerprint_after = caption_refresh_queue_fingerprint(conn, channel_filter)
    else:
        conn = connect_ledger_readonly(db_path)
        try:
            fingerprint_before = caption_refresh_queue_fingerprint(conn, channel_filter)
            fingerprint_after = fingerprint_before
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
                write_preview_if_requested()
                return 0

            assignments, source_orders = source_round_robin_assignments(queued_rows)
            if not any(len(source_order) >= 2 for source_order in source_orders.values()):
                print("[reel-scheduler] fewer than two source videos remain in the queued rows")
                write_preview_if_requested()
                return 0

            for slot, selected, desired_source in assignments:
                channel_id = str(selected["channel_id"])
                old_at = str(selected["scheduled_at"] or "")
                new_at = str(slot["scheduled_at"] or "")
                source = row_source_video(selected)
                previews.append(
                    (
                        channel_id,
                        old_at,
                        new_at,
                        source,
                        desired_source,
                        str(selected["status"] or ""),
                        str(selected["title"] or selected["clip_dir"] or "")[:70],
                    )
                )
                preview_records.append(
                    alternate_source_preview_record(
                        row=selected,
                        old_at=old_at,
                        new_at=new_at,
                        source=source,
                        desired_source=desired_source,
                    )
                )
        finally:
            conn.close()

    for channel_id, old_at, new_at, source, desired_source, status, title in previews[:24]:
        verb = "move" if old_at != new_at else "keep"
        desired_note = f", wanted {desired_source}" if desired_source and desired_source != source else ""
        print(
            f"[reel-scheduler] {verb} {channel_id:<14} {status:<16} "
            f"{source}{desired_note}: {old_at} -> {new_at}  {title}"
        )
    if len(previews) > 24:
        print(f"[reel-scheduler] ... {len(previews) - 24} more queued rows")
    write_preview_if_requested()
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
            publisher_name = str(platform_config(platform)["publisher"])
            with reel_ledger.connect(db_path) as conn:
                reel_ledger.set_status(
                    conn,
                    content_hash,
                    row_channel_id,
                    reel_ledger.STATUS_FAILED,
                    last_error=f"{publisher_name} exited {result.returncode}",
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
        if not name:
            continue
        values = item.get("values") if isinstance(item.get("values"), list) else []
        latest = values[-1] if values and isinstance(values[-1], dict) else {}
        value = latest.get("value") if isinstance(latest, dict) else None
        if value is None and isinstance(item.get("total_value"), dict):
            value = item["total_value"].get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        numeric = float(value)
        metrics[name] = int(numeric) if numeric.is_integer() else numeric
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

    request = {
        "access_token": access_token,
        "graph_version": graph_version,
        "graph_api_root": graph_api_root,
        "params": {"metric": ",".join(metrics)},
        "method": "GET",
        "timeout": 30,
    }
    for attempt, delay in enumerate((0.0, *INSIGHT_DNS_RETRY_DELAYS_SECONDS)):
        if delay:
            time_module.sleep(delay)
        try:
            return instagram_publish.graph_request(f"{media_id}/insights", **request)
        except SystemExit as exc:
            message = str(exc)
            dns_failure = any(
                marker in message.lower()
                for marker in (
                    "temporary failure in name resolution",
                    "name or service not known",
                    "nodename nor servname provided",
                    "failed to resolve",
                )
            )
            if not dns_failure or attempt == len(INSIGHT_DNS_RETRY_DELAYS_SECONDS):
                raise
            print(
                f"[reel-scheduler] transient Graph DNS failure for {media_id}; "
                f"retrying ({attempt + 1}/{len(INSIGHT_DNS_RETRY_DELAYS_SECONDS)})",
                flush=True,
            )
    raise AssertionError("unreachable")


def merge_insight_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge successful metric responses without inventing missing values."""
    merged: dict[str, Any] = {"data": []}
    seen: set[str] = set()
    for payload in payloads:
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if name and name in seen:
                continue
            if name:
                seen.add(name)
            merged["data"].append(item)
    return merged


def fetch_instagram_insights_resilient(
    *,
    media_id: str,
    metrics: list[str],
    access_token: str,
    graph_version: str,
    graph_api_root: str,
) -> tuple[dict[str, Any], list[str]]:
    """Fetch requested metrics while isolating media-dependent optional ones.

    Meta can reject a whole comma-separated request when a single retention or
    crosspost metric is unavailable for that media object. Try the efficient
    one-request path first. Only after it fails do we fetch the established
    core separately and probe optional metrics. A core failure still bubbles up
    so a deleted/inaccessible media id is never recorded as an empty snapshot.
    """
    try:
        return (
            fetch_insights(
                media_id=media_id,
                metrics=metrics,
                access_token=access_token,
                graph_version=graph_version,
                graph_api_root=graph_api_root,
            ),
            [],
        )
    except SystemExit:
        optional = [
            metric for metric in metrics if metric in INSTAGRAM_OPTIONAL_INSIGHT_METRIC_KEYS
        ]
        if not optional:
            raise

    core = [metric for metric in metrics if metric not in optional]
    payloads: list[dict[str, Any]] = []
    if core:
        payloads.append(
            fetch_insights(
                media_id=media_id,
                metrics=core,
                access_token=access_token,
                graph_version=graph_version,
                graph_api_root=graph_api_root,
            )
        )

    warnings: list[str] = []
    try:
        payloads.append(
            fetch_insights(
                media_id=media_id,
                metrics=optional,
                access_token=access_token,
                graph_version=graph_version,
                graph_api_root=graph_api_root,
            )
        )
    except SystemExit:
        for metric in optional:
            try:
                payloads.append(
                    fetch_insights(
                        media_id=media_id,
                        metrics=[metric],
                        access_token=access_token,
                        graph_version=graph_version,
                        graph_api_root=graph_api_root,
                    )
                )
            except SystemExit as exc:
                warnings.append(f"{metric}: {exc}")
    merged = merge_insight_payloads(payloads)
    if warnings:
        merged["optional_metric_errors"] = warnings
    return merged, warnings


INSIGHT_METRIC_KEYS = (
    "views",
    "total_views",
    "reach",
    "likes",
    "total_likes",
    "comments",
    "total_comments",
    "saved",
    "shares",
    "total_interactions",
    "ig_reels_video_view_total_time",
    "ig_reels_avg_watch_time",
    "reels_skip_rate",
    "clips_replays_count",
    "facebook_views",
    "crossposted_views",
    "follows",
)
INSTAGRAM_CORE_INSIGHT_METRIC_KEYS = (
    "views",
    "total_views",
    "reach",
    "likes",
    "total_likes",
    "comments",
    "total_comments",
    "saved",
    "shares",
    "total_interactions",
)
INSTAGRAM_OPTIONAL_INSIGHT_METRIC_KEYS = (
    "ig_reels_video_view_total_time",
    "ig_reels_avg_watch_time",
    "reels_skip_rate",
    "clips_replays_count",
    "facebook_views",
    "crossposted_views",
    "follows",
)
INSTAGRAM_DEFAULT_OPTIONAL_INSIGHT_METRIC_KEYS = (
    "ig_reels_video_view_total_time",
    "ig_reels_avg_watch_time",
    "reels_skip_rate",
    "facebook_views",
    "crossposted_views",
)
INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS = (
    *INSTAGRAM_CORE_INSIGHT_METRIC_KEYS,
    *INSTAGRAM_DEFAULT_OPTIONAL_INSIGHT_METRIC_KEYS,
)
DEFAULT_REPORT_SYNC_ACTION_URL = (
    f"http://{DEFAULT_QUEUE_UI_HOST}:{DEFAULT_QUEUE_UI_PORT}/sync-insights"
)
REPORT_TIMEZONE_LABELS = {
    "Asia/Tokyo": "JST",
    "Asia/Manila": "PHT",
    "UTC": "UTC",
}


def coerce_json_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        if not text:
            return None
        parsed = float(text)
    except (TypeError, ValueError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def read_optional_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return read_json(path)
    except SystemExit:
        return None


def source_root_for_clip(clip_dir: Path) -> Path:
    """Return the source video output folder for a reel-app clip directory."""
    if clip_dir.parent.name == "clips":
        return clip_dir.parent.parent
    return clip_dir.parent


def source_transcript_candidates(source_root: Path, lang: str) -> list[Path]:
    names: list[str] = []
    if lang:
        names.append(f"transcript.{lang}.json")
    names.extend(["transcript.en.json", "transcript.json"])
    candidates: list[Path] = []
    seen: set[Path] = set()
    for name in names:
        path = source_root / name
        if path not in seen:
            candidates.append(path)
            seen.add(path)
    return candidates


def transcript_segment_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("segments", "transcript"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def youtube_id_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    host = parsed.netloc.lower().removeprefix("www.")
    path_parts = [part for part in parsed.path.split("/") if part]
    if host in {"youtu.be", "m.youtube.com"} and path_parts:
        return path_parts[0]
    if host.endswith("youtube.com"):
        video_ids = parse_qs(parsed.query).get("v") or []
        if video_ids and video_ids[0]:
            return video_ids[0]
        for marker in ("shorts", "embed", "live"):
            if marker in path_parts:
                index = path_parts.index(marker)
                if index + 1 < len(path_parts):
                    return path_parts[index + 1]
    return ""


def manifest_source_url(manifest: dict[str, Any]) -> str:
    value = re.sub(r"\s+", " ", str(manifest.get("source_url") or "")).strip()
    if value:
        return value
    slides = manifest.get("slides") if isinstance(manifest.get("slides"), list) else []
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        value = re.sub(r"\s+", " ", str(slide.get("source_url") or "")).strip()
        if value:
            return value
    return ""


def manifest_media_path(manifest: dict[str, Any]) -> Path | None:
    slides = manifest.get("slides") if isinstance(manifest.get("slides"), list) else []
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        value = str(slide.get("path") or "").strip()
        if value:
            return Path(value)
    return None


def safe_load_notes(clip_dir: Path | None) -> tuple[dict[str, Any], Path | None]:
    if clip_dir is None or not clip_dir.is_dir():
        return {}, None
    return load_notes(clip_dir)


def reel_media_names(lang: str, channel_id: str, row_media_path: str, manifest_path: Path | None) -> list[str]:
    names = []
    if row_media_path:
        names.append(Path(row_media_path).name)
    if manifest_path is not None:
        names.append(manifest_path.name)
    if lang and channel_id:
        names.append(f"reel.{lang}.{channel_id}.mp4")
    return [name for index, name in enumerate(names) if name and name not in names[:index]]


def is_usable_clip_dir(clip_dir: Path, lang: str, channel_id: str) -> bool:
    if not clip_dir.is_dir():
        return False
    if (clip_dir / "notes.json").exists():
        return True
    if lang and (clip_dir / f"subtitles.{lang}.ass").exists():
        return True
    if lang and channel_id and (clip_dir / f"reel.{lang}.{channel_id}.mp4").exists():
        return True
    return False


def normalize_match_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def resolve_reel_clip_dir(
    *,
    row: Any,
    manifest: dict[str, Any],
    source_url: str,
) -> Path | None:
    lang = str(row["lang"] or "")
    channel_id = str(row["channel_id"] or "")
    raw_clip_dir = str(row["clip_dir"] or "").strip()
    raw_media_path = str(row["media_path"] or "").strip()
    manifest_media = manifest_media_path(manifest)
    direct_candidates: list[Path] = []
    if raw_clip_dir:
        direct_candidates.append(Path(raw_clip_dir))
    if raw_media_path:
        direct_candidates.append(Path(raw_media_path).parent)
    if manifest_media is not None:
        direct_candidates.append(manifest_media.parent)
    for candidate in direct_candidates:
        if is_usable_clip_dir(candidate, lang, channel_id):
            return candidate

    video_id = str(row["source_video"] or "").strip() or youtube_id_from_url(source_url)
    if not video_id:
        return None
    clips_root = DEFAULT_REEL_OUTPUTS.expanduser().resolve() / video_id / "clips"
    if not clips_root.is_dir():
        return None
    names = reel_media_names(lang, channel_id, raw_media_path, manifest_media)
    content_hash = str(row["content_hash"] or "")
    if content_hash and not content_hash.startswith("missing:"):
        for clip_dir in sorted(path for path in clips_root.iterdir() if path.is_dir()):
            for name in names:
                media_path = clip_dir / name
                if media_path.is_file() and reel_ledger.hash_file(media_path) == content_hash:
                    return clip_dir

    title = normalize_match_text(row["title"])
    if title:
        for clip_dir in sorted(path for path in clips_root.iterdir() if path.is_dir()):
            notes, _ = safe_load_notes(clip_dir)
            if normalize_match_text(routed_title(lang, notes, load_one_liners(clip_dir))) == title:
                return clip_dir
    return None


def ass_text_to_plain(text: str) -> str:
    text = re.sub(r"\{[^}]*\}", "", text)
    text = text.replace("\\N", " ").replace("\\n", " ").replace("\\h", " ")
    return re.sub(r"\s+", " ", text).strip()


def read_ass_transcript(path: Path) -> str:
    lines = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.startswith("Dialogue:"):
            continue
        payload = raw_line.removeprefix("Dialogue:").lstrip()
        parts = payload.split(",", 9)
        if len(parts) < 10:
            continue
        text = ass_text_to_plain(parts[9])
        if text:
            lines.append(text)
    return " ".join(lines)


def read_plain_subtitle_transcript(path: Path) -> str:
    lines = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.isdigit() or line.upper() == "WEBVTT" or "-->" in line:
            continue
        lines.append(re.sub(r"\s+", " ", line))
    return " ".join(lines)


def read_reel_transcript_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".ass":
        return read_ass_transcript(path)
    return read_plain_subtitle_transcript(path)


def reel_transcript_candidates(clip_dir: Path, lang: str) -> list[Path]:
    patterns = []
    if lang:
        patterns.extend([
            f"subtitles.{lang}.ass",
            f"subtitles.{lang}.srt",
            f"subtitles.{lang}.vtt",
            f"transcript.{lang}.txt",
        ])
    patterns.extend(["subtitles.en.ass", "subtitles.en.srt", "subtitles.en.vtt", "transcript.txt"])
    candidates = [clip_dir / pattern for pattern in patterns]
    if not any(path.exists() for path in candidates):
        candidates.extend(sorted(clip_dir.glob("subtitles.*.ass")))
    seen: set[Path] = set()
    out = []
    for path in candidates:
        if path not in seen:
            out.append(path)
            seen.add(path)
    return out


def load_reel_transcript(
    *,
    clip_dir: Path | None,
    lang: str,
    notes: dict[str, Any],
) -> tuple[str, str]:
    if clip_dir is not None and clip_dir.is_dir():
        for path in reel_transcript_candidates(clip_dir, lang):
            if not path.is_file():
                continue
            transcript = read_reel_transcript_file(path)
            if transcript:
                return transcript, str(path)
    return str(notes.get("transcript") or "").strip(), ""


def overlapping_transcript_segments(
    *,
    clip_dir: Path,
    lang: str,
    start: int | float | None,
    end: int | float | None,
) -> tuple[str, list[dict[str, Any]]]:
    if start is None or end is None:
        return "", []
    source_root = source_root_for_clip(clip_dir)
    for path in source_transcript_candidates(source_root, lang):
        data = read_optional_json(path)
        segments = transcript_segment_list(data)
        if not segments:
            continue
        selected: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = str(segment.get("text") or "").strip()
            segment_start = coerce_json_number(segment.get("start"))
            segment_end = coerce_json_number(segment.get("end"))
            if segment_start is None or segment_end is None or not text:
                continue
            if float(segment_end) < float(start) or float(segment_start) > float(end):
                continue
            selected.append({"start": segment_start, "end": segment_end, "text": text})
        if selected:
            return str(path), selected
    return "", []


def parse_raw_insight_payload(row: Any) -> Any | None:
    raw = str(row["raw"] or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def row_value(row: Any, key: str) -> Any | None:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def latest_insight_metrics(row: Any) -> dict[str, int | float]:
    """Return scope-safe metrics, repairing legacy snapshots from raw JSON.

    Schema v2 stored combined totals in the visible ``views``, ``likes``, and
    ``comments`` columns. The untouched API payload still contains both scopes,
    so it takes precedence when present. New snapshots persist each field in a
    dedicated column and use the same code path.
    """
    raw_payload = parse_raw_insight_payload(row)
    raw_metrics = parse_insight_metrics(raw_payload) if isinstance(raw_payload, dict) else {}
    metrics: dict[str, int | float] = {}
    for key in INSIGHT_METRIC_KEYS:
        value = raw_metrics.get(key, row_value(row, key))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        metrics[key] = value
    return metrics


def row_insight_metric(row: Any, key: str) -> int | float | None:
    return latest_insight_metrics(row).get(key)


def build_reel_insight_export_item(row: Any) -> dict[str, Any]:
    manifest_path = Path(str(row["manifest_path"] or ""))
    manifest_data = read_optional_json(manifest_path) if str(row["manifest_path"] or "").strip() else None
    manifest = manifest_data if isinstance(manifest_data, dict) else {}
    lang = str(row["lang"] or "")
    source_url = manifest_source_url(manifest)
    clip_dir = resolve_reel_clip_dir(row=row, manifest=manifest, source_url=source_url)
    notes, notes_path = safe_load_notes(clip_dir)
    source_metadata = load_source_metadata(clip_dir.parent) if clip_dir is not None else {}
    if not source_metadata:
        video_id = str(row["source_video"] or "").strip() or youtube_id_from_url(source_url)
        metadata = read_optional_json(DEFAULT_REEL_OUTPUTS.expanduser().resolve() / video_id / "metadata.json") if video_id else None
        source_metadata = metadata if isinstance(metadata, dict) else {}
    source_url = source_url or source_metadata_value(source_metadata, "webpage_url", "original_url", "url")
    start = coerce_json_number(notes.get("start"))
    end = coerce_json_number(notes.get("end"))
    reel_transcript, reel_transcript_path = load_reel_transcript(
        clip_dir=clip_dir,
        lang=lang,
        notes=notes,
    )
    source_transcript_path, source_segments = (
        overlapping_transcript_segments(
            clip_dir=clip_dir,
            lang=lang,
            start=start,
            end=end,
        )
        if clip_dir is not None
        else ("", [])
    )
    item = {
        "content_hash": str(row["content_hash"] or ""),
        "channel_id": str(row["channel_id"] or ""),
        "lang": lang,
        "title": str(row["title"] or ""),
        "caption": str(row["caption"] or ""),
        "published_at": str(row["published_at"] or ""),
        "scheduled_at": str(row["scheduled_at"] or ""),
        "permalink": str(row["permalink"] or ""),
        "media_id": str(row["media_id"] or ""),
        "media_path": str(row["media_path"] or ""),
        "manifest_path": str(row["manifest_path"] or ""),
        "insights": {
            "captured_at": str(row["captured_at"] or ""),
            "has_snapshot": bool(row["captured_at"]),
            "metrics": latest_insight_metrics(row),
            "raw_api_payload": parse_raw_insight_payload(row),
        },
        "source": {
            "video_id": str(row["source_video"] or ""),
            "title": source_metadata_value(source_metadata, "title"),
            "uploader": source_metadata_value(source_metadata, "uploader", "channel"),
            "url": source_url,
        },
        "segment": {
            "clip_dir": str(clip_dir or ""),
            "notes_path": str(notes_path or ""),
            "start": start,
            "end": end,
            "duration": coerce_json_number(notes.get("duration")),
            "score": coerce_json_number(notes.get("score")),
            "source_chapter": note_text(notes, "source_chapter"),
            "one_liner": note_text(notes, "one_liner"),
            "one_liner_translated": note_text(notes, "one_liner_translated"),
            "reason": note_text(notes, "reason"),
            "transcript": str(notes.get("transcript") or "").strip(),
            "reel_transcript": reel_transcript,
            "reel_transcript_path": reel_transcript_path,
            "source_transcript_path": source_transcript_path,
            "source_transcript_segments": source_segments,
        },
    }
    if row_trial_enabled(row):
        item["trial_reel"] = True
        item["trial_graduation_strategy"] = row_trial_strategy(row)
    return item


def build_insights_export(
    *,
    insight_rows: list[Any],
    db_path: Path,
    platform: str,
    channel_filter: str | None,
) -> dict[str, Any]:
    return {
        "generated_at": utc_now(),
        "platform": platform,
        "channel_filter": channel_filter or "",
        "db_path": str(db_path),
        "metric_scopes": {
            "instagram": ["views", "reach", "likes", "comments", "saved", "shares"],
            "meta_all_surfaces": ["total_views", "total_likes", "total_comments"],
            "retention": [
                "ig_reels_video_view_total_time",
                "ig_reels_avg_watch_time",
                "reels_skip_rate",
                "clips_replays_count",
            ],
            "cross_surface_diagnostics": ["facebook_views", "crossposted_views"],
            "definitions": {
                "total_views": "Plays or displays across all surfaces.",
                "facebook_views": (
                    "Facebook plays, including crossposted and recommended plays."
                ),
                "crossposted_views": "Plays aggregated across Instagram and Facebook.",
            },
            "warning": (
                "Instagram, Facebook, crossposted, and Meta all-surface view metrics can overlap; "
                "never add them together."
            ),
        },
        "prompt": (
            "Review these published reels with metric scopes kept separate: views/likes/comments "
            "are Instagram, total_views/total_likes/total_comments are Meta all-surface totals, "
            "crossposted_views is the explicit Instagram-plus-Facebook aggregate when available, "
            "and these scopes must never be added because they can overlap. "
            "Use fixed-age snapshots, retention metrics when present, source metadata, and "
            "segment.reel_transcript to recommend what to scale, iterate, or stop."
        ),
        "items": [build_reel_insight_export_item(row) for row in insight_rows],
    }


def report_json_path(report_out: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    return report_out.expanduser().resolve().with_suffix(".insights.json")


def report_markdown_path(report_out: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    return report_out.expanduser().resolve().with_suffix(".insights.md")


def report_href(target: Path, html_path: Path) -> str:
    try:
        return os.path.relpath(target.expanduser().resolve(), html_path.expanduser().resolve().parent)
    except ValueError:
        return str(target)


def load_report_data(
    *,
    db_path: Path,
    channel_filter: str | None,
    limit: int | None,
) -> tuple[dict[str, dict[str, int]], list[Any], list[Any], list[Any]]:
    with reel_ledger.connect(db_path) as conn:
        counts = reel_ledger.status_counts(conn, channel_filter)
        upcoming = reel_ledger.upcoming(conn, channel_filter, limit=limit)
        published = reel_ledger.recent_published(conn, channel_filter, limit=limit)
        insight_rows = reel_ledger.latest_insight_rows(conn, channel_filter, limit=limit)
    return counts, upcoming, published, insight_rows


def report_channel_timezone(
    channel_id: Any,
    *,
    platform: str,
    cache: dict[str, str],
) -> str:
    channel_key = str(channel_id or "").strip()
    if not channel_key:
        return DEFAULT_TIMEZONE
    if channel_key not in cache:
        try:
            settings = reel_settings(load_channel(channel_key), settings_key_for(platform))
            cache[channel_key] = setting_text(settings, "timezone", DEFAULT_TIMEZONE)
        except (SystemExit, ValueError):
            cache[channel_key] = DEFAULT_TIMEZONE
    return cache[channel_key]


def readable_report_datetime(value: Any, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    tz = timezone_for(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    local = parsed.astimezone(tz)
    hour = local.hour % 12 or 12
    minute = f":{local.minute:02d}" if local.minute else ""
    period = "AM" if local.hour < 12 else "PM"
    label = REPORT_TIMEZONE_LABELS.get(timezone_name) or local.tzname() or timezone_name
    return f"{local.strftime('%B')} {local.day}, {hour}{minute}{period} {label}"


def markdown_cell(value: Any) -> str:
    text = str(value or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("|", "\\|")
    return text.replace("\n", "<br>")


def markdown_number(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return ""


def truncate_text(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def item_metric(item: dict[str, Any], key: str) -> Any:
    insights = item.get("insights") if isinstance(item.get("insights"), dict) else {}
    metrics = insights.get("metrics") if isinstance(insights.get("metrics"), dict) else {}
    return metrics.get(key)


def item_segment(item: dict[str, Any]) -> dict[str, Any]:
    segment = item.get("segment")
    return segment if isinstance(segment, dict) else {}


def item_source(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source")
    return source if isinstance(source, dict) else {}


def row_trial_enabled(row: Any) -> bool:
    try:
        return bool(row["trial_reel"])
    except (KeyError, IndexError, TypeError):
        return False


def row_trial_strategy(row: Any) -> str:
    try:
        return str(row["trial_graduation_strategy"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def row_publish_type(row: Any) -> tuple[str, str]:
    if not row_trial_enabled(row):
        return "Regular", "Regular Reel"
    strategy = row_trial_strategy(row)
    detail = f"Trial Reel ({strategy})" if strategy else "Trial Reel"
    return "Trial", detail


def item_hook(item: dict[str, Any]) -> str:
    segment = item_segment(item)
    return (
        str(item.get("title") or "").strip()
        or str(segment.get("one_liner_translated") or "").strip()
        or str(segment.get("one_liner") or "").strip()
    )


def item_transcript(item: dict[str, Any]) -> str:
    segment = item_segment(item)
    reel_transcript = str(segment.get("reel_transcript") or "").strip()
    if reel_transcript:
        return reel_transcript
    transcript = str(segment.get("transcript") or "").strip()
    if transcript:
        return transcript
    segments = segment.get("source_transcript_segments")
    if isinstance(segments, list):
        return " ".join(
            str(part.get("text") or "").strip()
            for part in segments
            if isinstance(part, dict) and str(part.get("text") or "").strip()
        )
    return ""


def item_transcript_path(item: dict[str, Any]) -> str:
    segment = item_segment(item)
    return str(segment.get("reel_transcript_path") or "").strip()


def render_insights_markdown(export: dict[str, Any], *, max_transcript_chars: int = 0) -> str:
    items = export.get("items") if isinstance(export.get("items"), list) else []
    lines = [
        "# Reel Insights",
        "",
        f"- Generated: {markdown_cell(export.get('generated_at'))}",
        f"- Platform: {markdown_cell(export.get('platform'))}",
        f"- Items: {len(items)}",
        "",
        "| # | Published | Channel | Reel | Instagram views | Meta all-surface views | Instagram reach | Instagram likes | Meta all-surface likes | Instagram comments | Meta all-surface comments | Saved | Shares | Interactions | Avg watch time (ms) | Skip rate | Facebook views | IG + Facebook crossposted views | Follows | Hook | Reel Transcript |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for index, raw_item in enumerate(items, start=1):
        if not isinstance(raw_item, dict):
            continue
        permalink = str(raw_item.get("permalink") or "").strip()
        reel_link = f"[reel]({permalink})" if permalink else ""
        transcript = truncate_text(item_transcript(raw_item), max_transcript_chars)
        row = [
            str(index),
            markdown_cell(raw_item.get("published_at")),
            markdown_cell(raw_item.get("channel_id")),
            reel_link,
            markdown_number(item_metric(raw_item, "views")),
            markdown_number(item_metric(raw_item, "total_views")),
            markdown_number(item_metric(raw_item, "reach")),
            markdown_number(item_metric(raw_item, "likes")),
            markdown_number(item_metric(raw_item, "total_likes")),
            markdown_number(item_metric(raw_item, "comments")),
            markdown_number(item_metric(raw_item, "total_comments")),
            markdown_number(item_metric(raw_item, "saved")),
            markdown_number(item_metric(raw_item, "shares")),
            markdown_number(item_metric(raw_item, "total_interactions")),
            markdown_number(item_metric(raw_item, "ig_reels_avg_watch_time")),
            markdown_number(item_metric(raw_item, "reels_skip_rate")),
            markdown_number(item_metric(raw_item, "facebook_views")),
            markdown_number(item_metric(raw_item, "crossposted_views")),
            markdown_number(item_metric(raw_item, "follows")),
            markdown_cell(item_hook(raw_item)),
            markdown_cell(transcript),
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def write_insights_markdown(
    *,
    export: dict[str, Any],
    out_path: Path,
    max_transcript_chars: int = 0,
) -> None:
    out_path.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    out_path.expanduser().resolve().write_text(
        render_insights_markdown(export, max_transcript_chars=max_transcript_chars),
        encoding="utf-8",
    )


def render_report_html(
    *,
    counts: dict[str, dict[str, int]],
    upcoming: list[Any],
    published: list[Any],
    insight_rows: list[Any],
    db_path: Path,
    platform: str = DEFAULT_PLATFORM,
    sync_action_url: str = "",
    insights_json_href: str = "",
    insights_markdown_href: str = "",
    message: str = "",
    error: str = "",
) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    timezone_cache: dict[str, str] = {}

    def row_time(row: Any, key: str) -> str:
        return readable_report_datetime(
            row[key],
            report_channel_timezone(row["channel_id"], platform=platform, cache=timezone_cache),
        )

    order = [
        reel_ledger.STATUS_NEW,
        reel_ledger.STATUS_SCHEDULED,
        reel_ledger.STATUS_PUBLISHING,
        reel_ledger.STATUS_PREVIEWED,
        reel_ledger.STATUS_PUBLISHED,
        reel_ledger.STATUS_FAILED,
        reel_ledger.STATUS_SKIPPED,
    ]
    channel_ids = sorted(
        {str(channel_id or "").strip() for channel_id in counts}
        | {
            str(row["channel_id"] or "").strip()
            for row in [*upcoming, *published, *insight_rows]
        }
    ) or [""]
    show_publish_type = any(row_trial_enabled(row) for row in [*upcoming, *published, *insight_rows])
    label_cache: dict[str, str] = {}

    def channel_anchor(channel_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "-", channel_id).strip("-").lower()
        return f"channel-{safe or 'all'}"

    def channel_label(channel_id: str) -> str:
        if not channel_id:
            return "All Channels"
        if channel_id not in label_cache:
            try:
                channel = load_channel(channel_id)
                label = channel.brand_name or channel.account_name or channel_id
            except (SystemExit, ValueError):
                label = channel_id
            label_cache[channel_id] = f"{label} ({channel_id})" if label != channel_id else channel_id
        return label_cache[channel_id]

    def rows_for_channel(rows: list[Any], channel_id: str) -> list[Any]:
        return [row for row in rows if str(row["channel_id"] or "").strip() == channel_id]

    def status_body(channel_id: str) -> str:
        per_channel = counts.get(channel_id, {})
        cells = "".join(f"<td>{per_channel.get(status, 0)}</td>" for status in order)
        return f"<tr>{cells}</tr>"

    def publish_type_badge(row: Any) -> str:
        label, detail = row_publish_type(row)
        class_name = "trial" if label == "Trial" else "regular"
        return (
            f"<span class=\"post-type {class_name}\" title=\"{esc(detail)}\">"
            f"{esc(label)}</span>"
        )

    def publish_type_cell(row: Any) -> str:
        return f"<td>{publish_type_badge(row)}</td>" if show_publish_type else ""

    def upcoming_body_for(channel_id: str) -> str:
        rows = [
            "<tr>"
            f"<td>{esc(row_time(row, 'scheduled_at'))}</td>"
            f"{publish_type_cell(row)}"
            f"<td>{esc(row['status'])}</td>"
            f"<td>{esc(row['title'] or row['clip_dir'])}</td>"
            "</tr>"
            for row in rows_for_channel(upcoming, channel_id)
        ]
        colspan = 4 if show_publish_type else 3
        return "".join(rows) or f'<tr><td colspan="{colspan}">No queued reels</td></tr>'

    def published_body_for(channel_id: str) -> str:
        rows = [
            "<tr>"
            f"<td>{esc(row_time(row, 'published_at'))}</td>"
            f"{publish_type_cell(row)}"
            f"<td><a href=\"{esc(row['permalink'])}\">{esc(row['permalink'] or row['media_id'])}</a></td>"
            "</tr>"
            for row in rows_for_channel(published, channel_id)
        ]
        colspan = 3 if show_publish_type else 2
        return "".join(rows) or f'<tr><td colspan="{colspan}">No published reels</td></tr>'

    def insight_body_for(channel_id: str) -> str:
        rows = [
            "<tr>"
            f"<td>{esc(row_time(row, 'published_at'))}</td>"
            f"{publish_type_cell(row)}"
            f"<td>{esc(row['title'])}</td>"
            f"<td>{esc(row_insight_metric(row, 'views'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'total_views'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'reach'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'likes'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'total_likes'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'comments'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'total_comments'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'saved'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'shares'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'total_interactions'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'ig_reels_avg_watch_time'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'reels_skip_rate'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'facebook_views'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'crossposted_views'))}</td>"
            f"<td>{esc(row_insight_metric(row, 'follows'))}</td>"
            f"<td>{esc(row_time(row, 'captured_at') or 'not synced')}</td>"
            "</tr>"
            for row in rows_for_channel(insight_rows, channel_id)
        ]
        colspan = 19 if show_publish_type else 18
        return "".join(rows) or f'<tr><td colspan="{colspan}">No published reels</td></tr>'
    platform_label = "Instagram" if platform == "instagram" else platform.title()
    sync_form = (
        "<form method=\"post\" "
        f"action=\"{esc(sync_action_url)}\" "
        "onsubmit=\"return confirm('Fetch latest insights from the platform API now?');\">"
        f"<button type=\"submit\">Update {esc(platform_label)} Insights</button>"
        "</form>"
        if sync_action_url
        else ""
    )
    json_link = (
        f"<a class=\"json-link\" href=\"{esc(insights_json_href)}\">LLM JSON</a>"
        if insights_json_href
        else ""
    )
    markdown_link = (
        f"<a class=\"json-link\" href=\"{esc(insights_markdown_href)}\">Markdown Table</a>"
        if insights_markdown_href
        else ""
    )
    actions = (
        f"<div class=\"actions\">{sync_form}{json_link}{markdown_link}</div>"
        if sync_form or json_link or markdown_link
        else ""
    )
    message_html = f"<div class=\"notice ok\">{esc(message)}</div>" if message else ""
    error_html = f"<div class=\"notice error\">{esc(error)}</div>" if error else ""
    status_headers = "".join(f"<th>{esc(status)}</th>" for status in order)
    type_header = "<th>Type</th>" if show_publish_type else ""
    channel_nav = (
        '<nav class="channel-nav" aria-label="Channel views">'
        + "".join(
            f"<a class=\"channel-link\" href=\"#{esc(channel_anchor(channel_id))}\">{esc(channel_label(channel_id))}</a>"
            for channel_id in channel_ids
        )
        + "</nav>"
        if len(channel_ids) > 1
        else ""
    )
    channel_sections = "\n".join(
        f"""  <section class="channel-section" id="{esc(channel_anchor(channel_id))}">
    <h2>{esc(channel_label(channel_id))}</h2>
    <h3>Status Counts</h3>
    <table>
      <thead><tr>{status_headers}</tr></thead>
      <tbody>{status_body(channel_id)}</tbody>
    </table>
    <h3>Upcoming Queue</h3>
    <table>
      <thead><tr><th>Scheduled</th>{type_header}<th>Status</th><th>Title</th></tr></thead>
      <tbody>{upcoming_body_for(channel_id)}</tbody>
    </table>
    <h3>Recently Published</h3>
    <table>
      <thead><tr><th>Published</th>{type_header}<th>Permalink</th></tr></thead>
      <tbody>{published_body_for(channel_id)}</tbody>
    </table>
    <h3>Latest Insights</h3>
    <p><strong>Metric scope:</strong> Instagram views/reach are Instagram-only;
      Meta all-surface totals cover all surfaces; IG + Facebook crossposted views
      are the explicit two-platform aggregate when available. These fields overlap,
      so never add them together.</p>
    <table>
      <thead><tr><th>Published</th>{type_header}<th>Title</th><th>Instagram views</th><th>Meta all-surface views</th><th>Instagram reach</th><th>Instagram likes</th><th>Meta all-surface likes</th><th>Instagram comments</th><th>Meta all-surface comments</th><th>Saved</th><th>Shares</th><th>Interactions</th><th>Avg watch time (ms)</th><th>Skip rate</th><th>Facebook views</th><th>IG + Facebook crossposted views</th><th>Follows</th><th>Captured</th></tr></thead>
      <tbody>{insight_body_for(channel_id)}</tbody>
    </table>
 </section>"""
        for channel_id in channel_ids
    )
    publish_type_css = (
        """
    .post-type { display: inline-block; border-radius: 999px; padding: 3px 8px; font-size: 12px; font-weight: 800; line-height: 1.2; white-space: nowrap; }
    .post-type.trial { color: #0f5132; background: #d1e7dd; border: 1px solid #badbcc; }
    .post-type.regular { color: #41464b; background: #e2e3e5; border: 1px solid #d3d6d8; }"""
        if show_publish_type
        else ""
    )
    generated_at = readable_report_datetime(utc_now(), DEFAULT_TIMEZONE)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reel Ledger Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #151515; background: #fafafa; }}
    h1 {{ font-size: 28px; margin: 0 0 8px; }}
    h2 {{ font-size: 22px; margin: 0 0 12px; }}
    h3 {{ font-size: 15px; margin: 22px 0 8px; }}
    .meta {{ color: #666; margin-bottom: 24px; }}
    .top {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }}
    .actions {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    button, .json-link {{ border: 1px solid #145cc7; border-radius: 6px; background: #145cc7; color: white; padding: 8px 11px; font-weight: 700; text-decoration: none; cursor: pointer; }}
    .json-link {{ background: white; color: #145cc7; }}
    .channel-nav {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 24px; }}
    .channel-link {{ border: 1px solid #c9d6e8; border-radius: 6px; background: white; color: #145cc7; padding: 7px 10px; font-weight: 700; text-decoration: none; }}
    .channel-section {{ margin-top: 30px; }}
    .channel-section + .channel-section {{ border-top: 2px solid #ddd; padding-top: 28px; }}
    .notice {{ margin: 12px 0; border: 1px solid #d2d2d2; border-radius: 6px; padding: 10px 12px; background: white; }}
    .notice.ok {{ border-color: rgba(20, 92, 199, 0.35); color: #145cc7; }}
    .notice.error {{ border-color: rgba(169, 53, 39, 0.45); color: #a93527; }}
    table {{ border-collapse: collapse; width: 100%; background: white; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f0f0f0; }}
    a {{ color: #145cc7; }}
{publish_type_css}
  </style>
</head>
<body>
  <div class="top">
    <div>
      <h1>Reel Ledger Report</h1>
      <div class="meta">Generated {esc(generated_at)} from {esc(db_path)}</div>
    </div>
    {actions}
  </div>
  {message_html}
  {error_html}
  {channel_nav}
{channel_sections}
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
    run_due.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
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
    scan.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")

    plan_ledger = subparsers.add_parser(
        "plan-ledger", help="Scan multi-channel clips and assign new ledger rows to per-channel slots"
    )
    plan_ledger.add_argument("clips_dir", type=Path, help="reel-app clips folder (multi-channel)")
    plan_ledger.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Publishing platform (default: instagram)"
    )
    plan_ledger.add_argument("date", nargs="?", help="Optional first eligible date (YYYY-MM-DD) or ISO datetime")
    plan_ledger.add_argument("--channel", help="Limit planning to one channel id")
    plan_ledger.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
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
    queue_outputs.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
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
    queue_outputs.add_argument(
        "--reshuffle-only-if-new",
        action="store_true",
        help="With --mode reshuffle, reflow the unpublished queue only when new rows were planned",
    )
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
    reflow.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
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
    alternate_sources.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    alternate_sources.add_argument("--out", type=Path, help="Write a .md or .json dry-run/apply preview")
    alternate_sources.add_argument("--apply", action="store_true", help="Actually update queued rows")

    refresh_captions = subparsers.add_parser(
        "refresh-captions",
        help="Rebuild queued ledger captions from current channel settings",
    )
    refresh_captions.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which platform caption settings to use"
    )
    refresh_captions.add_argument("--channel", help="Limit to one channel id")
    refresh_captions.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    refresh_captions.add_argument("--limit", type=int, help="Maximum queued rows to inspect")
    refresh_captions.add_argument("--out", type=Path, help="Write a .md or .json preview of changed captions")
    refresh_captions.add_argument("--apply", action="store_true", help="Actually update queued rows and manifests")

    audit_queue = subparsers.add_parser(
        "audit-queue",
        help="Write a read-only growth audit for queued ledger reels",
    )
    audit_queue.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to audit (default: instagram)"
    )
    audit_queue.add_argument("--channel", help="Limit to one channel id")
    audit_queue.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    audit_queue.add_argument("--limit", type=int, help="Maximum queued rows to inspect")
    audit_queue.add_argument("--out", type=Path, default=ROOT / "out" / "queue_growth_audit.md")

    importer = subparsers.add_parser(
        "import-schedules", help="Seed the ledger from existing schedule.json files"
    )
    importer.add_argument("--root", type=Path, default=DEFAULT_OUT, help="Folder of <schedule>/schedule.json dirs")
    importer.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")

    status = subparsers.add_parser(
        "status", help="Summarize ledger counts, upcoming posts, and recent publishes"
    )
    status.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to read (default: instagram)"
    )
    status.add_argument("--channel", help="Limit to one channel id")
    status.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    status.add_argument("--limit", type=int, default=15, help="Max upcoming rows to show")

    queue_ui = subparsers.add_parser(
        "queue-ui",
        help="Serve a local queue review UI with remove buttons for unpublished scheduled posts",
    )
    queue_ui.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to review (default: instagram)"
    )
    queue_ui.add_argument("--channel", help="Limit to one channel id")
    queue_ui.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    queue_ui.add_argument("--host", default=DEFAULT_QUEUE_UI_HOST, help="Host interface to bind")
    queue_ui.add_argument("--port", type=int, default=DEFAULT_QUEUE_UI_PORT, help="Port to listen on")
    queue_ui.add_argument("--limit", type=int, default=200, help="Max queued rows to show")
    queue_ui.add_argument("--report-out", type=Path, default=ROOT / "out" / "reel_report.html")
    queue_ui.add_argument(
        "--outputs-root",
        type=Path,
        default=DEFAULT_REEL_OUTPUTS,
        help=f"Folder containing <youtube_id>/clips dirs for the reshuffle button (default: {DEFAULT_REEL_OUTPUTS})",
    )
    queue_ui.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT / "ledger",
        help="Folder for generated per-reel manifests when the button plans new rows",
    )
    queue_ui.add_argument("--limit-per-channel", type=int, help="Maximum new rows to schedule per channel from the button")
    queue_ui.add_argument("--jitter-minutes", type=int, help="Override channel jitter for button reshuffles")

    cleanup = subparsers.add_parser(
        "cleanup-missing", help="Dry-run/delete ledger rows whose media file no longer exists"
    )
    cleanup.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="Which ledger to clean (default: instagram)"
    )
    cleanup.add_argument("--channel", help="Limit to one channel id")
    cleanup.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    cleanup.add_argument("--all-statuses", action="store_true", help="Consider every status, not just failed rows")
    cleanup.add_argument("--apply", action="store_true", help="Actually delete missing-media rows")

    sync = subparsers.add_parser("sync-insights", help="Fetch insights for published ledger rows")
    sync.add_argument(
        "--platform", choices=sorted(PLATFORMS), default=None, help="instagram (Graph) or tiktok (video.list)"
    )
    sync.add_argument("--channel", help="Limit to one channel id")
    sync.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    sync.add_argument("--limit", type=int, help="Maximum published rows to sync")
    sync.add_argument(
        "--media-id",
        action="append",
        default=None,
        help="Sync one exact published Instagram media id; repeat for multiple ids",
    )
    sync.add_argument("--dry-run", action="store_true", help="Print rows that would be synced")
    sync.add_argument(
        "--metrics",
        default=",".join(INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS),
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
    report.add_argument("--db", type=Path, default=None, help="Ledger db path (default: platform db)")
    report.add_argument("--out", type=Path, default=ROOT / "out" / "reel_report.html")
    report.add_argument("--limit", type=int, default=0, help="Rows per report table; 0 means all")
    report.add_argument(
        "--insights-json-out",
        type=Path,
        default=None,
        help="LLM-ready insights/transcript JSON path (default: beside the HTML report)",
    )
    report.add_argument(
        "--insights-md-out",
        type=Path,
        default=None,
        help="Readable Markdown insights table path (default: beside the HTML report)",
    )
    report.add_argument(
        "--max-transcript-chars",
        type=int,
        default=0,
        help="Truncate transcript cells in the Markdown table; 0 keeps full transcripts",
    )
    report.add_argument(
        "--sync-action-url",
        default=DEFAULT_REPORT_SYNC_ACTION_URL,
        help="POST URL used by the report's update-insights button",
    )

    insights_md = subparsers.add_parser("insights-md", help="Convert an insights JSON export to Markdown")
    insights_md.add_argument(
        "json_path",
        nargs="?",
        type=Path,
        default=ROOT / "out" / "reel_report.insights.json",
        help="Insights JSON export path",
    )
    insights_md.add_argument("--out", type=Path, default=None, help="Markdown output path")
    insights_md.add_argument(
        "--max-transcript-chars",
        type=int,
        default=0,
        help="Truncate transcript cells; 0 keeps full transcripts",
    )
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
    if args.limit_per_channel is not None and args.limit_per_channel <= 0:
        raise SystemExit("--limit-per-channel must be greater than zero")
    if args.jitter_minutes is not None and args.jitter_minutes < 0:
        raise SystemExit("--jitter-minutes must be zero or greater")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    settings_key = settings_key_for(platform)
    if args.mode == "reshuffle":
        result = scan_and_reshuffle_outputs(
            db_path=db_path,
            outputs_root=args.outputs_root,
            out_dir=args.out_dir,
            channel_filter=args.channel,
            platform=platform,
            settings_key=settings_key,
            limit_per_channel=args.limit_per_channel,
            jitter_minutes=args.jitter_minutes,
            start_at_text=args.start_at,
            after_text=args.after,
            only_if_planned=args.reshuffle_only_if_new,
        )
        planned = result["planned"]
    else:
        result = scan_and_plan_outputs(
            db_path=db_path,
            outputs_root=args.outputs_root,
            out_dir=args.out_dir,
            channel_filter=args.channel,
            platform=platform,
            settings_key=settings_key,
            limit_per_channel=args.limit_per_channel,
            jitter_minutes=args.jitter_minutes,
            start_at_text=args.start_at,
        )
        planned = result["planned"]

    if result["clips_dirs"] == 0:
        print(f"[reel-scheduler] no output clips folders found in {args.outputs_root}")
        return 0
    print(f"[reel-scheduler] scanned {result['clips_dirs']} output folder(s)")
    if planned:
        for channel_id in sorted(planned):
            print(f"[reel-scheduler] planned {planned[channel_id]} new {channel_id} {platform} reel(s)")
    else:
        print("[reel-scheduler] no new ledger rows to schedule")
    if args.mode == "reshuffle":
        print(
            f"[reel-scheduler] reshuffled queue from {result['start_at']}: "
            f"channels={len(result['reflowed'])} alternated={result['alternated']}"
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
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    alternate_source_queue_rows(
        db_path=db_path,
        after_text=args.after,
        channel_filter=args.channel,
        apply=args.apply,
        preview_out=args.out,
        settings_key=settings_key_for(platform),
    )
    print(f"[reel-scheduler] ledger: {db_path}")
    return 0


def refresh_captions_command(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    refresh_queued_captions(
        db_path=db_path,
        channel_filter=args.channel,
        settings_key=settings_key_for(platform),
        apply=args.apply,
        limit=args.limit,
        preview_out=args.out,
        platform=platform,
    )
    print(f"[reel-scheduler] ledger: {db_path}")
    return 0


def audit_queue_command(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    audit = build_queue_growth_audit(
        db_path=db_path,
        channel_filter=args.channel,
        platform=platform,
        limit=args.limit,
    )
    write_queue_growth_audit(args.out, audit)
    print(f"[reel-scheduler] wrote queue growth audit -> {args.out.expanduser().resolve()}")
    print(f"[reel-scheduler] queued={audit['queued_count']} warnings={len(audit['warnings'])}")
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
    if args.limit_per_channel is not None and args.limit_per_channel <= 0:
        raise SystemExit("--limit-per-channel must be greater than zero")
    if args.jitter_minutes is not None and args.jitter_minutes < 0:
        raise SystemExit("--jitter-minutes must be zero or greater")
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    handler = make_queue_ui_handler(
        db_path=db_path,
        channel_filter=args.channel,
        limit=args.limit,
        settings_key=settings_key_for(platform),
        platform=platform,
        report_out=args.report_out,
        outputs_root=args.outputs_root,
        out_dir=args.out_dir,
        limit_per_channel=args.limit_per_channel,
        jitter_minutes=args.jitter_minutes,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    public_host = DEFAULT_QUEUE_UI_HOST if args.host in {"", "0.0.0.0"} else args.host
    url = f"http://{public_host}:{args.port}/"
    print(f"[reel-scheduler] queue UI: {url}", flush=True)
    print(f"[reel-scheduler] ledger: {db_path}", flush=True)
    print(f"[reel-scheduler] outputs: {args.outputs_root}", flush=True)
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
    platform = resolve_platform(args)
    media_ids = getattr(args, "media_id", None)
    if media_ids and platform != "instagram":
        raise SystemExit("--media-id is currently supported only for Instagram insights")
    if platform == "tiktok":
        return sync_tiktok_insights(args, db_path)
    if platform == "facebook":
        raise SystemExit("Facebook insights sync is not implemented; use report/status for this ledger")
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
    requested_media_ids = tuple(
        dict.fromkeys(
            str(media_id).strip()
            for media_id in (getattr(args, "media_id", None) or ())
            if str(media_id).strip()
        )
    )
    with reel_ledger.connect(db_path) as conn:
        rows = reel_ledger.published_reels_for_insights(
            conn,
            args.channel,
            args.limit,
            media_ids=requested_media_ids if requested_media_ids else None,
        )
    if not rows:
        print("[reel-scheduler] no published media ids to sync")
        return 1 if requested_media_ids else 0
    found_media_ids = {str(row["media_id"] or "") for row in rows}
    missing_media_ids = sorted(set(requested_media_ids) - found_media_ids)
    for media_id in missing_media_ids:
        print(f"[reel-scheduler] skip {media_id}: exact published media id not found")
    synced = 0
    skipped = len(missing_media_ids)
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
        try:
            payload, metric_warnings = fetch_instagram_insights_resilient(
                media_id=str(row["media_id"]),
                metrics=metrics,
                access_token=access_token,
                graph_version=graph_version,
                graph_api_root=graph_root,
            )
        except SystemExit as exc:
            skipped += 1
            print(f"[reel-scheduler] skip {row['channel_id']} {row['media_id']}: {exc}")
            continue
        for warning in metric_warnings:
            print(
                f"[reel-scheduler] optional metric unavailable "
                f"{row['channel_id']} {row['media_id']}: {warning}"
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
    if getattr(args, "max_transcript_chars", 0) < 0:
        raise SystemExit("--max-transcript-chars must be zero or greater")
    limit = None if args.limit == 0 else args.limit
    platform = resolve_platform(args)
    db_path = resolve_db(args)
    out_path = args.out.expanduser().resolve()
    channel_filter = getattr(args, "channel", None)
    counts, upcoming, published, insight_rows = load_report_data(
        db_path=db_path,
        channel_filter=channel_filter,
        limit=limit,
    )
    json_out = report_json_path(out_path, getattr(args, "insights_json_out", None))
    md_out = report_markdown_path(out_path, getattr(args, "insights_md_out", None))
    export = build_insights_export(
        insight_rows=insight_rows,
        db_path=db_path,
        platform=platform,
        channel_filter=channel_filter,
    )
    write_json(json_out, export)
    write_insights_markdown(
        export=export,
        out_path=md_out,
        max_transcript_chars=getattr(args, "max_transcript_chars", 0),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_report_html(
        counts=counts,
        upcoming=upcoming,
        published=published,
        insight_rows=insight_rows,
        db_path=db_path,
        platform=platform,
        sync_action_url=getattr(args, "sync_action_url", DEFAULT_REPORT_SYNC_ACTION_URL),
        insights_json_href=report_href(json_out, out_path),
        insights_markdown_href=report_href(md_out, out_path),
    )
    out_path.write_text(html_text, encoding="utf-8")
    print(f"[reel-scheduler] wrote report -> {out_path}")
    print(f"[reel-scheduler] wrote insights JSON -> {json_out}")
    print(f"[reel-scheduler] wrote insights Markdown -> {md_out}")
    return 0


def insights_markdown_command(args: argparse.Namespace) -> int:
    if args.max_transcript_chars < 0:
        raise SystemExit("--max-transcript-chars must be zero or greater")
    data = read_json(args.json_path.expanduser().resolve())
    if not isinstance(data, dict):
        raise SystemExit("Insights JSON must be an object")
    out_path = (
        args.out.expanduser().resolve()
        if args.out is not None
        else args.json_path.expanduser().resolve().with_suffix(".md")
    )
    write_insights_markdown(
        export=data,
        out_path=out_path,
        max_transcript_chars=args.max_transcript_chars,
    )
    print(f"[reel-scheduler] wrote insights Markdown -> {out_path}")
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
    if args.command == "refresh-captions":
        return refresh_captions_command(args)
    if args.command == "audit-queue":
        return audit_queue_command(args)
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
    if args.command == "insights-md":
        return insights_markdown_command(args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
