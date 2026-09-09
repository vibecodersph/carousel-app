#!/usr/bin/env python3
"""Atomically replace unpublished AI Brief JP Trials with a rendered pool batch."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import reel_ledger
import reel_scheduler
from scripts import sync_aibrief_facebook_queue as facebook_sync


DEFAULT_PLAN = ROOT / "config" / "aibrief_jp_trial_rotation_batch_20260830.json"
DEFAULT_LOCK = ROOT / "state" / "reel_scheduler.lock"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--db", type=Path, default=ROOT / "state" / "reels.db")
    parser.add_argument(
        "--facebook-db", type=Path, default=ROOT / "state" / "facebook.db"
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "out" / "reel_schedules" / "trial_pool_rotation_20260830",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as target_conn:
        source_conn.backup(target_conn)


def restore_sqlite(source_backup: Path, destination: Path) -> None:
    with sqlite3.connect(source_backup) as source_conn, sqlite3.connect(destination) as target_conn:
        source_conn.backup(target_conn)


def plan_inputs(plan_path: Path) -> tuple[dict[str, Any], Path, dict[str, dict[str, Any]]]:
    plan = load_json(plan_path)
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise RuntimeError("Batch plan must be a schema_version=1 object")
    if plan.get("channel_id") != "aibrief_jp":
        raise RuntimeError("Batch plan channel must be aibrief_jp")
    pool_path = (ROOT / str(plan.get("pool_path") or "")).resolve()
    pool = load_json(pool_path)
    if plan.get("pool_id") != pool.get("pool_id"):
        raise RuntimeError("Batch plan and rotation pool ids differ")
    output_root = (ROOT / str(plan.get("output_root") or "")).resolve()
    report = load_json(output_root / "batch_render_report.json")
    render_results = {
        str(item.get("experiment_id") or ""): item
        for item in report.get("results") or []
        if isinstance(item, dict)
    }
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Batch plan entries must be non-empty")
    if len(render_results) != len(entries):
        raise RuntimeError("Rendered result count does not match the batch plan")
    for entry in entries:
        result = render_results.get(str(entry.get("experiment_id") or ""))
        if result is None:
            raise RuntimeError(f"Missing render for {entry.get('experiment_id')}")
        exact = {
            "scheduled_at": result.get("scheduled_at"),
            "hook": result.get("variant_hook"),
            "parent_content_hash": (result.get("parent") or {}).get("content_hash"),
        }
        for key, actual in exact.items():
            if str(entry.get(key) or "") != str(actual or ""):
                raise RuntimeError(
                    f"Render metadata mismatch for {entry['experiment_id']}: {key}"
                )
        media_path = Path(str((result.get("output") or {}).get("path") or ""))
        expected_hash = str((result.get("output") or {}).get("sha256") or "")
        if not media_path.is_file() or sha256_file(media_path) != expected_hash:
            raise RuntimeError(f"Rendered media hash drifted: {media_path}")
        qa = result.get("qa") or {}
        if qa.get("decoded_audio_matches_parent") is not True:
            raise RuntimeError(f"Audio QA failed for {entry['experiment_id']}")
        if float(qa.get("video_band_ssim_vs_parent") or 0) < 0.95:
            raise RuntimeError(f"Central-video QA failed for {entry['experiment_id']}")
    return plan, pool_path, render_results


def active_regular_snapshot(db_path: Path, channel_id: str) -> list[dict[str, Any]]:
    with reel_ledger.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT content_hash,status,scheduled_at,trial_reel,title,media_path "
            "FROM reels WHERE channel_id=? AND trial_reel=0 "
            "AND status IN (?,?) ORDER BY scheduled_at,content_hash",
            (
                channel_id,
                reel_ledger.STATUS_SCHEDULED,
                reel_ledger.STATUS_PREVIEWED,
            ),
        ).fetchall()
    return [dict(row) for row in rows]


def copy_retired_manifests(
    *,
    db_path: Path,
    channel_id: str,
    rows: list[dict[str, Any]],
    destination: Path,
) -> dict[str, Path]:
    hashes = [str(row["content_hash"]) for row in rows]
    if not hashes:
        return {}
    placeholders = ",".join("?" for _ in hashes)
    with reel_ledger.connect(db_path) as conn:
        ledger_rows = conn.execute(
            "SELECT content_hash,manifest_path FROM reels WHERE channel_id=? "
            f"AND content_hash IN ({placeholders})",
            [channel_id, *hashes],
        ).fetchall()
    copied: dict[str, Path] = {}
    for row in ledger_rows:
        source = Path(str(row["manifest_path"] or ""))
        if not source.is_file():
            raise RuntimeError(f"Retired Trial manifest is missing: {source}")
        target = destination / f"{row['content_hash']}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied[str(row["content_hash"])] = target
    return copied


def schedule_entries(
    *,
    db_path: Path,
    plan: dict[str, Any],
    pool_path: Path,
    render_results: dict[str, dict[str, Any]],
    out_dir: Path,
    apply: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for entry in plan["entries"]:
        experiment_id = str(entry["experiment_id"])
        render = render_results[experiment_id]
        media_path = Path(str(render["output"]["path"]))
        with reel_ledger.connect(db_path) as conn:
            parent = reel_ledger.get_reel(
                conn,
                str(entry["parent_content_hash"]),
                str(plan["channel_id"]),
            )
            if parent is None:
                raise RuntimeError(f"Parent vanished: {entry['parent_content_hash']}")
            family = str(parent["source_video"] or "")
        result = reel_scheduler.add_trial_from_published(
            db_path=db_path,
            channel_id=str(plan["channel_id"]),
            parent_content_hash=str(entry["parent_content_hash"]),
            media_path=media_path,
            experiment_id=experiment_id,
            variant_hook=str(entry["hook"]),
            scheduled_at=str(entry["scheduled_at"]),
            expected_scheduled_at=str(entry["scheduled_at"]),
            asset_family_id=family,
            changed_variables=["overlay_hook"],
            graduation_strategy="MANUAL",
            out_dir=out_dir,
            apply=apply,
            caption_mode="preserve-parent",
            rotation_pool_path=pool_path,
        )
        results.append(result)
    return results


def rehearse(
    *,
    db_path: Path,
    facebook_db: Path,
    plan: dict[str, Any],
    pool_path: Path,
    render_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    channel_id = str(plan["channel_id"])
    retirement = reel_scheduler.retire_unpublished_trial_reels(
        db_path=db_path,
        channel_id=channel_id,
        apply=False,
        reason="replaced by fixed photo-pool rotation",
    )
    planned_slots = {str(entry["scheduled_at"]) for entry in plan["entries"]}
    retiring_slots = {str(row["scheduled_at"]) for row in retirement["rows"]}
    if retirement["count"] != len(plan["entries"]) or retiring_slots != planned_slots:
        raise RuntimeError(
            "The unpublished Trial queue no longer matches the reviewed replacement slots"
        )
    if retirement["scheduled_conversions"]:
        raise RuntimeError("Unexpected scheduled-conversion Trial in replacement scope")

    with tempfile.TemporaryDirectory(prefix="aibrief-trial-pool-rehearsal-") as temporary:
        root = Path(temporary)
        shadow_db = root / "reels.db"
        shadow_facebook = root / "facebook.db"
        sqlite_backup(db_path, shadow_db)
        sqlite_backup(facebook_db, shadow_facebook)
        copied = copy_retired_manifests(
            db_path=shadow_db,
            channel_id=channel_id,
            rows=retirement["rows"],
            destination=root / "old-manifests",
        )
        with reel_ledger.connect(shadow_db) as conn:
            for content_hash, manifest_path in copied.items():
                conn.execute(
                    "UPDATE reels SET manifest_path=? WHERE content_hash=? AND channel_id=?",
                    (str(manifest_path), content_hash, channel_id),
                )
        reel_scheduler.retire_unpublished_trial_reels(
            db_path=shadow_db,
            channel_id=channel_id,
            apply=True,
            reason="replaced by fixed photo-pool rotation",
        )
        preview = schedule_entries(
            db_path=shadow_db,
            plan=plan,
            pool_path=pool_path,
            render_results=render_results,
            out_dir=root / "new-manifests",
            apply=False,
        )
        applied = schedule_entries(
            db_path=shadow_db,
            plan=plan,
            pool_path=pool_path,
            render_results=render_results,
            out_dir=root / "new-manifests",
            apply=True,
        )
        facebook_before = active_regular_snapshot(shadow_facebook, channel_id)
        start_at = facebook_sync.parse_aware_datetime(
            facebook_sync.configured_start_at(channel_id), field="start_at"
        )
        facebook_counts = facebook_sync.sync_queue(
            source_db=shadow_db,
            facebook_db=shadow_facebook,
            channel_id=channel_id,
            start_at=start_at,
        )
        facebook_after = active_regular_snapshot(shadow_facebook, channel_id)
        if facebook_before != facebook_after:
            raise RuntimeError("Facebook regular queue would change during pool migration")
    return {
        "retirement": retirement,
        "replacement_count": len(applied),
        "dry_run_count": len(preview),
        "facebook_counts": facebook_counts,
    }


def verify_live(
    *,
    db_path: Path,
    facebook_db: Path,
    plan: dict[str, Any],
    render_results: dict[str, dict[str, Any]],
    old_rows: list[dict[str, Any]],
    regular_before: list[dict[str, Any]],
    facebook_before: list[dict[str, Any]],
) -> dict[str, Any]:
    channel_id = str(plan["channel_id"])
    with reel_ledger.connect(db_path) as conn:
        for old in old_rows:
            row = reel_ledger.get_reel(conn, str(old["content_hash"]), channel_id)
            experiment = reel_ledger.get_trial_experiment(
                conn, str(old["experiment_id"])
            )
            if (
                row is None
                or row["status"] != reel_ledger.STATUS_SKIPPED
                or row["scheduled_at"] is not None
                or int(row["trial_reel"] or 0) != 0
                or experiment is None
                or experiment["state"] != reel_ledger.TRIAL_STATE_STOPPED
            ):
                raise RuntimeError(f"Old Trial was not safely retired: {old}")
        new_rows: list[dict[str, Any]] = []
        for entry in plan["entries"]:
            render = render_results[str(entry["experiment_id"])]
            content_hash = str(render["output"]["sha256"])
            row = reel_ledger.get_reel(conn, content_hash, channel_id)
            parent = reel_ledger.get_reel(
                conn, str(entry["parent_content_hash"]), channel_id
            )
            experiment = reel_ledger.get_trial_experiment(
                conn, str(entry["experiment_id"])
            )
            if (
                row is None
                or parent is None
                or row["status"] != reel_ledger.STATUS_SCHEDULED
                or row["scheduled_at"] != entry["scheduled_at"]
                or int(row["trial_reel"] or 0) != 1
                or row["caption"] != parent["caption"]
                or experiment is None
                or experiment["state"] != reel_ledger.TRIAL_STATE_SCHEDULED
                or experiment["case_type"]
                != reel_ledger.TRIAL_CASE_SUCCESSFUL_POST_VARIANT
                or experiment["parent_content_hash"]
                != entry["parent_content_hash"]
            ):
                raise RuntimeError(f"Replacement Trial verification failed: {entry}")
            new_rows.append(
                {
                    "content_hash": content_hash,
                    "experiment_id": entry["experiment_id"],
                    "parent_content_hash": entry["parent_content_hash"],
                    "scheduled_at": entry["scheduled_at"],
                    "title": row["title"],
                    "caption_preserved": True,
                }
            )
    if active_regular_snapshot(db_path, channel_id) != regular_before:
        raise RuntimeError("Instagram regular queue changed during pool migration")
    facebook_after = active_regular_snapshot(facebook_db, channel_id)
    if facebook_after != facebook_before:
        raise RuntimeError("Facebook regular queue changed during pool migration")
    with reel_ledger.connect(facebook_db) as conn:
        mirrored_trials = conn.execute(
            "SELECT content_hash FROM reels WHERE channel_id=? AND content_hash IN ("
            + ",".join("?" for _ in new_rows)
            + ") AND status IN (?,?)",
            [
                channel_id,
                *[row["content_hash"] for row in new_rows],
                reel_ledger.STATUS_SCHEDULED,
                reel_ledger.STATUS_PREVIEWED,
            ],
        ).fetchall()
    if mirrored_trials:
        raise RuntimeError("One or more additive Trials leaked into Facebook")
    return {"new_rows": new_rows, "facebook_trial_mirrors": 0}


def main() -> int:
    args = parse_args()
    plan_path = args.plan.resolve()
    db_path = args.db.resolve()
    facebook_db = args.facebook_db.resolve()
    out_dir = args.out_dir.resolve()
    plan, pool_path, render_results = plan_inputs(plan_path)
    preflight = rehearse(
        db_path=db_path,
        facebook_db=facebook_db,
        plan=plan,
        pool_path=pool_path,
        render_results=render_results,
    )
    result: dict[str, Any] = {
        "action": "replace_unpublished_trials_with_fixed_pool_batch",
        "mode": "apply" if args.apply else "dry-run",
        "plan_path": str(plan_path),
        "pool_path": str(pool_path),
        "preflight": preflight,
    }
    if not args.apply:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("[trial-pool-migration] dry run only; rerun with --apply")
        return 0

    lock_path = args.lock.resolve()
    try:
        lock_path.mkdir(parents=False)
    except FileExistsError as exc:
        raise SystemExit(f"Scheduler lock is already held: {lock_path}") from exc

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = (
        ROOT / "state" / "backups" / f"aibrief_jp_trial_pool_rotation_{stamp}"
    )
    backup_db = backup_dir / "reels.db"
    backup_facebook = backup_dir / "facebook.db"
    old_manifest_sources: dict[str, Path] = {}
    applied_manifests: list[Path] = []
    try:
        # Rehearse again while the publisher lock is held, then snapshot exact
        # recovery inputs before the first live mutation.
        preflight = rehearse(
            db_path=db_path,
            facebook_db=facebook_db,
            plan=plan,
            pool_path=pool_path,
            render_results=render_results,
        )
        retirement = preflight["retirement"]
        regular_before = active_regular_snapshot(db_path, str(plan["channel_id"]))
        facebook_before = active_regular_snapshot(
            facebook_db, str(plan["channel_id"])
        )
        sqlite_backup(db_path, backup_db)
        sqlite_backup(facebook_db, backup_facebook)
        old_manifest_sources = {}
        with reel_ledger.connect(db_path) as conn:
            for old in retirement["rows"]:
                row = reel_ledger.get_reel(
                    conn, str(old["content_hash"]), str(plan["channel_id"])
                )
                old_manifest_sources[str(old["content_hash"])] = Path(
                    str(row["manifest_path"])
                )
        copied = copy_retired_manifests(
            db_path=db_path,
            channel_id=str(plan["channel_id"]),
            rows=retirement["rows"],
            destination=backup_dir / "old_manifests",
        )
        shutil.copy2(plan_path, backup_dir / plan_path.name)
        shutil.copy2(pool_path, backup_dir / pool_path.name)
        write_json(backup_dir / "preflight.json", preflight)

        retired = reel_scheduler.retire_unpublished_trial_reels(
            db_path=db_path,
            channel_id=str(plan["channel_id"]),
            apply=True,
            reason="replaced by fixed photo-pool rotation",
        )
        replacements = schedule_entries(
            db_path=db_path,
            plan=plan,
            pool_path=pool_path,
            render_results=render_results,
            out_dir=out_dir,
            apply=True,
        )
        applied_manifests = [
            Path(str(item["manifest_path"]))
            for item in replacements
            if item.get("manifest_path")
        ]
        start_at = facebook_sync.parse_aware_datetime(
            facebook_sync.configured_start_at(str(plan["channel_id"])),
            field="start_at",
        )
        facebook_counts = facebook_sync.sync_queue(
            source_db=db_path,
            facebook_db=facebook_db,
            channel_id=str(plan["channel_id"]),
            start_at=start_at,
        )
        verification = verify_live(
            db_path=db_path,
            facebook_db=facebook_db,
            plan=plan,
            render_results=render_results,
            old_rows=retirement["rows"],
            regular_before=regular_before,
            facebook_before=facebook_before,
        )
        result.update(
            {
                "backup_dir": str(backup_dir),
                "retired": retired,
                "replacement_count": len(replacements),
                "facebook_sync": facebook_counts,
                "verification": verification,
            }
        )
        write_json(backup_dir / "apply_report.json", result)
        write_json(out_dir / "migration_report.json", result)
    except BaseException:
        if backup_db.is_file() and backup_facebook.is_file():
            restore_sqlite(backup_db, db_path)
            restore_sqlite(backup_facebook, facebook_db)
            for content_hash, original_path in old_manifest_sources.items():
                saved = backup_dir / "old_manifests" / f"{content_hash}.json"
                if saved.is_file():
                    shutil.copy2(saved, original_path)
            for manifest_path in applied_manifests:
                if manifest_path.is_file():
                    rollback_path = backup_dir / "rolled_back_manifests" / manifest_path.name
                    rollback_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(manifest_path), rollback_path)
        raise
    finally:
        lock_path.rmdir()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
