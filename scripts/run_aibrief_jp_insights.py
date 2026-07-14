#!/usr/bin/env python3
"""Safely refresh AI Brief JP insights and stage a validated dedicated report."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
CHANNEL = "aibrief_jp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh aibrief_jp Instagram insights and validate its dedicated report"
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--data-holds",
        type=Path,
        default=None,
        help="Reviewed full-refresh exception manifest (default: ops/aibrief-jp-insights-data-holds.json)",
    )
    parser.add_argument("--lock-wait-seconds", type=int, default=60)
    parser.add_argument(
        "--sync-limit",
        type=int,
        default=13,
        help=(
            "Newest published media IDs to refresh; with four successful posts per day "
            "and no ad-hoc extras, 13 covers the four-slot cadence through roughly +73h. "
            "Use 0 for a full-account refresh."
        ),
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help=(
            "Skip freshness checks and rebuild/promote reports from the current ledger; "
            "use only for an explicit local rebuild"
        ),
    )
    parser.add_argument(
        "--daily-full-refresh",
        action="store_true",
        help=(
            "Use a full-account refresh when any currently synced media snapshot "
            "is older than 13 hours; otherwise use --sync-limit. During that full "
            "refresh only, tolerate exact identities in the reviewed data-hold "
            "manifest when every other target refreshes."
        ),
    )
    return parser


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@contextmanager
def scheduler_lock(lock_dir: Path, wait_seconds: int) -> Iterator[None]:
    if wait_seconds < 0:
        raise SystemExit("--lock-wait-seconds must be non-negative")
    deadline = time.monotonic() + wait_seconds
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"[aibrief-jp-insights] scheduler lock remained busy for {wait_seconds}s: {lock_dir}"
                )
            time.sleep(min(5, max(0.1, deadline - time.monotonic())))
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except FileNotFoundError:
            pass


def scalar(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> int:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(sql, params).fetchone()
    return int(row[0] if row and row[0] is not None else 0)


def published_count(db_path: Path) -> int:
    return scalar(
        db_path,
        "SELECT COUNT(*) FROM reels WHERE channel_id=? AND status='published'",
        (CHANNEL,),
    )


def published_identities(db_path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT content_hash, media_id
            FROM reels
            WHERE channel_id=?
              AND status='published'
              AND media_id IS NOT NULL
              AND media_id != ''
            """,
            (CHANNEL,),
        ).fetchall()
    return {(str(content_hash), str(media_id)) for content_hash, media_id in rows}


def snapshot_media_count(db_path: Path) -> int:
    return scalar(
        db_path,
        """
        SELECT COUNT(*)
        FROM reels AS r
        WHERE r.channel_id=?
          AND r.status='published'
          AND r.media_id IS NOT NULL
          AND r.media_id != ''
          AND EXISTS (
              SELECT 1
              FROM insights AS i
              WHERE i.content_hash=r.content_hash
                AND i.channel_id=r.channel_id
                AND i.media_id=r.media_id
          )
        """,
        (CHANNEL,),
    )


def target_identities(db_path: Path, limit: int) -> list[tuple[str, str]]:
    query = (
        "SELECT content_hash, media_id FROM reels "
        "WHERE channel_id=? AND status='published' "
        "AND media_id IS NOT NULL AND media_id != '' "
        "ORDER BY published_at DESC"
    )
    params: list[object] = [CHANNEL]
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(query, params).fetchall()
    return [(str(content_hash), str(media_id)) for content_hash, media_id in rows]


