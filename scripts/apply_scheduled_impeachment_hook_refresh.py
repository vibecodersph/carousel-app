#!/usr/bin/env python3
"""Safely install validated hook rerenders into the paused Facebook queue.

The command is deliberately narrower than the generic scheduler.  It accepts
one immutable editorial review and one fully validated staging plan, verifies
that both still describe the complete scheduled/unpublished impeachment queue,
and migrates the media hash primary keys together with their live artifacts.

``check`` is read-only. ``apply`` takes a SQLite backup plus byte-for-byte
backups of every live MP4, notes file, manifest, and caption before changing
anything.  Files are swapped while a guarded ``BEGIN IMMEDIATE`` transaction is
open; any exception restores both the database and all files from those
backups.  The scheduler lock is held for the complete operation.  This script
never invokes launchctl or otherwise resumes publishing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import reel_scheduler  # noqa: E402
from channel import Channel, load_channel  # noqa: E402


DEFAULT_REVIEW = (
    ROOT
    / "out/reel_schedules/facebook_impeachment/hook_refresh_review.varied.json"
)
DEFAULT_DB = ROOT / "state/facebook_impeachment.db"
DEFAULT_LOCK = ROOT / "state/facebook_impeachment_scheduler.lock"
DEFAULT_RUNS_ROOT = (
    ROOT / "out/reel_schedules/facebook_impeachment/hook_refresh_migrations"
)
SETTINGS_KEY = "facebook_reels"


class MigrationError(RuntimeError):
    """A safety invariant failed; no partial migration should be retained."""


@dataclass(frozen=True)
class ScheduleValue:
    scheduled_at: datetime
    trial_reel: bool
    trial_graduation_strategy: str


@dataclass
class PreparedItem:
    old_hash: str
    new_hash: str
    review: dict[str, Any]
    plan: dict[str, Any]
    render_result: dict[str, Any]
    row: dict[str, Any]
    staged_media: Path
    staged_notes: Path
    live_media: Path
    live_notes: Path
    live_manifest: Path
    live_caption: Path
    old_manifest: dict[str, Any]
    new_notes: dict[str, Any]
    schedule: ScheduleValue | None = None
    caption: str = ""
    manifest: dict[str, Any] | None = None
    temp_media: Path | None = None
    temp_notes: Path | None = None
    temp_manifest: Path | None = None
    temp_caption: Path | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "apply"):
        command = subparsers.add_parser(action)
        command.add_argument("--stage-plan", type=Path, required=True)
        command.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
        command.add_argument("--db", type=Path, default=DEFAULT_DB)
        command.add_argument("--channel", default="vibecodersph")
        command.add_argument("--expected-count", type=int, default=29)
        command.add_argument("--expected-published-count", type=int, default=29)
        command.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
        command.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
        command.add_argument(
            "--now",
            help="ISO-8601 clock override for a reproducible check (normally omitted).",
        )
        command.add_argument("--reflow-threshold-minutes", type=int, default=8)
    restore = subparsers.add_parser(
        "restore",
        help="Explicitly restore a recorded run's database and live artifacts.",
    )
    restore.add_argument("--run-dir", type=Path, required=True)
    restore.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    restore.add_argument(
        "--confirm-run-id",
        required=True,
        help="Must exactly equal the run directory name.",
    )
    return parser.parse_args()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(encoded)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_bytes(
        path,
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
    )


def read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationError(f"Required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"Expected a JSON object in {path}")
    return value


def ensure_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise MigrationError(f"Missing or empty {label}: {path}")


def resolve_strict_child(path: Path, parent: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(parent.expanduser().resolve())
    except ValueError as exc:
        raise MigrationError(f"{label} escapes its staging root: {resolved}") from exc
    return resolved


def parse_moment(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationError(f"Invalid ISO timestamp for {label}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise MigrationError(f"Timestamp has no timezone for {label}: {value!r}")
    return parsed.replace(microsecond=0)


def operation_now(value: str | None) -> datetime:
    return (
        parse_moment(value, "--now").astimezone(timezone.utc)
        if value
        else datetime.now(timezone.utc).replace(microsecond=0)
    )


def rows_as_dicts(rows: Sequence[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def database_connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(str(path.resolve()), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        record = {
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "event": event,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


@contextmanager
def scheduler_lock(
    lock_path: Path,
    owner: dict[str, Any],
    *,
    adopt_recovery_run_id: str | None = None,
) -> Iterator[None]:
    lock_path = lock_path.expanduser().resolve()
    owner_path = lock_path / "owner.json"
    try:
        lock_path.mkdir(parents=False)
    except FileExistsError as exc:
        existing = ""
        existing_owner: dict[str, Any] = {}
        try:
            existing = owner_path.read_text(encoding="utf-8").strip()
            parsed = json.loads(existing)
            existing_owner = parsed if isinstance(parsed, dict) else {}
        except OSError:
            pass
        except json.JSONDecodeError:
            pass
        previous_pid = existing_owner.get("pid")
        previous_process_alive = False
        if isinstance(previous_pid, int) and previous_pid > 0:
            try:
                os.kill(previous_pid, 0)
                previous_process_alive = True
            except ProcessLookupError:
                pass
            except PermissionError:
                previous_process_alive = True
        recognized_recovery_owner = (
            existing_owner.get("retain_lock") is True
            or str(existing_owner.get("action") or "").startswith("hook-refresh-")
        )
        can_adopt = (
            adopt_recovery_run_id
            and existing_owner.get("run_id") == adopt_recovery_run_id
            and recognized_recovery_owner
            and not previous_process_alive
        )
        if not can_adopt:
            detail = f" Owner: {existing}" if existing else ""
            raise MigrationError(f"Scheduler lock is already held: {lock_path}.{detail}") from exc
    try:
        atomic_write_json(owner_path, owner)
        yield
    finally:
        if owner.get("retain_lock") is True:
            # A failed restore is the one case where releasing the lock would
            # be unsafe.  Persist an actionable owner record; the explicit
            # restore command can adopt this exact run's retained lock.
            try:
                atomic_write_json(owner_path, owner)
            except OSError:
                pass
        else:
            try:
                owner_path.unlink()
            except FileNotFoundError:
                pass
            try:
                lock_path.rmdir()
            except FileNotFoundError:
                pass


@contextmanager
def shield_sigint() -> Iterator[None]:
    """Ignore additional Ctrl-C events while restoring the consistency boundary."""
    previous: Any = None
    installed = False
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        installed = True
    except (AttributeError, ValueError):
        # signal handlers can only be changed from the main thread.  Recovery
        # still catches BaseException when invoked from a test/helper thread.
        pass
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)


def validate_review(path: Path, expected_count: int, channel_id: str) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    review = json.loads(raw)
    if not isinstance(review, dict):
        raise MigrationError("Review must be a JSON object")
    items = review.get("items") or []
    failures: list[str] = []
    if len(items) != expected_count:
        failures.append(f"expected {expected_count} items, found {len(items)}")
    if review.get("approved_count") != expected_count or review.get("rejected_count") != 0:
        failures.append("approval totals are not complete")
    if review.get("selection_profile") != "ph-impeachment-news":
        failures.append("selection_profile is not ph-impeachment-news")
    if review.get("selection_profile_version") != 3:
        failures.append("selection_profile_version is not 3")
    if review.get("rendered") is not False or review.get("queue_modified") is not False:
        failures.append("review is not the pristine final pre-render artifact")
    old_hashes: set[str] = set()
    identities: set[tuple[str, int]] = set()
    live_paths: set[str] = set()
    manifest_paths: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            failures.append("review contains a non-object item")
            continue
        old_hash = str(item.get("content_hash") or "")
        identity = (str(item.get("source_id") or ""), int(item.get("candidate_index", -1)))
        assessment = item.get("hook_assessment") or {}
        if item.get("status") != "approved" or item.get("validation_failures"):
            failures.append(f"{identity}: not cleanly approved")
        if item.get("channel_id") != channel_id:
            failures.append(f"{identity}: wrong channel")
        if not str(item.get("new_hook") or "").strip():
            failures.append(f"{identity}: blank hook")
        if assessment.get("formula_pass") is not True:
            failures.append(f"{identity}: formula gate failed")
        if assessment.get("surface_diversity_pass") is not True:
            failures.append(f"{identity}: surface-diversity gate failed")
        if old_hash in old_hashes or not old_hash:
            failures.append(f"duplicate or blank old hash: {old_hash!r}")
        if identity in identities or not identity[0] or identity[1] < 0:
            failures.append(f"duplicate or invalid identity: {identity!r}")
        media_path = str(Path(str(item.get("media_path") or "")).expanduser().resolve())
        manifest_path = str(Path(str(item.get("manifest_path") or "")).expanduser().resolve())
        if media_path in live_paths or manifest_path in manifest_paths:
            failures.append(f"{identity}: duplicate live artifact path")
        old_hashes.add(old_hash)
        identities.add(identity)
        live_paths.add(media_path)
        manifest_paths.add(manifest_path)
    if failures:
        raise MigrationError("Review preflight failed: " + "; ".join(failures))
    return review, sha256_bytes(raw)


def validate_stage_plan(
    path: Path,
    review: dict[str, Any],
    review_sha: str,
    expected_count: int,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    plan_path = path.expanduser().resolve()
    plan = read_json_object(plan_path)
    staging_root = plan_path.parent.resolve()
    if plan.get("schema_version") != 1:
        raise MigrationError("Unsupported stage-plan schema")
    if plan.get("review_sha256") != review_sha:
        raise MigrationError("Stage plan does not match the exact final review bytes")
    if plan.get("expected_count") != expected_count:
        raise MigrationError("Stage plan expected_count is stale")
    if plan.get("rendered") is not True or plan.get("validated") is not True:
        raise MigrationError("Stage plan has not completed render validation")
    plan_items = plan.get("items") or []
    results = plan.get("render_results") or []
    if len(plan_items) != expected_count or len(results) != expected_count:
        raise MigrationError(
            f"Stage plan is incomplete: items={len(plan_items)}, results={len(results)}"
        )
    review_by_hash = {str(item["content_hash"]): item for item in review["items"]}
    plan_by_hash: dict[str, dict[str, Any]] = {}
    results_by_hash: dict[str, dict[str, Any]] = {}
    new_hashes: set[str] = set()
    for result in results:
        old_hash = str(result.get("content_hash") or "")
        new_hash = str(result.get("new_content_hash") or "")
        if old_hash in results_by_hash or old_hash not in review_by_hash:
            raise MigrationError(f"Unexpected or duplicate render result: {old_hash}")
        if len(new_hash) != 64 or new_hash in new_hashes or new_hash == old_hash:
            raise MigrationError(f"Invalid or duplicate staged hash for {old_hash}: {new_hash}")
        results_by_hash[old_hash] = result
        new_hashes.add(new_hash)
    for item in plan_items:
        old_hash = str(item.get("content_hash") or "")
        approved = review_by_hash.get(old_hash)
        result = results_by_hash.get(old_hash)
        if approved is None or result is None or old_hash in plan_by_hash:
            raise MigrationError(f"Unexpected, incomplete, or duplicate stage item: {old_hash}")
        for key in (
            "channel_id",
            "source_id",
            "candidate_index",
            "start",
            "end",
            "slug",
            "media_path",
            "manifest_path",
            "scheduled_at",
            "new_hook",
            "hook_assessment",
        ):
            if item.get(key) != approved.get(key):
                raise MigrationError(f"Stage item {old_hash} drifted from review field {key}")
        staged_media = resolve_strict_child(
            Path(str(item.get("staged_media_path") or "")), staging_root, "staged media"
        )
        staged_notes = resolve_strict_child(
            Path(str(item.get("staged_notes_path") or "")), staging_root, "staged notes"
        )
        staged_subtitle = resolve_strict_child(
            Path(str(item.get("staged_subtitle_path") or "")), staging_root, "staged subtitle"
        )
        for required, label in (
            (staged_media, "staged media"),
            (staged_notes, "staged notes"),
            (staged_subtitle, "staged subtitle"),
        ):
            ensure_file(required, label)
        actual_new_hash = sha256_file(staged_media)
        if actual_new_hash != result.get("new_content_hash"):
            raise MigrationError(f"Staged media hash drifted after validation: {old_hash}")
        if sha256_file(staged_subtitle) != item.get("live_subtitle_sha256"):
            raise MigrationError(f"Staged subtitle drifted after validation: {old_hash}")
        notes = read_json_object(staged_notes)
        if notes.get("one_liner") != approved["new_hook"]:
            raise MigrationError(f"Staged hook drifted for {old_hash}")
        if notes.get("hook_assessment") != approved["hook_assessment"]:
            raise MigrationError(f"Staged hook assessment drifted for {old_hash}")
        identity = (
            int(notes.get("index", -1)),
            float(notes.get("start", -1)),
            float(notes.get("end", -1)),
            str(notes.get("slug") or ""),
        )
        expected_identity = (
            int(approved["candidate_index"]),
            float(approved["start"]),
            float(approved["end"]),
            str(approved["slug"]),
        )
        if identity != expected_identity:
            raise MigrationError(f"Staged candidate identity drifted for {old_hash}")
        plan_by_hash[old_hash] = item
    partials = list(staging_root.rglob("*.partial.mp4"))
    if partials:
        raise MigrationError(f"Staging still contains partial renders: {partials}")
    return plan, plan_by_hash, results_by_hash


def table_fingerprint(connection: sqlite3.Connection, query: str, params: Sequence[Any]) -> str:
    return canonical_hash(rows_as_dicts(connection.execute(query, params).fetchall()))


def validate_scheduled_unpublished_row(row: dict[str, Any], content_hash: str) -> None:
    """Reject queued rows carrying any residue from a publish attempt."""
    failures: list[str] = []
    if row.get("status") != "scheduled":
        failures.append(f"status={row.get('status')!r}")
    for field in ("published_at", "media_id", "permalink"):
        if row.get(field) is not None:
            failures.append(f"{field}={row.get(field)!r} (expected NULL)")
    last_error = row.get("last_error")
    if last_error is not None and str(last_error).strip():
        failures.append("last_error is non-blank")
    if failures:
        raise MigrationError(
            f"Scheduled row {content_hash} has unsafe publish residue: "
            + "; ".join(failures)
        )


def preflight_live_queue(
    *,
    db_path: Path,
    review: dict[str, Any],
    plan_by_hash: dict[str, dict[str, Any]],
    results_by_hash: dict[str, dict[str, Any]],
    channel_id: str,
    expected_count: int,
    expected_published_count: int,
) -> tuple[list[PreparedItem], dict[str, Any]]:
    review_by_hash = {str(item["content_hash"]): item for item in review["items"]}
    old_hashes = set(review_by_hash)
    connection = database_connect(db_path, readonly=True)
    try:
        scheduled_rows = connection.execute(
            """
            SELECT * FROM reels
             WHERE channel_id=? AND status='scheduled' AND published_at IS NULL
             ORDER BY scheduled_at, content_hash
            """,
            (channel_id,),
        ).fetchall()
        if len(scheduled_rows) != expected_count:
            raise MigrationError(
                f"Expected {expected_count} scheduled/unpublished rows, found {len(scheduled_rows)}"
            )
        scheduled_hashes = {str(row["content_hash"]) for row in scheduled_rows}
        if scheduled_hashes != old_hashes:
            missing = sorted(old_hashes - scheduled_hashes)
            extra = sorted(scheduled_hashes - old_hashes)
            raise MigrationError(f"Queue no longer matches review; missing={missing}, extra={extra}")
        for scheduled_row in scheduled_rows:
            validate_scheduled_unpublished_row(
                dict(scheduled_row), str(scheduled_row["content_hash"])
            )
        active_rows = connection.execute(
            """
            SELECT content_hash, status, published_at FROM reels
             WHERE channel_id=? AND published_at IS NULL
               AND status IN ('scheduled', 'publish_previewed', 'publishing')
            """,
            (channel_id,),
        ).fetchall()
        if len(active_rows) != expected_count or {
            str(row["content_hash"]) for row in active_rows
        } != old_hashes:
            raise MigrationError(
                "An additional previewed/publishing queue row exists outside the reviewed set: "
                f"{rows_as_dicts(active_rows)}"
            )
        published_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM reels WHERE channel_id=? AND status='published'",
                (channel_id,),
            ).fetchone()[0]
        )
        if published_count != expected_published_count:
            raise MigrationError(
                f"Expected {expected_published_count} published history rows, found {published_count}"
            )
        placeholders = ",".join("?" for _ in old_hashes)
        insight_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM insights WHERE channel_id=? AND content_hash IN ({placeholders})",
                (channel_id, *sorted(old_hashes)),
            ).fetchone()[0]
        )
        if insight_count:
            raise MigrationError(f"Refusing to change primary keys with {insight_count} child insight rows")
        new_hashes = [
            str(results_by_hash[old_hash]["new_content_hash"])
            for old_hash in sorted(old_hashes)
        ]
        new_placeholders = ",".join("?" for _ in new_hashes)
        collisions = connection.execute(
            f"SELECT content_hash, channel_id, status FROM reels WHERE channel_id=? "
            f"AND content_hash IN ({new_placeholders})",
            (channel_id, *new_hashes),
        ).fetchall()
        if collisions:
            raise MigrationError(f"New media hashes already exist in the ledger: {rows_as_dicts(collisions)}")
        published_fingerprint = table_fingerprint(
            connection,
            "SELECT * FROM reels WHERE channel_id=? AND status='published' "
            "ORDER BY content_hash, channel_id",
            (channel_id,),
        )
        non_target_fingerprint = table_fingerprint(
            connection,
            f"SELECT * FROM reels WHERE NOT (channel_id=? AND content_hash IN ({placeholders})) "
            "ORDER BY channel_id, content_hash",
            (channel_id, *sorted(old_hashes)),
        )
        insights_fingerprint = table_fingerprint(
            connection,
            "SELECT * FROM insights ORDER BY id",
            (),
        )
    finally:
        connection.close()

    row_by_hash = {str(row["content_hash"]): dict(row) for row in scheduled_rows}
    prepared: list[PreparedItem] = []
    live_paths: set[Path] = set()
    for approved in review["items"]:
        old_hash = str(approved["content_hash"])
        row = row_by_hash[old_hash]
        plan_item = plan_by_hash[old_hash]
        result = results_by_hash[old_hash]
        live_media = Path(str(row["media_path"])).expanduser().resolve()
        live_notes = live_media.parent / "notes.json"
        live_manifest = Path(str(row["manifest_path"] or "")).expanduser().resolve()
        live_caption = live_manifest.parent / "caption.txt"
        staged_media = Path(str(plan_item["staged_media_path"])).expanduser().resolve()
        staged_notes = Path(str(plan_item["staged_notes_path"])).expanduser().resolve()
        for required, label in (
            (live_media, "live media"),
            (live_notes, "live notes"),
            (live_manifest, "live manifest"),
            (live_caption, "live caption"),
        ):
            ensure_file(required, label)
            if required in live_paths:
                raise MigrationError(f"Live artifact is shared by multiple queue rows: {required}")
            live_paths.add(required)
        if live_media != Path(str(approved["media_path"])).expanduser().resolve():
            raise MigrationError(f"Live media path drifted for {old_hash}")
        if live_manifest != Path(str(approved["manifest_path"])).expanduser().resolve():
            raise MigrationError(f"Live manifest path drifted for {old_hash}")
        if str(row["scheduled_at"]) != str(approved["scheduled_at"]):
            raise MigrationError(f"Queue schedule drifted for {old_hash}")
        if sha256_file(live_media) != old_hash:
            raise MigrationError(f"Live media bytes no longer match ledger hash {old_hash}")
        old_notes = read_json_object(live_notes)
        identity = (
            int(old_notes.get("index", -1)),
            float(old_notes.get("start", -1)),
            float(old_notes.get("end", -1)),
            str(old_notes.get("slug") or ""),
        )
        expected_identity = (
            int(approved["candidate_index"]),
            float(approved["start"]),
            float(approved["end"]),
            str(approved["slug"]),
        )
        if identity != expected_identity or old_notes.get("one_liner") != approved.get("old_hook"):
            raise MigrationError(f"Live notes drifted from reviewed candidate {old_hash}")
        old_manifest = read_json_object(live_manifest)
        ledger = old_manifest.get("reel_ledger") or {}
        if ledger.get("content_hash") != old_hash or ledger.get("channel_id") != row["channel_id"]:
            raise MigrationError(f"Live manifest ledger identity drifted for {old_hash}")
        if old_manifest.get("scheduled_at") != row["scheduled_at"]:
            raise MigrationError(f"Live manifest schedule drifted for {old_hash}")
        slides = old_manifest.get("slides") or []
        if len(slides) != 1 or Path(str(slides[0].get("path") or "")).resolve() != live_media:
            raise MigrationError(f"Live manifest media path drifted for {old_hash}")
        caption_text = live_caption.read_text(encoding="utf-8").strip()
        if caption_text != str(row["caption"] or "").strip():
            raise MigrationError(f"Caption file and ledger caption differ for {old_hash}")
        if str(old_manifest.get("instagram_caption") or "").strip() != caption_text:
            raise MigrationError(f"Manifest and caption file differ for {old_hash}")
        prepared.append(
            PreparedItem(
                old_hash=old_hash,
                new_hash=str(result["new_content_hash"]),
                review=approved,
                plan=plan_item,
                render_result=result,
                row=row,
                staged_media=staged_media,
                staged_notes=staged_notes,
                live_media=live_media,
                live_notes=live_notes,
                live_manifest=live_manifest,
                live_caption=live_caption,
                old_manifest=old_manifest,
                new_notes=read_json_object(staged_notes),
            )
        )
    prepared.sort(
        key=lambda item: (
            parse_moment(str(item.row["scheduled_at"]), item.old_hash).astimezone(timezone.utc),
            item.old_hash,
        )
    )
    snapshot = {
        "published_count": expected_published_count,
        "published_fingerprint": published_fingerprint,
        "non_target_fingerprint": non_target_fingerprint,
        "insights_fingerprint": insights_fingerprint,
        "target_rows_fingerprint": canonical_hash([item.row for item in prepared]),
    }
    return prepared, snapshot


def schedule_items(
    *,
    connection: sqlite3.Connection,
    items: list[PreparedItem],
    channel: Channel,
    now: datetime,
    threshold_minutes: int,
) -> dict[str, Any]:
    if threshold_minutes < 0:
        raise MigrationError("Reflow threshold must be non-negative")
    settings = reel_scheduler.reel_settings(channel, SETTINGS_KEY)
    timezone_name = reel_scheduler.setting_text(
        settings, "timezone", reel_scheduler.DEFAULT_TIMEZONE
    )
    earliest = min(
        parse_moment(str(item.row["scheduled_at"]), item.old_hash).astimezone(timezone.utc)
        for item in items
    )
    boundary = now.astimezone(timezone.utc) + timedelta(minutes=threshold_minutes)
    reflowed = earliest <= boundary
    if reflowed:
        slots = reel_scheduler.posting_slots(settings)
        max_jitter = max((slot.jitter_minutes for slot in slots), default=0)
        # The helper applies negative jitter after comparing the base slot with
        # start_at.  Advancing start_at by max_jitter + one minute therefore
        # guarantees every final timestamp remains beyond the safety boundary.
        helper_start = boundary + timedelta(minutes=max_jitter + 1)
        old_hashes = {item.old_hash for item in items}
        all_scheduled = connection.execute(
            "SELECT * FROM reels WHERE channel_id=? AND scheduled_at IS NOT NULL",
            (channel.id,),
        ).fetchall()
        existing = [row for row in all_scheduled if str(row["content_hash"]) not in old_hashes]
        blockers = reel_scheduler.reflow_blocking_rows(
            existing,
            start_at=helper_start,
            timezone_name=timezone_name,
        )
        assignments = reel_scheduler.next_open_slot_assignments(
            channel=channel,
            start_at=helper_start,
            existing_rows=blockers,
            count=len(items),
            content_hashes=[item.new_hash for item in items],
            settings_key=SETTINGS_KEY,
            include_start_at=False,
        )
        if len(assignments) != len(items):
            raise MigrationError("Scheduler did not return a complete queue reflow")
        for item, assignment in zip(items, assignments):
            moment = assignment.scheduled_at.replace(microsecond=0)
            if moment.astimezone(timezone.utc) <= boundary:
                raise MigrationError(f"Unsafe reflow timestamp for {item.old_hash}: {moment}")
            item.schedule = ScheduleValue(
                scheduled_at=moment,
                trial_reel=assignment.trial_reel,
                trial_graduation_strategy=assignment.trial_graduation_strategy,
            )
    else:
        helper_start = None
        for item in items:
            item.schedule = ScheduleValue(
                scheduled_at=parse_moment(str(item.row["scheduled_at"]), item.old_hash),
                trial_reel=bool(item.row.get("trial_reel")),
                trial_graduation_strategy=str(
                    item.row.get("trial_graduation_strategy") or ""
                ),
            )
    scheduled_moments = [item.schedule.scheduled_at for item in items if item.schedule]
    if len(set(scheduled_moments)) != len(scheduled_moments):
        raise MigrationError("Queue schedule contains duplicate timestamps")
    if scheduled_moments != sorted(scheduled_moments):
        raise MigrationError("Queue order was not preserved by the reflow")
    return {
        "reflowed_all_remaining": reflowed,
        "threshold_minutes": threshold_minutes,
        "decision_boundary": boundary.isoformat(),
        "earliest_before": earliest.isoformat(),
        "helper_start": helper_start.isoformat() if helper_start else None,
        "earliest_after": scheduled_moments[0].isoformat(),
        "latest_after": scheduled_moments[-1].isoformat(),
        "timezone": timezone_name,
    }


def build_metadata(items: list[PreparedItem], channel: Channel) -> None:
    for item in items:
        assert item.schedule is not None
        clip_dir = Path(str(item.row["clip_dir"])).expanduser().resolve()
        if clip_dir != item.live_media.parent:
            raise MigrationError(f"Ledger clip_dir drifted for {item.old_hash}")
        source_metadata = reel_scheduler.load_source_metadata(clip_dir.parent)
        source_url = str(item.old_manifest.get("source_url") or "").strip()
        if not source_url:
            source_url = reel_scheduler.source_metadata_value(
                source_metadata, "webpage_url", "original_url", "url"
            )
        caption, hashtags = reel_scheduler.build_caption(
            channel,
            clip_dir,
            item.new_notes,
            source_url=source_url,
            title_override=str(item.review["new_hook"]),
            settings_key=SETTINGS_KEY,
        )
        manifest = reel_scheduler.make_manifest(
            channel=channel,
            clip_dir=clip_dir,
            media_path=item.live_media,
            notes=item.new_notes,
            notes_path=item.live_notes,
            source_metadata=source_metadata,
            scheduled_at=item.schedule.scheduled_at,
            caption=caption,
            hashtags=hashtags,
            title_override=str(item.review["new_hook"]),
            content_hash=item.new_hash,
            settings_key=SETTINGS_KEY,
            trial_reel=item.schedule.trial_reel,
            trial_graduation_strategy=item.schedule.trial_graduation_strategy,
        )
        for field in ("source_url", "source_title", "source_uploader"):
            if not manifest.get(field) and item.old_manifest.get(field):
                manifest[field] = item.old_manifest[field]
        manifest["source_url"] = source_url
        manifest["slides"][0]["source_url"] = source_url
        # Facebook reads facebook_caption first.  Keeping instagram_caption in
        # sync preserves compatibility with existing reports and fallback code.
        manifest["facebook_caption"] = caption
        manifest["instagram_caption"] = caption
        if not item.schedule.trial_reel:
            manifest.pop("instagram_trial_reel", None)
        item.caption = caption
        item.manifest = manifest


def backup_database(source_path: Path, destination_path: Path) -> None:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(source_path.resolve()))
    destination = sqlite3.connect(str(destination_path.resolve()))
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise MigrationError(f"Database backup integrity_check failed: {result}")
        destination.commit()
    finally:
        destination.close()
        source.close()


def copy_file_fsynced(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise MigrationError(f"Refusing to overwrite prepared file: {destination}")
    with source.open("rb") as source_handle, destination.open("xb") as destination_handle:
        shutil.copyfileobj(source_handle, destination_handle, length=1024 * 1024)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
    shutil.copystat(source, destination, follow_symlinks=True)


def create_backups(
    *,
    db_path: Path,
    items: list[PreparedItem],
    run_dir: Path,
    review_sha: str,
    stage_plan_path: Path,
) -> dict[str, Any]:
    backup_root = run_dir / "backup"
    backup_db = backup_root / "facebook_impeachment.db"
    backup_database(db_path, backup_db)
    records: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        item_root = backup_root / "artifacts" / f"{index:02d}-{item.old_hash[:12]}"
        for kind, live_path, backup_name in (
            ("media", item.live_media, "reel.mp4"),
            ("notes", item.live_notes, "notes.json"),
            ("manifest", item.live_manifest, "manifest.json"),
            ("caption", item.live_caption, "caption.txt"),
        ):
            backup_path = item_root / backup_name
            original_hash = sha256_file(live_path)
            copy_file_fsynced(live_path, backup_path)
            backup_hash = sha256_file(backup_path)
            if backup_hash != original_hash:
                raise MigrationError(f"Backup verification failed for {live_path}")
            records.append(
                {
                    "old_content_hash": item.old_hash,
                    "kind": kind,
                    "live_path": str(live_path),
                    "backup_path": str(backup_path),
                    "sha256": original_hash,
                    "bytes": live_path.stat().st_size,
                }
            )
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "database_path": str(db_path.resolve()),
        "database_backup_path": str(backup_db.resolve()),
        "database_backup_sha256": sha256_file(backup_db),
        "review_sha256": review_sha,
        "stage_plan_path": str(stage_plan_path.resolve()),
        "artifacts": records,
    }
    atomic_write_json(run_dir / "backup_manifest.json", manifest)
    return manifest


def temp_path(target: Path, run_id: str) -> Path:
    return target.with_name(f".{target.name}.hook-refresh-{run_id}.tmp")


def prepare_temporary_files(items: list[PreparedItem], run_id: str) -> None:
    for item in items:
        if item.manifest is None:
            raise MigrationError("Internal error: manifest was not built")
        item.temp_media = temp_path(item.live_media, run_id)
        item.temp_notes = temp_path(item.live_notes, run_id)
        item.temp_manifest = temp_path(item.live_manifest, run_id)
        item.temp_caption = temp_path(item.live_caption, run_id)
        copy_file_fsynced(item.staged_media, item.temp_media)
        copy_file_fsynced(item.staged_notes, item.temp_notes)
        atomic_write_bytes(
            item.temp_manifest,
            (json.dumps(item.manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        atomic_write_bytes(item.temp_caption, (item.caption + "\n").encode("utf-8"))
        if sha256_file(item.temp_media) != item.new_hash:
            raise MigrationError(f"Prepared media hash mismatch for {item.old_hash}")
        if read_json_object(item.temp_notes).get("one_liner") != item.review["new_hook"]:
            raise MigrationError(f"Prepared notes mismatch for {item.old_hash}")


def cleanup_temporary_files(items: list[PreparedItem]) -> None:
    for item in items:
        for path in (
            item.temp_media,
            item.temp_notes,
            item.temp_manifest,
            item.temp_caption,
        ):
            if path is not None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def assert_target_rows_unchanged(connection: sqlite3.Connection, items: list[PreparedItem]) -> None:
    for item in items:
        row = connection.execute(
            "SELECT * FROM reels WHERE content_hash=? AND channel_id=?",
            (item.old_hash, item.row["channel_id"]),
        ).fetchone()
        if row is None or dict(row) != item.row:
            raise MigrationError(f"Ledger row changed between preflight and transaction: {item.old_hash}")
        if sha256_file(item.live_media) != item.old_hash:
            raise MigrationError(f"Live media changed between preflight and swap: {item.old_hash}")


def swap_and_update(
    *,
    connection: sqlite3.Connection,
    items: list[PreparedItem],
    updated_at: str,
) -> None:
    assert_target_rows_unchanged(connection, items)
    for item in items:
        assert item.temp_media and item.temp_notes and item.temp_manifest and item.temp_caption
        os.replace(item.temp_media, item.live_media)
        os.replace(item.temp_notes, item.live_notes)
        os.replace(item.temp_manifest, item.live_manifest)
        os.replace(item.temp_caption, item.live_caption)
    for item in items:
        assert item.schedule is not None
        cursor = connection.execute(
            """
            UPDATE reels
               SET content_hash=?, title=?, caption=?, scheduled_at=?,
                   trial_reel=?, trial_graduation_strategy=?, updated_at=?,
                   status='scheduled', last_error=NULL
             WHERE content_hash=? AND channel_id=?
               AND status='scheduled' AND published_at IS NULL
               AND media_id IS NULL AND permalink IS NULL
               AND (last_error IS NULL OR TRIM(last_error)='')
               AND media_path=? AND manifest_path=? AND scheduled_at=?
            """,
            (
                item.new_hash,
                item.review["new_hook"],
                item.caption,
                item.schedule.scheduled_at.isoformat(),
                1 if item.schedule.trial_reel else 0,
                item.schedule.trial_graduation_strategy or None,
                updated_at,
                item.old_hash,
                item.row["channel_id"],
                item.row["media_path"],
                item.row["manifest_path"],
                item.row["scheduled_at"],
            ),
        )
        if cursor.rowcount != 1:
            raise MigrationError(f"Guarded ledger update failed for {item.old_hash}")


def verify_migration(
    *,
    db_path: Path,
    items: list[PreparedItem],
    snapshot: dict[str, Any],
    channel_id: str,
    expected_count: int,
    expected_published_count: int,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    owns_connection = connection is None
    if connection is None:
        connection = database_connect(db_path, readonly=True)
    assert connection is not None
    try:
        old_hashes = [item.old_hash for item in items]
        new_hashes = [item.new_hash for item in items]
        old_placeholders = ",".join("?" for _ in old_hashes)
        new_placeholders = ",".join("?" for _ in new_hashes)
        old_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM reels WHERE channel_id=? AND content_hash IN ({old_placeholders})",
                (channel_id, *old_hashes),
            ).fetchone()[0]
        )
        if old_count:
            raise MigrationError(f"{old_count} obsolete ledger hashes remain")
        rows = connection.execute(
            f"SELECT * FROM reels WHERE channel_id=? AND content_hash IN ({new_placeholders}) "
            "ORDER BY scheduled_at, content_hash",
            (channel_id, *new_hashes),
        ).fetchall()
        if len(rows) != expected_count:
            raise MigrationError(f"Expected {expected_count} migrated rows, found {len(rows)}")
        scheduled_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM reels WHERE channel_id=? AND status='scheduled' "
                "AND published_at IS NULL",
                (channel_id,),
            ).fetchone()[0]
        )
        if scheduled_count != expected_count:
            raise MigrationError(f"Scheduled queue count changed to {scheduled_count}")
        published_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM reels WHERE channel_id=? AND status='published'",
                (channel_id,),
            ).fetchone()[0]
        )
        if published_count != expected_published_count:
            raise MigrationError(f"Published history count changed to {published_count}")
        published_fingerprint = table_fingerprint(
            connection,
            "SELECT * FROM reels WHERE channel_id=? AND status='published' "
            "ORDER BY content_hash, channel_id",
            (channel_id,),
        )
        if published_fingerprint != snapshot["published_fingerprint"]:
            raise MigrationError("Published history changed during migration")
        non_target_fingerprint = table_fingerprint(
            connection,
            f"SELECT * FROM reels WHERE NOT (channel_id=? AND content_hash IN ({new_placeholders})) "
            "ORDER BY channel_id, content_hash",
            (channel_id, *new_hashes),
        )
        if non_target_fingerprint != snapshot["non_target_fingerprint"]:
            raise MigrationError("A non-target ledger row changed during migration")
        insights_fingerprint = table_fingerprint(
            connection,
            "SELECT * FROM insights ORDER BY id",
            (),
        )
        if insights_fingerprint != snapshot["insights_fingerprint"]:
            raise MigrationError("Insights history changed during migration")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise MigrationError(f"Foreign-key errors after migration: {foreign_key_errors}")
        migrated_by_hash = {str(row["content_hash"]): dict(row) for row in rows}
        mapping: list[dict[str, Any]] = []
        for item in items:
            assert item.schedule is not None and item.manifest is not None
            row = migrated_by_hash.get(item.new_hash)
            if row is None:
                raise MigrationError(f"Missing migrated ledger row for {item.old_hash}")
            expected = {
                "title": item.review["new_hook"],
                "caption": item.caption,
                "scheduled_at": item.schedule.scheduled_at.isoformat(),
                "status": "scheduled",
                "published_at": None,
                "media_id": None,
                "permalink": None,
                "last_error": None,
                "media_path": item.row["media_path"],
                "manifest_path": item.row["manifest_path"],
                "trial_reel": 1 if item.schedule.trial_reel else 0,
                "trial_graduation_strategy": (
                    item.schedule.trial_graduation_strategy or None
                ),
            }
            for field, value in expected.items():
                if row.get(field) != value:
                    raise MigrationError(
                        f"Migrated ledger field {field} is wrong for {item.new_hash}"
                    )
            if sha256_file(item.live_media) != item.new_hash:
                raise MigrationError(f"Live media does not match new PK for {item.new_hash}")
            notes = read_json_object(item.live_notes)
            if notes.get("one_liner") != item.review["new_hook"]:
                raise MigrationError(f"Live notes hook is wrong for {item.new_hash}")
            assessment = notes.get("hook_assessment") or {}
            if assessment.get("formula_pass") is not True or assessment.get(
                "surface_diversity_pass"
            ) is not True:
                raise MigrationError(f"Live notes gates are incomplete for {item.new_hash}")
            manifest = read_json_object(item.live_manifest)
            ledger = manifest.get("reel_ledger") or {}
            if ledger != {"content_hash": item.new_hash, "channel_id": channel_id}:
                raise MigrationError(f"Manifest ledger identity is wrong for {item.new_hash}")
            for field in ("topic", "description"):
                if manifest.get(field) != item.review["new_hook"]:
                    raise MigrationError(f"Manifest {field} is wrong for {item.new_hash}")
            if manifest.get("scheduled_at") != item.schedule.scheduled_at.isoformat():
                raise MigrationError(f"Manifest schedule is wrong for {item.new_hash}")
            if manifest.get("instagram_caption") != item.caption or manifest.get(
                "facebook_caption"
            ) != item.caption:
                raise MigrationError(f"Manifest captions are wrong for {item.new_hash}")
            slides = manifest.get("slides") or []
            if len(slides) != 1 or Path(str(slides[0].get("path") or "")).resolve() != item.live_media:
                raise MigrationError(f"Manifest media path is wrong for {item.new_hash}")
            if Path(str(manifest.get("notes_path") or "")).resolve() != item.live_notes:
                raise MigrationError(f"Manifest notes path is wrong for {item.new_hash}")
            if item.live_caption.read_text(encoding="utf-8").strip() != item.caption:
                raise MigrationError(f"Caption file is wrong for {item.new_hash}")
            mapping.append(
                {
                    "old_content_hash": item.old_hash,
                    "new_content_hash": item.new_hash,
                    "source_id": item.review["source_id"],
                    "candidate_index": item.review["candidate_index"],
                    "hook": item.review["new_hook"],
                    "scheduled_at_before": item.row["scheduled_at"],
                    "scheduled_at_after": item.schedule.scheduled_at.isoformat(),
                    "media_path": str(item.live_media),
                    "manifest_path": str(item.live_manifest),
                }
            )
        return {
            "verified_count": len(mapping),
            "scheduled_count": scheduled_count,
            "published_count": published_count,
            "published_fingerprint": published_fingerprint,
            "non_target_fingerprint": non_target_fingerprint,
            "insights_fingerprint": insights_fingerprint,
            "foreign_key_errors": 0,
            "items": mapping,
        }
    finally:
        if owns_connection:
            connection.close()


def restore_from_backup(backup_manifest: dict[str, Any]) -> dict[str, Any]:
    database_path = Path(str(backup_manifest["database_path"])).resolve()
    database_backup = Path(str(backup_manifest["database_backup_path"])).resolve()
    ensure_file(database_backup, "database backup")
    if sha256_file(database_backup) != backup_manifest.get("database_backup_sha256"):
        raise MigrationError("Database backup bytes do not match the recorded checksum")
    source = sqlite3.connect(str(database_backup))
    destination = sqlite3.connect(str(database_path))
    try:
        source.backup(destination)
        destination.commit()
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise MigrationError(f"Restored database integrity_check failed: {result}")
        # The live ledger normally uses WAL.  Checkpoint the restored pages so
        # no committed migration frame remains outside the restored main file.
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        destination.close()
        source.close()
    restored = 0
    for record in backup_manifest.get("artifacts") or []:
        live_path = Path(str(record["live_path"])).resolve()
        backup_path = Path(str(record["backup_path"])).resolve()
        ensure_file(backup_path, "artifact backup")
        if sha256_file(backup_path) != record.get("sha256"):
            raise MigrationError(f"Artifact backup checksum mismatch: {backup_path}")
        temporary = live_path.with_name(f".{live_path.name}.restore-{os.getpid()}.tmp")
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        copy_file_fsynced(backup_path, temporary)
        os.replace(temporary, live_path)
        if sha256_file(live_path) != record.get("sha256"):
            raise MigrationError(f"Restored artifact checksum mismatch: {live_path}")
        restored += 1
    return {"database": str(database_path), "restored_artifacts": restored}


def create_run_dir(root: Path, review_sha: str, now: datetime) -> tuple[str, Path]:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_id = f"{stamp}-{review_sha[:12]}"
    run_id = base_id
    index = 1
    while (root / run_id).exists():
        index += 1
        run_id = f"{base_id}-{index}"
    run_dir = (root / run_id).resolve()
    run_dir.mkdir(parents=True)
    return run_id, run_dir


def common_preflight(args: argparse.Namespace) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    list[PreparedItem],
    dict[str, Any],
    dict[str, Any],
]:
    review_path = args.review.expanduser().resolve()
    stage_plan_path = args.stage_plan.expanduser().resolve()
    db_path = args.db.expanduser().resolve()
    ensure_file(review_path, "review")
    ensure_file(stage_plan_path, "stage plan")
    ensure_file(db_path, "scheduler database")
    review, review_sha = validate_review(
        review_path, args.expected_count, args.channel
    )
    queue_database = Path(str(review.get("queue_database") or "")).expanduser().resolve()
    if queue_database != db_path:
        raise MigrationError(
            f"Review was generated from {queue_database}, not requested database {db_path}"
        )
    plan, plan_by_hash, results_by_hash = validate_stage_plan(
        stage_plan_path,
        review,
        review_sha,
        args.expected_count,
    )
    items, snapshot = preflight_live_queue(
        db_path=db_path,
        review=review,
        plan_by_hash=plan_by_hash,
        results_by_hash=results_by_hash,
        channel_id=args.channel,
        expected_count=args.expected_count,
        expected_published_count=args.expected_published_count,
    )
    channel = load_channel(args.channel)
    connection = database_connect(db_path, readonly=True)
    try:
        schedule_report = schedule_items(
            connection=connection,
            items=items,
            channel=channel,
            now=operation_now(args.now),
            threshold_minutes=args.reflow_threshold_minutes,
        )
    finally:
        connection.close()
    build_metadata(items, channel)
    return review, review_sha, plan, items, snapshot, schedule_report


def check_command(args: argparse.Namespace) -> int:
    now = operation_now(args.now)
    owner = {
        "pid": os.getpid(),
        "action": "hook-refresh-check",
        "started_at": now.isoformat(),
    }
    with scheduler_lock(args.lock, owner):
        review, review_sha, plan, items, snapshot, schedule_report = common_preflight(args)
        result = {
            "action": "check",
            "safe_to_apply": True,
            "review_sha256": review_sha,
            "stage_plan": str(args.stage_plan.expanduser().resolve()),
            "validated_stage_count": len(plan["items"]),
            "scheduled_unpublished_count": len(items),
            "published_count": snapshot["published_count"],
            "schedule": schedule_report,
            "hooks": [
                {
                    "old_content_hash": item.old_hash,
                    "new_content_hash": item.new_hash,
                    "hook": item.review["new_hook"],
                    "scheduled_at": item.schedule.scheduled_at.isoformat()
                    if item.schedule
                    else None,
                }
                for item in items
            ],
            "scheduler_resumed": False,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def apply_command(args: argparse.Namespace) -> int:
    now = operation_now(args.now)
    # Read the review hash before creating the run directory; full validation is
    # repeated under the scheduler lock immediately below.
    review_raw = args.review.expanduser().resolve().read_bytes()
    review_sha = sha256_bytes(review_raw)
    run_id, run_dir = create_run_dir(args.runs_root.expanduser().resolve(), review_sha, now)
    journal = Journal(run_dir / "journal.jsonl")
    owner = {
        "pid": os.getpid(),
        "run_id": run_id,
        "action": "hook-refresh-apply",
        "started_at": now.isoformat(),
    }
    backup_manifest: dict[str, Any] | None = None
    items: list[PreparedItem] = []
    mutations_started = False
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "started_at": now.isoformat(),
        "status": "started",
        "scheduler_resumed": False,
    }
    atomic_write_json(run_dir / "report.json", report)
    with scheduler_lock(args.lock, owner):
        try:
            journal.write("lock_acquired", lock=str(args.lock.expanduser().resolve()))
            review, validated_sha, plan, items, snapshot, schedule_report = common_preflight(args)
            if validated_sha != review_sha:
                raise MigrationError("Review bytes changed while starting the migration")
            report.update(
                {
                    "review_path": str(args.review.expanduser().resolve()),
                    "review_sha256": validated_sha,
                    "stage_plan": str(args.stage_plan.expanduser().resolve()),
                    "stage_plan_sha256": sha256_file(args.stage_plan.expanduser().resolve()),
                    "database": str(args.db.expanduser().resolve()),
                    "preflight_count": len(items),
                    "schedule": schedule_report,
                }
            )
            journal.write(
                "preflight_complete",
                target_count=len(items),
                reflowed=schedule_report["reflowed_all_remaining"],
            )
            backup_manifest = create_backups(
                db_path=args.db.expanduser().resolve(),
                items=items,
                run_dir=run_dir,
                review_sha=validated_sha,
                stage_plan_path=args.stage_plan,
            )
            report["backup_manifest"] = str(run_dir / "backup_manifest.json")
            journal.write(
                "backup_complete",
                artifact_count=len(backup_manifest["artifacts"]),
                database_backup=backup_manifest["database_backup_path"],
            )
            prepare_temporary_files(items, run_id)
            journal.write("temporary_files_prepared", file_count=len(items) * 4)

            connection = database_connect(args.db.expanduser().resolve())
            try:
                connection.execute("BEGIN IMMEDIATE")
                journal.write("transaction_started")
                mutations_started = True
                swap_and_update(
                    connection=connection,
                    items=items,
                    updated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                )
                journal.write("files_swapped_and_rows_updated", target_count=len(items))
                inside_verification = verify_migration(
                    db_path=args.db.expanduser().resolve(),
                    items=items,
                    snapshot=snapshot,
                    channel_id=args.channel,
                    expected_count=args.expected_count,
                    expected_published_count=args.expected_published_count,
                    connection=connection,
                )
                journal.write(
                    "transaction_verification_complete",
                    verified_count=inside_verification["verified_count"],
                )
                connection.commit()
                journal.write("transaction_committed")
            except BaseException:
                # KeyboardInterrupt is a BaseException, not an Exception.  A
                # mid-swap Ctrl-C must roll back the ledger before the outer
                # handler restores every already-replaced file.
                with shield_sigint():
                    try:
                        connection.rollback()
                    except BaseException:
                        # The verified SQLite backup is restored below even if
                        # rollback itself cannot complete.
                        pass
                raise
            finally:
                connection.close()

            verification = verify_migration(
                db_path=args.db.expanduser().resolve(),
                items=items,
                snapshot=snapshot,
                channel_id=args.channel,
                expected_count=args.expected_count,
                expected_published_count=args.expected_published_count,
            )
            journal.write(
                "post_commit_verification_complete",
                verified_count=verification["verified_count"],
            )
            report.update(
                {
                    "status": "complete",
                    "completed_at": datetime.now(timezone.utc)
                    .replace(microsecond=0)
                    .isoformat(),
                    "verification": verification,
                    "backup_retained": True,
                    "scheduler_resumed": False,
                }
            )
            atomic_write_json(run_dir / "report.json", report)
            journal.write("complete", report=str(run_dir / "report.json"))
            cleanup_temporary_files(items)
            print(json.dumps(report, indent=2, ensure_ascii=False))
        except BaseException as exc:
            # Recovery happens before scheduler_lock exits, so no periodic run
            # can observe the file/database identity split even on an error.
            failure_traceback = traceback.format_exc()
            restore_result: dict[str, Any] | None = None
            restore_error = ""
            cleanup_error = ""
            restore_attempts = 0
            with shield_sigint():
                try:
                    cleanup_temporary_files(items)
                except BaseException as cleanup_exc:  # pragma: no cover - defensive
                    cleanup_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                if mutations_started and backup_manifest is None:
                    restore_error = "Mutation began without an available backup manifest"
                elif mutations_started and backup_manifest is not None:
                    # Restoring is idempotent. Retry once if a programmatic
                    # BaseException or transient I/O failure interrupts the
                    # first attempt; Ctrl-C itself is shielded in this block.
                    restore_failures: list[str] = []
                    for attempt in (1, 2):
                        restore_attempts = attempt
                        try:
                            try:
                                journal.write("automatic_restore_started", attempt=attempt)
                            except BaseException:
                                pass
                            restore_result = restore_from_backup(backup_manifest)
                            try:
                                journal.write(
                                    "automatic_restore_complete",
                                    attempt=attempt,
                                    **restore_result,
                                )
                            except BaseException:
                                pass
                            break
                        except BaseException as restore_exc:  # pragma: no cover - catastrophic path
                            detail = f"attempt {attempt}: {type(restore_exc).__name__}: {restore_exc}"
                            restore_failures.append(detail)
                            try:
                                journal.write("automatic_restore_attempt_failed", error=detail)
                            except BaseException:
                                pass
                    if restore_result is None:
                        restore_error = "; ".join(restore_failures)
            if mutations_started and restore_result is None:
                owner["retain_lock"] = True
                owner["retained_reason"] = restore_error or "automatic restore did not complete"
            report.update(
                {
                    "status": (
                        "failed_restored"
                        if restore_result
                        else "failed_unrecovered"
                        if mutations_started
                        else "failed"
                    ),
                    "failed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": failure_traceback,
                    "automatic_restore": restore_result,
                    "automatic_restore_attempts": restore_attempts,
                    "restore_error": restore_error or None,
                    "cleanup_error": cleanup_error or None,
                    "scheduler_lock_retained": owner.get("retain_lock") is True,
                    "scheduler_resumed": False,
                }
            )
            atomic_write_json(run_dir / "report.json", report)
            journal.write("failed", error=report["error"], restored=bool(restore_result))
            if restore_error:
                raise MigrationError(
                    f"Migration failed ({exc}) and automatic restore also failed ({restore_error}). "
                    "Publishing must remain stopped. "
                    f"Recovery records: {run_dir}"
                ) from exc
            raise MigrationError(
                f"Migration failed and was {'restored' if restore_result else 'not mutated'}: {exc}. "
                f"Report: {run_dir / 'report.json'}"
            ) from exc
    return 0


def restore_command(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.expanduser().resolve()
    if args.confirm_run_id != run_dir.name:
        raise MigrationError("--confirm-run-id must exactly match the run directory name")
    backup_manifest_path = run_dir / "backup_manifest.json"
    backup_manifest = read_json_object(backup_manifest_path)
    journal = Journal(run_dir / "journal.jsonl")
    owner = {
        "pid": os.getpid(),
        "run_id": run_dir.name,
        "action": "hook-refresh-explicit-restore",
        "started_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    with scheduler_lock(
        args.lock,
        owner,
        adopt_recovery_run_id=run_dir.name,
    ):
        try:
            journal.write("explicit_restore_started")
            with shield_sigint():
                result = restore_from_backup(backup_manifest)
            journal.write("explicit_restore_complete", **result)
            output = {
                "status": "restored",
                "run_id": run_dir.name,
                **result,
                "scheduler_resumed": False,
            }
            atomic_write_json(run_dir / "restore_report.json", output)
            print(json.dumps(output, indent=2, ensure_ascii=False))
        except BaseException as exc:
            owner["retain_lock"] = True
            owner["retained_reason"] = (
                f"Explicit restore failed: {type(exc).__name__}: {exc}"
            )
            try:
                journal.write("explicit_restore_failed", error=owner["retained_reason"])
            except BaseException:
                pass
            raise MigrationError(
                f"Explicit restore failed; scheduler lock retained at {args.lock}: {exc}"
            ) from exc
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.action == "check":
            return check_command(args)
        if args.action == "apply":
            return apply_command(args)
        return restore_command(args)
    except (MigrationError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[hook-refresh] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
