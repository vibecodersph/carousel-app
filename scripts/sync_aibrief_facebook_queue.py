#!/usr/bin/env python3
"""Mirror the active AI Brief Instagram schedule into the Facebook ledger.

The activation boundary prevents older Instagram rows from being backfilled.
The Facebook ledger remains independent, so publishing state and duplicate
guards are tracked separately on each platform.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import reel_ledger
from channel import load_channel

DEFAULT_SOURCE_DB = ROOT / "state" / "reels.db"
DEFAULT_FACEBOOK_DB = ROOT / "state" / "facebook.db"
DEFAULT_CHANNEL = "aibrief_jp"

SOURCE_ACTIVE_STATUSES = {
    reel_ledger.STATUS_SCHEDULED,
    reel_ledger.STATUS_PREVIEWED,
}
FACEBOOK_MUTABLE_STATUSES = {
    reel_ledger.STATUS_NEW,
    reel_ledger.STATUS_SKIPPED,
    reel_ledger.STATUS_SCHEDULED,
    reel_ledger.STATUS_PREVIEWED,
}


def parse_aware_datetime(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset: {value}")
    return parsed


def facebook_settings(channel_id: str) -> dict[str, Any]:
    publishing = load_channel(channel_id).publishing
    settings = publishing.get("facebook_reels")
    return settings if isinstance(settings, dict) else {}


def configured_start_at(channel_id: str) -> str:
    settings = facebook_settings(channel_id)
    if settings.get("enabled") is not True or settings.get("mirror_instagram_queue") is not True:
        raise ValueError(f"Facebook queue mirroring is not enabled for {channel_id}")
    start_at = str(settings.get("mirror_start_at") or "").strip()
    if not start_at:
        raise ValueError(f"publishing.facebook_reels.mirror_start_at is required for {channel_id}")
    return start_at


def row_datetime(row: Any) -> datetime | None:
    value = str(row["scheduled_at"] or "").strip()
    if not value:
        return None
    try:
        return parse_aware_datetime(value, field="scheduled_at")
    except ValueError:
        return None


def row_key(row: Any) -> tuple[str, str]:
    return str(row["content_hash"]), str(row["channel_id"])


def source_mirror_state(
    source_db: Path,
    *,
    channel_id: str,
    start_at: datetime,
) -> tuple[dict[tuple[str, str], Any], dict[tuple[str, str], Any]]:
    with reel_ledger.connect(source_db) as conn:
        rows = conn.execute(
            "SELECT * FROM reels WHERE channel_id=? ORDER BY scheduled_at, content_hash",
            (channel_id,),
        ).fetchall()
    desired: dict[tuple[str, str], Any] = {}
    blocked_trials: dict[tuple[str, str], Any] = {}
    boundary = start_at.astimezone(timezone.utc)
    for row in rows:
        key = row_key(row)
        if int(row["trial_reel"] or 0):
            # The ledger does not yet record a reliable graduation timestamp.
            # Never infer graduation from publish status or strategy: a row stays
            # isolated from Facebook until explicit graduated state exists.
            blocked_trials[key] = row
            continue
        scheduled_at = row_datetime(row)
        if (
            str(row["status"]) in SOURCE_ACTIVE_STATUSES
            and scheduled_at is not None
            and scheduled_at.astimezone(timezone.utc) >= boundary
        ):
            desired[key] = row
    return desired, blocked_trials


def insert_facebook_row(conn: Any, row: Any) -> None:
    now = reel_ledger.utc_now()
    conn.execute(
        "INSERT INTO reels (content_hash, channel_id, lang, clip_dir, media_path, "
        "source_video, title, caption, status, scheduled_at, manifest_path, "
        "trial_reel, trial_graduation_strategy, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["content_hash"],
            row["channel_id"],
            row["lang"],
            row["clip_dir"],
            row["media_path"],
            row["source_video"],
            row["title"],
            row["caption"],
            reel_ledger.STATUS_SCHEDULED,
            row["scheduled_at"],
            row["manifest_path"],
            int(row["trial_reel"] or 0),
            row["trial_graduation_strategy"],
            now,
            now,
        ),
    )


def update_facebook_row(conn: Any, row: Any) -> None:
    conn.execute(
        "UPDATE reels SET lang=?, clip_dir=?, media_path=?, source_video=?, title=?, "
        "caption=?, status=?, scheduled_at=?, manifest_path=?, trial_reel=?, "
        "trial_graduation_strategy=?, published_at=NULL, media_id=NULL, permalink=NULL, "
        "last_error=NULL, updated_at=? WHERE content_hash=? AND channel_id=?",
        (
            row["lang"],
            row["clip_dir"],
            row["media_path"],
            row["source_video"],
            row["title"],
            row["caption"],
            reel_ledger.STATUS_SCHEDULED,
            row["scheduled_at"],
            row["manifest_path"],
            int(row["trial_reel"] or 0),
            row["trial_graduation_strategy"],
            reel_ledger.utc_now(),
            row["content_hash"],
            row["channel_id"],
        ),
    )


def sync_queue(
    *,
    source_db: Path,
    facebook_db: Path,
    channel_id: str,
    start_at: datetime,
) -> dict[str, int]:
    desired, blocked_trials = source_mirror_state(
        source_db,
        channel_id=channel_id,
        start_at=start_at,
    )
    counts = {"inserted": 0, "updated": 0, "kept": 0, "cancelled": 0}
    boundary = start_at.astimezone(timezone.utc)

    with reel_ledger.connect(facebook_db) as conn:
        existing_rows = conn.execute(
            "SELECT * FROM reels WHERE channel_id=?",
            (channel_id,),
        ).fetchall()
        existing = {row_key(row): row for row in existing_rows}

        for key, source_row in desired.items():
            facebook_row = existing.get(key)
            if facebook_row is None:
                insert_facebook_row(conn, source_row)
                counts["inserted"] += 1
            elif str(facebook_row["status"]) in FACEBOOK_MUTABLE_STATUSES:
                update_facebook_row(conn, source_row)
                counts["updated"] += 1
            else:
                counts["kept"] += 1

        for key, facebook_row in existing.items():
            if key in desired:
                continue

            status = str(facebook_row["status"])
            if key in blocked_trials:
                if status == reel_ledger.STATUS_SKIPPED or status not in FACEBOOK_MUTABLE_STATUSES:
                    continue
                scheduled_at = row_datetime(facebook_row) or row_datetime(blocked_trials[key])
                if scheduled_at is None or scheduled_at.astimezone(timezone.utc) < boundary:
                    continue
                reel_ledger.set_status(
                    conn,
                    str(facebook_row["content_hash"]),
                    str(facebook_row["channel_id"]),
                    reel_ledger.STATUS_SKIPPED,
                    last_error="Instagram Trial Reel excluded from Facebook mirroring",
                )
                counts["cancelled"] += 1
                continue

            if status not in {
                reel_ledger.STATUS_SCHEDULED,
                reel_ledger.STATUS_PREVIEWED,
            }:
                continue
            scheduled_at = row_datetime(facebook_row)
            if scheduled_at is None or scheduled_at.astimezone(timezone.utc) < boundary:
                continue
            reel_ledger.set_status(
                conn,
                str(facebook_row["content_hash"]),
                str(facebook_row["channel_id"]),
                reel_ledger.STATUS_SKIPPED,
                last_error="removed from mirrored Instagram queue",
            )
            counts["cancelled"] += 1
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--facebook-db", type=Path, default=DEFAULT_FACEBOOK_DB)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument(
        "--start-at",
        help="Activation boundary; defaults to channel publishing.facebook_reels.mirror_start_at",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    start_text = args.start_at or configured_start_at(args.channel)
    start_at = parse_aware_datetime(start_text, field="start_at")
    counts = sync_queue(
        source_db=args.source_db.expanduser().resolve(),
        facebook_db=args.facebook_db.expanduser().resolve(),
        channel_id=args.channel,
        start_at=start_at,
    )
    print(
        "[aibrief-facebook-sync] "
        f"start_at={start_text} desired={sum(counts.values()) - counts['cancelled']} "
        + " ".join(f"{key}={value}" for key, value in counts.items())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