def current_media_snapshot_rows(db_path: Path) -> list[tuple[str, str, str, str]]:
    """Current published media with their publish time and latest matching snapshot."""
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT r.content_hash, r.media_id, r.published_at, MAX(i.captured_at)
            FROM reels AS r
            LEFT JOIN insights AS i
              ON i.content_hash=r.content_hash
             AND i.channel_id=r.channel_id
             AND i.media_id=r.media_id
            WHERE r.channel_id=?
              AND r.status='published'
              AND r.media_id IS NOT NULL
              AND r.media_id != ''
            GROUP BY r.content_hash, r.channel_id, r.media_id, r.published_at
            """,
            (CHANNEL,),
        ).fetchall()
    return [
        (
            str(content_hash or ""),
            str(media_id or ""),
            str(published_at or ""),
            str(captured_at or ""),
        )
        for content_hash, media_id, published_at, captured_at in rows
    ]


def daily_full_refresh_due(
    db_path: Path,
    *,
    as_of: str,
    exempt_identities: set[tuple[str, str]] | None = None,
    max_snapshot_age_hours: int = 13,
) -> bool:
    """Return true when the all-account inventory needs its daily refresh."""
    reference = parse_utc(as_of)
    if reference is None:
        raise ValueError(f"invalid refresh reference time: {as_of!r}")
    exemptions = exempt_identities or set()
    rows = [
        row
        for row in current_media_snapshot_rows(db_path)
        if (row[0], row[1]) not in exemptions
    ]
    if not rows:
        return True
    captures = [parse_utc(captured_at) for _, _, _, captured_at in rows]
    if any(captured is None for captured in captures):
        return True
    cutoff = reference - timedelta(hours=max_snapshot_age_hours)
    return min(captured for captured in captures if captured is not None) <= cutoff


def approved_data_hold_identities(
    db_path: Path,
    identities: list[tuple[str, str]],
    *,
    manifest_path: Path,
) -> set[tuple[str, str]]:
    """Reviewed current identities that still have no matching snapshot."""
    if not manifest_path.is_file():
        return set()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = payload.get("holds") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"invalid data-hold manifest: {manifest_path}")
    approved_pairs = {
        (str(entry.get("content_hash") or ""), str(entry.get("media_id") or ""))
        for entry in entries
        if isinstance(entry, dict)
        and str(entry.get("content_hash") or "")
        and str(entry.get("media_id") or "")
    }
    target_set = set(identities)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT r.content_hash, r.media_id
            FROM reels AS r
            WHERE r.channel_id=?
              AND r.status='published'
              AND r.media_id IS NOT NULL
              AND r.media_id != ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM insights AS i
                  WHERE i.content_hash=r.content_hash
                    AND i.channel_id=r.channel_id
                    AND i.media_id=r.media_id
              )
            """,
            (CHANNEL,),
        ).fetchall()
    return {
        (str(content_hash), str(media_id))
        for content_hash, media_id in rows
        if (str(content_hash), str(media_id)) in approved_pairs
        and (str(content_hash), str(media_id)) in target_set
    }


def never_synced_identities(db_path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT r.content_hash, r.media_id
            FROM reels AS r
            WHERE r.channel_id=?
              AND r.status='published'
              AND r.media_id IS NOT NULL
              AND r.media_id != ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM insights AS i
                  WHERE i.content_hash=r.content_hash
                    AND i.channel_id=r.channel_id
                    AND i.media_id=r.media_id
              )
            """,
            (CHANNEL,),
        ).fetchall()
    return {(str(content_hash), str(media_id)) for content_hash, media_id in rows}


def max_insight_id(db_path: Path) -> int:
    return scalar(db_path, "SELECT COALESCE(MAX(id), 0) FROM insights")


def insight_identities_after(
    db_path: Path,
    *,
    minimum_id: int,
) -> set[tuple[str, str]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT content_hash, media_id
            FROM insights
            WHERE channel_id=? AND id>?
            """,
            (CHANNEL, minimum_id),
        ).fetchall()
    return {(str(content_hash), str(media_id)) for content_hash, media_id in rows}


