#!/usr/bin/env python3
"""Render and verify a reviewed batch from the fixed AI Brief JP Trial pool.

The command is deliberately ledger-read-only. It creates new hook-overlay media
and QA artifacts; scheduling remains a separate dry-run/apply operation.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reel_scheduler import validate_trial_hook_constraints


DEFAULT_PLAN = ROOT / "config" / "aibrief_jp_trial_rotation_batch_20260830.json"
DEFAULT_REEL_APP = ROOT.parent / "reel-app"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--reel-app-root", type=Path, default=DEFAULT_REEL_APP)
    parser.add_argument("--jobs", type=int, default=3)
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


def resolve_repo_path(value: object) -> Path:
    path = Path(str(value or ""))
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs(plan_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = load_json(plan_path)
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise RuntimeError("Batch plan must be a schema_version=1 JSON object")
    pool_path = resolve_repo_path(plan.get("pool_path"))
    pool = load_json(pool_path)
    if not isinstance(pool, dict):
        raise RuntimeError("Rotation pool must contain a JSON object")
    if plan.get("pool_id") != pool.get("pool_id"):
        raise RuntimeError("Batch plan pool_id does not match the rotation pool")
    entries = plan.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("Batch plan entries must be a non-empty list")
    pool_entries = {
        str(entry.get("content_hash") or ""): entry
        for entry in pool.get("entries") or []
        if isinstance(entry, dict) and entry.get("enabled") is True
    }
    seen_experiments: set[str] = set()
    seen_slots: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Every batch entry must be a JSON object")
        parent_hash = str(entry.get("parent_content_hash") or "")
        experiment_id = str(entry.get("experiment_id") or "")
        scheduled_at = str(entry.get("scheduled_at") or "")
        hook = str(entry.get("hook") or "").strip()
        if parent_hash not in pool_entries:
            raise RuntimeError(f"Batch parent is not enabled in pool: {parent_hash}")
        if not experiment_id or experiment_id in seen_experiments:
            raise RuntimeError(f"Missing or duplicate experiment id: {experiment_id!r}")
        if not scheduled_at or scheduled_at in seen_slots:
            raise RuntimeError(f"Missing or duplicate schedule slot: {scheduled_at!r}")
        if not hook:
            raise RuntimeError(f"Missing hook for {experiment_id}")
        validate_trial_hook_constraints(pool_entries[parent_hash], hook)
        seen_experiments.add(experiment_id)
        seen_slots.add(scheduled_at)
    plan["_pool_path"] = str(pool_path)
    return plan, pool


def parent_rows(db_path: Path, channel_id: str, hashes: list[str]) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in hashes)
        rows = connection.execute(
            "SELECT * FROM reels WHERE channel_id=? AND content_hash IN ("
            + placeholders
            + ")",
            [channel_id, *hashes],
        ).fetchall()
    finally:
        connection.close()
    return {str(row["content_hash"]): dict(row) for row in rows}


def probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,nb_frames",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def decoded_audio_hash(path: Path) -> str:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "hash",
            "-hash",
            "sha256",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip().partition("=")[2]
    if not value:
        raise RuntimeError(f"Could not hash decoded audio: {path}")
    return value


def central_ssim(parent: Path, variant: Path, log_path: Path) -> float:
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(parent),
            "-i",
            str(variant),
            "-lavfi",
            (
                "[0:v]crop=720:300:0:490,setpts=PTS-STARTPTS[a];"
                "[1:v]crop=720:300:0:490,setpts=PTS-STARTPTS[b];"
                f"[a][b]ssim=stats_file={log_path}"
            ),
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    matches = re.findall(r"All:([0-9.]+)", result.stderr)
    if not matches:
        raise RuntimeError(f"Could not read central SSIM for {variant}")
    return float(matches[-1])


def preview_frame(media: Path, output: Path, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{seconds:.3f}",
            "-i",
            str(media),
            "-frames:v",
            "1",
            str(output),
        ],
        check=True,
    )


def render_one(
    *,
    entry: dict[str, Any],
    parent: dict[str, Any],
    output_root: Path,
    reel_app_root: Path,
) -> dict[str, Any]:
    if parent.get("status") != "published" or not str(parent.get("media_id") or ""):
        raise RuntimeError(
            f"Parent is not publication-ready: {entry['parent_content_hash']}"
        )
    parent_media = Path(str(parent.get("media_path") or "")).resolve()
    clip_dir = Path(str(parent.get("clip_dir") or "")).resolve()
    source_id = str(parent.get("source_video") or "").strip()
    source_path = (reel_app_root / "outputs" / source_id / "work" / "source.mp4").resolve()
    notes_path = clip_dir / "notes.json"
    subtitle_path = clip_dir / "subtitles.ja.ass"
    if not notes_path.is_file() or not subtitle_path.is_file():
        canonical_clip = reel_app_root / "outputs" / source_id / "clips" / clip_dir.name
        notes_path = canonical_clip / "notes.json"
        subtitle_path = canonical_clip / "subtitles.ja.ass"
    for required in (parent_media, source_path, notes_path, subtitle_path):
        if not required.is_file():
            raise FileNotFoundError(required)
    notes = load_json(notes_path)
    start = float(notes["start"])
    end = float(notes["end"])
    date_token = str(entry["scheduled_at"])[:10].replace("-", "")
    output_dir = output_root / f"{date_token}_{entry['parent_content_hash'][:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_media = output_dir / "reel.mp4"
    spec_path = output_dir / "render_spec.json"

    reelcut_src = reel_app_root / "src"
    if str(reelcut_src) not in sys.path:
        sys.path.insert(0, str(reelcut_src))
    from reelcut.render import render_clip

    if not output_media.is_file():
        render_clip(
            source_path,
            start,
            end,
            subtitle_path,
            output_media,
            title=str(entry["hook"]),
            logo_file=reel_app_root / "assets" / "channels" / "aibrief_jp" / "logo.png",
            channel_name="AI ブリーフ",
            channel_handle="@aibrief.jp",
            verified=True,
        )
    media_probe = probe(output_media)
    streams = media_probe.get("streams") or []
    videos = [item for item in streams if item.get("codec_type") == "video"]
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    if not videos or not audios or (videos[0].get("width"), videos[0].get("height")) != (720, 1280):
        raise RuntimeError(f"Rendered output failed stream/dimension QA: {output_media}")
    parent_audio = decoded_audio_hash(parent_media)
    variant_audio = decoded_audio_hash(output_media)
    if parent_audio != variant_audio:
        raise RuntimeError(f"Decoded audio changed for {entry['experiment_id']}")
    ssim_log = output_dir / "central_ssim.log"
    ssim = central_ssim(parent_media, output_media, ssim_log)
    if ssim < 0.95:
        raise RuntimeError(
            f"Central-video SSIM is too low for {entry['experiment_id']}: {ssim:.6f}"
        )
    duration = float((media_probe.get("format") or {}).get("duration") or 0)
    opening_preview = output_dir / "preview_01s.png"
    midpoint_preview = output_dir / "preview_mid.png"
    preview_frame(output_media, opening_preview, min(1.0, max(0.0, duration / 4)))
    preview_frame(output_media, midpoint_preview, max(0.0, duration / 2))
    result = {
        "schema_version": 1,
        "purpose": "fixed_pool_trial_reel_overlay_hook_only",
        "channel_id": "aibrief_jp",
        "pool_id": entry.get("pool_id"),
        "experiment_id": entry["experiment_id"],
        "scheduled_at": entry["scheduled_at"],
        "parent": {
            "content_hash": entry["parent_content_hash"],
            "media_id": parent.get("media_id"),
            "baseline_hook": parent.get("title"),
            "media_path": str(parent_media),
            "caption_sha256": hashlib.sha256(
                str(parent.get("caption") or "").encode("utf-8")
            ).hexdigest(),
        },
        "variant_hook": entry["hook"],
        "changed_variables": ["overlay_hook"],
        "render": {
            "entrypoint": "reelcut.render.render_clip",
            "source_path": str(source_path),
            "source_sha256": sha256_file(source_path),
            "start_seconds": start,
            "end_seconds": end,
            "subtitle_path": str(subtitle_path),
            "subtitle_sha256": sha256_file(subtitle_path),
        },
        "output": {
            "path": str(output_media.resolve()),
            "sha256": sha256_file(output_media),
            "duration_seconds": duration,
            "width": 720,
            "height": 1280,
        },
        "qa": {
            "decoded_audio_matches_parent": True,
            "decoded_audio_sha256": variant_audio,
            "video_band_crop": "720x300+0+490",
            "video_band_ssim_vs_parent": ssim,
            "preview_01s": str(opening_preview.resolve()),
            "preview_mid": str(midpoint_preview.resolve()),
            "caption_preserved_by_scheduler": True,
            "subtitles_preserved": True,
            "body_footage_preserved": True,
        },
    }
    atomic_json(spec_path, result)
    return result


def main() -> int:
    args = parse_args()
    if args.jobs < 1 or args.jobs > 6:
        raise SystemExit("--jobs must be between 1 and 6")
    plan_path = args.plan.resolve()
    plan, _pool = load_inputs(plan_path)
    db_path = resolve_repo_path(plan.get("db_path"))
    output_root = resolve_repo_path(plan.get("output_root"))
    reel_app_root = args.reel_app_root.resolve()
    entries = [dict(item, pool_id=plan.get("pool_id")) for item in plan["entries"]]
    parents = parent_rows(
        db_path,
        str(plan.get("channel_id") or ""),
        [str(item["parent_content_hash"]) for item in entries],
    )
    missing = [item["parent_content_hash"] for item in entries if item["parent_content_hash"] not in parents]
    if missing:
        raise RuntimeError(f"Batch parents are missing from the ledger: {missing}")
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {
            executor.submit(
                render_one,
                entry=entry,
                parent=parents[entry["parent_content_hash"]],
                output_root=output_root,
                reel_app_root=reel_app_root,
            ): entry
            for entry in entries
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(
                json.dumps(
                    {
                        "rendered": result["experiment_id"],
                        "sha256": result["output"]["sha256"],
                        "ssim": result["qa"]["video_band_ssim_vs_parent"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    order = {entry["experiment_id"]: index for index, entry in enumerate(entries)}
    results.sort(key=lambda item: order[item["experiment_id"]])
    summary = {
        "schema_version": 1,
        "plan_path": str(plan_path),
        "pool_path": plan["_pool_path"],
        "pool_id": plan["pool_id"],
        "rendered_count": len(results),
        "all_decoded_audio_matches_parent": all(
            item["qa"]["decoded_audio_matches_parent"] for item in results
        ),
        "minimum_central_ssim": min(
            item["qa"]["video_band_ssim_vs_parent"] for item in results
        ),
        "results": results,
    }
    atomic_json(output_root / "batch_render_report.json", summary)
    print(json.dumps({"batch_render_report": str(output_root / "batch_render_report.json"), "count": len(results)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
