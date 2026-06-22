#!/usr/bin/env python3
"""Plan and publish channel-aware Instagram reels from a clips folder.

``plan`` scans clip directories for the channel's configured media filename,
writes one Instagram publisher manifest per reel, and assigns publish times.
``run-due`` is intended for cron or another periodic runner; it publishes only
jobs whose scheduled time has arrived and persists their status after each run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from channel import Channel, load_channel

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "out" / "reel_schedules"
DEFAULT_MEDIA_FILENAME = "reel.mp4"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_PUBLISH_TIME = "09:00"
SCHEDULE_VERSION = 1

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


def reel_settings(channel: Channel) -> dict[str, Any]:
    publishing = channel.publishing if isinstance(channel.publishing, dict) else {}
    settings = publishing.get("instagram_reels")
    return settings if isinstance(settings, dict) else {}


def setting_text(settings: dict[str, Any], key: str, default: str) -> str:
    value = str(settings.get(key) or "").strip()
    return value or default


def timezone_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Unknown timezone: {name}") from exc


def parse_datetime(value: str, timezone_name: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SystemExit(
            f"Invalid date/time '{value}'. Use ISO 8601, for example 2026-06-23T09:00:00+09:00."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone_for(timezone_name))
    return parsed


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
) -> tuple[str, list[str]]:
    settings = reel_settings(channel)
    title = clip_title(channel, clip_dir, notes)
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
) -> dict[str, Any]:
    title = clip_title(channel, clip_dir, notes)
    source_url = source_metadata_value(source_metadata, "webpage_url", "original_url", "url")
    return {
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
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "instagram_publish.py"),
        str(job["manifest_path"]),
        "--single-video-media-type",
        "REELS",
        "--out",
        str(job["publish_report_path"]),
    ]
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
    if media_base_url:
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
                permalink = report_permalink(Path(str(job["publish_report_path"])))
                if permalink:
                    job["permalink"] = permalink
        else:
            failures += 1
            job["status"] = "publish_failed"
            job["failure"] = f"instagram_publish.py exited {result.returncode}"
        write_json(schedule_path, schedule)

    schedule["last_run_at"] = utc_now()
    schedule["last_run_dry_run"] = dry_run
    schedule["last_run_processed"] = processed
    schedule["last_run_failures"] = failures
    write_json(schedule_path, schedule)
    print(f"[reel-scheduler] processed={processed} failures={failures}")
    return (1 if failures else 0), schedule


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Schedule reels from a clips folder and publish due jobs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Create a channel-aware reel upload schedule")
    plan.add_argument("clips_dir", type=Path, help="Folder containing one subfolder per clip")
    plan.add_argument("--channel", default=os.environ.get("CAROUSEL_CHANNEL"), help="Publishing channel id")
    plan.add_argument("--start-at", help="First publish time in ISO 8601; naive values use channel timezone")
    plan.add_argument("--timezone", help="IANA timezone; defaults to channel publishing config")
    plan.add_argument("--interval-hours", type=float, help="Hours between reels; defaults to channel config")
    plan.add_argument("--media-filename", help="Filename selected inside each clip folder")
    plan.add_argument("--out-dir", type=Path, help="Schedule output directory")
    plan.add_argument("--limit", type=int, help="Schedule only the first N clips")

    run_due = subparsers.add_parser("run-due", help="Publish reels whose scheduled time has arrived")
    run_due.add_argument("schedule", type=Path, help="Path to schedule.json")
    run_due.add_argument("--now", help="Override the current time in ISO 8601 (useful for operations/tests)")
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
    start_at = (
        parse_datetime(args.start_at, timezone_name)
        if args.start_at
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
    schedule_path = args.schedule.expanduser().resolve()
    schedule = read_json(schedule_path)
    if not isinstance(schedule, dict):
        raise SystemExit(f"Invalid reel schedule: {schedule_path}")
    timezone_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    now = parse_datetime(args.now, timezone_name) if args.now else datetime.now(timezone.utc)
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")
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
    )
    return rc


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "plan":
        return plan_command(args)
    if args.command == "run-due":
        return run_due_command(args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
