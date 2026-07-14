#!/usr/bin/env python3
"""Prepare and validate isolated rerenders for approved impeachment hooks.

This command never writes to the live reel output tree or scheduler database.
It materializes one renderer-compatible directory per source video beneath a
non-scanned staging root, while reusing the exact live clip boundaries and
subtitle files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_REVIEW = Path(
    "out/reel_schedules/facebook_impeachment/hook_refresh_review.varied.json"
)
DEFAULT_REEL_ROOT = Path(
    "/Users/aiagent/GitHub/reel-app/outputs/impeachments_news"
)
DEFAULT_STAGING_PARENT = Path(
    "out/reel_schedules/facebook_impeachment/hook_refresh_staging"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "validate"))
    parser.add_argument("--review", type=Path, default=DEFAULT_REVIEW)
    parser.add_argument("--reel-root", type=Path, default=DEFAULT_REEL_ROOT)
    parser.add_argument("--staging-root", type=Path)
    parser.add_argument("--expected-count", type=int, default=29)
    parser.add_argument("--ffprobe", default="ffprobe")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_review(path: Path, expected_count: int) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    items = payload.get("items") or []
    failures: list[str] = []
    if len(items) != expected_count:
        failures.append(f"expected {expected_count} items, found {len(items)}")
    if payload.get("approved_count") != expected_count:
        failures.append(f"approved_count={payload.get('approved_count')!r}")
    if payload.get("rejected_count") != 0:
        failures.append(f"rejected_count={payload.get('rejected_count')!r}")
    if payload.get("selection_profile") != "ph-impeachment-news":
        failures.append("wrong selection_profile")
    if payload.get("selection_profile_version") != 3:
        failures.append("selection_profile_version is not 3")
    if payload.get("rendered") is not False or payload.get("queue_modified") is not False:
        failures.append("review is not a pristine pre-render artifact")
    seen_hashes: set[str] = set()
    seen_identities: set[tuple[str, int]] = set()
    for item in items:
        identity = (str(item.get("source_id")), int(item.get("candidate_index", -1)))
        content_hash = str(item.get("content_hash") or "")
        assessment = item.get("hook_assessment") or {}
        if item.get("status") != "approved":
            failures.append(f"{identity}: not approved")
        if not str(item.get("new_hook") or "").strip():
            failures.append(f"{identity}: missing new hook")
        if item.get("validation_failures"):
            failures.append(f"{identity}: validation failures present")
        if assessment.get("formula_pass") is not True:
            failures.append(f"{identity}: formula gate failed")
        if assessment.get("surface_diversity_pass") is not True:
            failures.append(f"{identity}: surface-diversity gate failed")
        if content_hash in seen_hashes:
            failures.append(f"duplicate old hash {content_hash}")
        if identity in seen_identities:
            failures.append(f"duplicate identity {identity}")
        seen_hashes.add(content_hash)
        seen_identities.add(identity)
    if failures:
        raise RuntimeError("Review preflight failed: " + "; ".join(failures))
    return payload, hashlib.sha256(raw).hexdigest()


def staging_root_for(args: argparse.Namespace, review_sha: str) -> Path:
    if args.staging_root:
        return args.staging_root.resolve()
    return (DEFAULT_STAGING_PARENT / review_sha[:12]).resolve()


def validate_live_queue(review: dict[str, Any]) -> None:
    db_path = Path(review["queue_database"])
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        queued = {
            row["content_hash"]: row
            for row in connection.execute(
                """
                SELECT content_hash, channel_id, status, published_at, media_path,
                       scheduled_at, title
                  FROM reels
                 WHERE status IN ('scheduled', 'publish_previewed')
                   AND published_at IS NULL
                """
            )
        }
    finally:
        connection.close()
    if len(queued) != len(review["items"]):
        raise RuntimeError(
            f"Queue changed since review: expected {len(review['items'])} rows, found {len(queued)}"
        )
    for item in review["items"]:
        old_hash = item["content_hash"]
        row = queued.get(old_hash)
        if row is None:
            raise RuntimeError(f"Reviewed queue row is no longer scheduled: {old_hash}")
        media_path = Path(row["media_path"])
        if media_path.resolve() != Path(item["media_path"]).resolve():
            raise RuntimeError(f"Media path changed for {old_hash}")
        if row["scheduled_at"] != item["scheduled_at"]:
            raise RuntimeError(f"Schedule changed for {old_hash}")
        if sha256_file(media_path) != old_hash:
            raise RuntimeError(f"Live media hash drifted for {old_hash}")


def prepare(args: argparse.Namespace) -> int:
    review_path = args.review.resolve()
    review, review_sha = load_review(review_path, args.expected_count)
    validate_live_queue(review)
    reel_root = args.reel_root.resolve()
    staging_root = staging_root_for(args, review_sha)
    if staging_root.exists():
        raise FileExistsError(
            f"Staging root already exists; refusing to mix render attempts: {staging_root}"
        )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in review["items"]:
        grouped[str(item["source_id"])].append(item)

    plan_items: list[dict[str, Any]] = []
    for source_id, items in sorted(grouped.items()):
        live_source_dir = reel_root / source_id
        stage_source_dir = staging_root / source_id
        stage_work_dir = stage_source_dir / "work"
        stage_work_dir.mkdir(parents=True)

        for filename in ("transcript.en.json", "metadata.json"):
            source = live_source_dir / filename
            if not source.is_file():
                raise FileNotFoundError(source)
            shutil.copy2(source, stage_source_dir / filename)
        source_json = live_source_dir / "work" / "source.json"
        source_video = live_source_dir / "work" / "source.mp4"
        if not source_json.is_file() or not source_video.is_file():
            raise FileNotFoundError(f"Source work files missing for {source_id}")
        shutil.copy2(source_json, stage_work_dir / "source.json")
        # The renderer only reads the source. A symlink avoids duplicating roughly
        # two gigabytes of source media and does not mutate the live source inode.
        (stage_work_dir / "source.mp4").symlink_to(source_video)

        original_candidates = json.loads(
            (live_source_dir / "candidates.json").read_text(encoding="utf-8")
        )
        caption_source = original_candidates.get("caption_source")
        clips: list[dict[str, Any]] = []
        for item in sorted(items, key=lambda value: value["scheduled_at"]):
            live_media = Path(item["media_path"]).resolve()
            live_clip_dir = live_media.parent
            live_notes_path = live_clip_dir / "notes.json"
            live_subtitle = live_clip_dir / "subtitles.en.ass"
            if not live_notes_path.is_file() or not live_subtitle.is_file():
                raise FileNotFoundError(f"Live clip inputs missing for {live_clip_dir}")
            notes = json.loads(live_notes_path.read_text(encoding="utf-8"))
            identity = (
                int(notes.get("index", -1)),
                float(notes.get("start", -1)),
                float(notes.get("end", -1)),
                str(notes.get("slug") or ""),
            )
            reviewed_identity = (
                int(item["candidate_index"]),
                float(item["start"]),
                float(item["end"]),
                str(item["slug"]),
            )
            if identity != reviewed_identity:
                raise RuntimeError(
                    f"Live candidate identity drifted for {item['content_hash']}: "
                    f"{identity!r} != {reviewed_identity!r}"
                )
            notes["one_liner"] = item["new_hook"]
            notes["hook_variants"] = [item["new_hook"]]
            notes["hook_assessment"] = item["hook_assessment"]
            notes["hook_score"] = item["hook_score"]
            notes["value_score"] = item["value_score"]
            notes["score"] = item["score"]
            notes["reason"] = item["reason"]
            clips.append(notes)

            stage_clip_dir = stage_source_dir / "clips" / item["slug"]
            stage_clip_dir.mkdir(parents=True)
            stage_subtitle = stage_clip_dir / "subtitles.en.ass"
            shutil.copy2(live_subtitle, stage_subtitle)
            plan_items.append(
                {
                    **item,
                    "live_notes_path": str(live_notes_path),
                    "live_subtitle_path": str(live_subtitle),
                    "live_subtitle_sha256": sha256_file(live_subtitle),
                    "staged_source_dir": str(stage_source_dir),
                    "staged_media_path": str(
                        stage_clip_dir / "reel.en.vibecodersph.mp4"
                    ),
                    "staged_notes_path": str(stage_clip_dir / "notes.json"),
                    "staged_subtitle_path": str(stage_subtitle),
                }
            )

        atomic_json(
            stage_source_dir / "candidates.json",
            {
                "caption_source": caption_source,
                "selection_mode": "ai",
                "selection_profile": "ph-impeachment-news",
                "selection_profile_version": 3,
                "clips": clips,
            },
        )

    atomic_json(
        staging_root / "stage_plan.json",
        {
            "schema_version": 1,
            "review_path": str(review_path),
            "review_sha256": review_sha,
            "reel_root": str(reel_root),
            "expected_count": args.expected_count,
            "source_counts": {
                source_id: len(items) for source_id, items in sorted(grouped.items())
            },
            "rendered": False,
            "validated": False,
            "items": plan_items,
        },
    )
    print(
        json.dumps(
            {
                "staging_root": str(staging_root),
                "review_sha256": review_sha,
                "sources": {key: len(value) for key, value in sorted(grouped.items())},
                "clips": len(plan_items),
            },
            ensure_ascii=False,
        )
    )
    return 0


def probe_media(ffprobe: str, path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def validate(args: argparse.Namespace) -> int:
    review_path = args.review.resolve()
    review, review_sha = load_review(review_path, args.expected_count)
    staging_root = staging_root_for(args, review_sha)
    plan_path = staging_root / "stage_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("review_sha256") != review_sha:
        raise RuntimeError("Staging plan does not match the approved review bytes")
    items = plan.get("items") or []
    if len(items) != args.expected_count:
        raise RuntimeError(f"Staging plan contains {len(items)} items")

    review_by_hash = {item["content_hash"]: item for item in review["items"]}
    new_hashes: set[str] = set()
    results: list[dict[str, Any]] = []
    for item in items:
        approved = review_by_hash.get(item["content_hash"])
        if approved is None or approved["new_hook"] != item["new_hook"]:
            raise RuntimeError(f"Staging item drifted from review: {item['content_hash']}")
        staged_media = Path(item["staged_media_path"])
        staged_notes = Path(item["staged_notes_path"])
        staged_subtitle = Path(item["staged_subtitle_path"])
        for required in (staged_media, staged_notes, staged_subtitle):
            if not required.is_file() or required.stat().st_size == 0:
                raise FileNotFoundError(f"Missing or empty staged artifact: {required}")
        if sha256_file(staged_subtitle) != item["live_subtitle_sha256"]:
            raise RuntimeError(f"Subtitle bytes changed for {item['content_hash']}")
        notes = json.loads(staged_notes.read_text(encoding="utf-8"))
        if notes.get("one_liner") != item["new_hook"]:
            raise RuntimeError(f"Staged notes have the wrong hook for {item['content_hash']}")
        if notes.get("hook_assessment", {}).get("formula_pass") is not True:
            raise RuntimeError(f"Formula gate missing in staged notes for {item['content_hash']}")
        if notes.get("hook_assessment", {}).get("surface_diversity_pass") is not True:
            raise RuntimeError(
                f"Surface-diversity gate missing in staged notes for {item['content_hash']}"
            )
        identity = (
            int(notes.get("index", -1)),
            float(notes.get("start", -1)),
            float(notes.get("end", -1)),
            str(notes.get("slug") or ""),
        )
        expected_identity = (
            int(item["candidate_index"]),
            float(item["start"]),
            float(item["end"]),
            str(item["slug"]),
        )
        if identity != expected_identity:
            raise RuntimeError(f"Staged notes identity changed for {item['content_hash']}")

        new_hash = sha256_file(staged_media)
        if new_hash == item["content_hash"]:
            raise RuntimeError(f"Rerender bytes did not change for {item['content_hash']}")
        if new_hash in new_hashes:
            raise RuntimeError(f"Duplicate staged media hash: {new_hash}")
        new_hashes.add(new_hash)

        probe = probe_media(args.ffprobe, staged_media)
        streams = probe.get("streams") or []
        video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if not video_streams or not audio_streams:
            raise RuntimeError(f"Missing audio/video stream in {staged_media}")
        if (video_streams[0].get("width"), video_streams[0].get("height")) != (720, 1280):
            raise RuntimeError(f"Wrong dimensions in {staged_media}")
        duration = float((probe.get("format") or {}).get("duration") or 0)
        expected_duration = float(item["end"]) - float(item["start"])
        if abs(duration - expected_duration) > 1.5:
            raise RuntimeError(
                f"Duration mismatch for {staged_media}: {duration:.3f}s vs {expected_duration:.3f}s"
            )
        results.append(
            {
                "content_hash": item["content_hash"],
                "new_content_hash": new_hash,
                "source_id": item["source_id"],
                "candidate_index": item["candidate_index"],
                "staged_media_path": str(staged_media),
                "bytes": staged_media.stat().st_size,
                "duration": duration,
                "width": 720,
                "height": 1280,
                "audio": True,
                "video": True,
            }
        )

    partials = list(staging_root.rglob("*.partial.mp4"))
    staged_reels = list(staging_root.rglob("reel.en.vibecodersph.mp4"))
    staged_notes = list(staging_root.rglob("notes.json"))
    if partials:
        raise RuntimeError(f"Incomplete render files remain: {partials}")
    if len(staged_reels) != args.expected_count or len(staged_notes) != args.expected_count:
        raise RuntimeError(
            f"Unexpected staged counts: reels={len(staged_reels)}, notes={len(staged_notes)}"
        )

    plan["rendered"] = True
    plan["validated"] = True
    plan["render_results"] = results
    atomic_json(plan_path, plan)
    atomic_json(
        staging_root / "validation_report.json",
        {
            "schema_version": 1,
            "review_sha256": review_sha,
            "validated_count": len(results),
            "partials": 0,
            "all_subtitles_byte_identical": True,
            "all_dimensions_720x1280": True,
            "all_have_audio_video": True,
            "all_media_hashes_changed": True,
            "results": results,
        },
    )
    print(
        json.dumps(
            {
                "staging_root": str(staging_root),
                "validated": len(results),
                "unique_new_hashes": len(new_hashes),
                "partials": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    args = parse_args()
    return prepare(args) if args.action == "prepare" else validate(args)


if __name__ == "__main__":
    raise SystemExit(main())