def run(command: list[str], cwd: Path) -> int:
    print(f"[aibrief-jp-insights] run: {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=cwd, check=False).returncode


def validate_report(
    json_path: Path,
    expected_identities: set[tuple[str, str]],
) -> dict[str, object]:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if payload.get("platform") != "instagram":
        raise ValueError("report platform is not instagram")
    if payload.get("channel_filter") != CHANNEL:
        raise ValueError(
            f"dedicated report channel mismatch: {payload.get('channel_filter')!r}"
        )
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("report items is not a list")
    japanese_items = [item for item in items if item.get("channel_id") == CHANNEL]
    report_identities = {
        (str(item.get("content_hash") or ""), str(item.get("media_id") or ""))
        for item in japanese_items
    }
    if (
        len(items) != len(expected_identities)
        or len(japanese_items) != len(items)
        or report_identities != expected_identities
    ):
        raise ValueError(
            "Japanese report identity mismatch: "
            f"items={len(items)} Japanese={len(japanese_items)} "
            f"report_identities={len(report_identities)} "
            f"ledger_identities={len(expected_identities)}"
        )
    if not japanese_items:
        raise ValueError("report contains no aibrief_jp items")
    return payload


def promote_report(
    stage_html: Path,
    destination_html: Path,
    stage_analysis_json: Path,
    stage_analysis_markdown: Path,
) -> None:
    destination_html.parent.mkdir(parents=True, exist_ok=True)
    stage_json = stage_html.with_suffix(".insights.json")
    stage_markdown = stage_html.with_suffix(".insights.md")
    destination_json = destination_html.with_suffix(".insights.json")
    destination_markdown = destination_html.with_suffix(".insights.md")
    destination_analysis_json = destination_html.parent / "aibrief_jp_reach_brief.json"
    destination_analysis_markdown = destination_html.parent / "aibrief_jp_reach_brief.md"
    # Each replace is atomic for its individual file, but the five-file bundle is
    # not a filesystem transaction.  Move the HTML last so it acts as the commit
    # marker for consumers of the dedicated staged sidecars.
    stage_json.replace(destination_json)
    stage_markdown.replace(destination_markdown)
    stage_analysis_json.replace(destination_analysis_json)
    stage_analysis_markdown.replace(destination_analysis_markdown)
    stage_html.replace(destination_html)


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.expanduser().resolve()
    db_path = (args.db or root / "state" / "reels.db").expanduser().resolve()
    report_out = (
        args.out or root / "out" / "aibrief_jp_reel_report.html"
    ).expanduser().resolve()
    data_holds_path = (
        args.data_holds or root / "ops" / "aibrief-jp-insights-data-holds.json"
    ).expanduser().resolve()
    scheduler = root / "reel_scheduler.py"
    lock_dir = root / "state" / "reel_scheduler.lock"
    if not scheduler.is_file():
        raise SystemExit(f"reel_scheduler.py not found: {scheduler}")
    if not db_path.is_file():
        raise SystemExit(f"ledger database not found: {db_path}")
    if args.sync_limit < 0:
        raise SystemExit("--sync-limit must be zero or greater")
    analyzer = root / "scripts" / "aibrief_jp_reach_analysis.py"
    if not analyzer.is_file():
        raise SystemExit(f"reach analyzer not found: {analyzer}")

    with scheduler_lock(lock_dir, args.lock_wait_seconds):
        started_at = utc_now()
        published_before = published_count(db_path)
        identities_before = published_identities(db_path)
        all_targets = target_identities(db_path, 0)
        reviewed_data_holds = approved_data_hold_identities(
            db_path,
            all_targets,
            manifest_path=data_holds_path,
        )
        full_refresh = bool(
            args.daily_full_refresh
            and not args.no_sync
            and daily_full_refresh_due(
                db_path,
                as_of=started_at,
                exempt_identities=reviewed_data_holds,
            )
        )
        effective_sync_limit = 0 if full_refresh else args.sync_limit
        targets = target_identities(db_path, effective_sync_limit)
        allowed_data_holds = (
            set(targets) & reviewed_data_holds
            if full_refresh
            else set()
        )
        if args.daily_full_refresh and not args.no_sync:
            print(
                "[aibrief-jp-insights] refresh mode "
                f"{'full-account' if full_refresh else f'newest-{effective_sync_limit}'} "
                f"known_data_holds={len(allowed_data_holds)}"
            )
        insight_watermark = max_insight_id(db_path)
        sync_rc = 0
        if not args.no_sync:
            sync_rc = run(
                [
                    sys.executable,
                    str(scheduler),
                    "sync-insights",
                    "--platform",
                    "instagram",
                    "--channel",
                    CHANNEL,
                    "--db",
                    str(db_path),
                    *(
                        []
                        if effective_sync_limit == 0
                        else ["--limit", str(effective_sync_limit)]
                    ),
                ],
                root,
            )

        report_out.parent.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(
            tempfile.mkdtemp(prefix=".reel-report-stage.", dir=report_out.parent)
        )
        stage_html = stage_dir / report_out.name
        stage_analysis_json = stage_dir / "aibrief_jp_reach_brief.json"
        stage_analysis_markdown = stage_dir / "aibrief_jp_reach_brief.md"
        try:
            report_rc = run(
                [
                    sys.executable,
                    str(scheduler),
                    "report",
                    "--platform",
                    "instagram",
                    "--channel",
                    CHANNEL,
                    "--db",
                    str(db_path),
                    "--out",
                    str(stage_html),
                ],
                root,
            )
            if report_rc != 0:
                print(
                    f"[aibrief-jp-insights] report generation failed rc={report_rc}; existing report preserved",
                    file=sys.stderr,
                )
                return 2
            published_after = published_count(db_path)
            identities_after = published_identities(db_path)
            try:
                payload = validate_report(
                    stage_html.with_suffix(".insights.json"), identities_after
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(
                    f"[aibrief-jp-insights] staged report validation failed: {exc}; existing report preserved",
                    file=sys.stderr,
                )
                return 2
            snapshots_after = snapshot_media_count(db_path)
            generated_at = str(payload.get("generated_at") or "")
            if not args.no_sync:
                fresh_target_snapshots = insight_identities_after(
                    db_path,
                    minimum_id=insight_watermark,
                ) & set(targets)
                target_set = set(targets)
                missing_target_set = target_set - fresh_target_snapshots
                missing_targets = sorted(media_id for _, media_id in missing_target_set)
                accepted_data_holds = sorted(
                    media_id
                    for _, media_id in missing_target_set & allowed_data_holds
                )
                unexpected_missing = missing_target_set - allowed_data_holds
                required_targets = target_set - allowed_data_holds
                current_never_synced = never_synced_identities(db_path)
                unreviewed_data_holds = current_never_synced - reviewed_data_holds
                coverage = (snapshots_after / published_after) if published_after else 0.0
                sync_result_acceptable = sync_rc == 0 or (
                    sync_rc == 1
                    and bool(missing_target_set)
                    and missing_target_set <= allowed_data_holds
                )
                healthy = (
                    sync_result_acceptable
                    and published_before == published_after
                    and identities_before == identities_after
                    and len(identities_after) == published_after
                    and bool(target_set)
                    and required_targets <= fresh_target_snapshots
                    and not unexpected_missing
                    and not unreviewed_data_holds
                    and coverage >= 0.90
                )
                print(
                    "[aibrief-jp-insights] summary "
                    f"sync_rc={sync_rc} published={published_after} "
                    f"target_fresh={len(fresh_target_snapshots)}/{len(targets)} "
                    f"missing_targets={','.join(missing_targets) or '-'} "
                    f"accepted_data_holds={','.join(accepted_data_holds) or '-'} "
                    f"unreviewed_data_holds={','.join(sorted(media_id for _, media_id in unreviewed_data_holds)) or '-'} "
                    f"with_snapshots={snapshots_after} coverage={coverage:.1%} "
                    f"generated_at={generated_at} report={report_out}"
                )
                if not healthy:
                    print(
                        "[aibrief-jp-insights] refresh health check failed; "
                        "existing validated report preserved",
                        file=sys.stderr,
                    )
                    return 1

            analysis_rc = run(
                [
                    sys.executable,
                    str(analyzer),
                    "--report",
                    str(stage_html.with_suffix(".insights.json")),
                    "--source-report-label",
                    str(report_out.with_suffix(".insights.json")),
                    "--db",
                    str(db_path),
                    "--json-out",
                    str(stage_analysis_json),
                    "--markdown-out",
                    str(stage_analysis_markdown),
                ],
                root,
            )
            if analysis_rc != 0 or not stage_analysis_json.is_file() or not stage_analysis_markdown.is_file():
                print(
                    "[aibrief-jp-insights] reach analysis failed; existing report preserved",
                    file=sys.stderr,
                )
                return 2
            promote_report(
                stage_html,
                report_out,
                stage_analysis_json,
                stage_analysis_markdown,
            )
            if args.no_sync:
                print(
                    "[aibrief-jp-insights] no-sync rebuild; freshness not checked "
                    f"published={published_after} with_snapshots={snapshots_after} "
                    f"generated_at={generated_at} report={report_out}"
                )
                return 0
            return 0
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
