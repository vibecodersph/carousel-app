#!/usr/bin/env python3
"""Short local music clips for carousel cover videos.

Instagram's Graph API cannot choose in-app carousel music for us, so this module
keeps music as a render-time concern: pick a local clip, trim/fade it, and mux it
into a carousel video item before upload.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent
DEFAULT_MUSIC_LIBRARY = ROOT / "assets" / "music" / "library.json"
DEFAULT_SHORT_CLIP_SECONDS = 60.0
MIN_SHORT_CLIP_SECONDS = 3.0
MAX_SHORT_CLIP_SECONDS = 60.0
DEFAULT_VOLUME = 0.42
DEFAULT_FADE_SECONDS = 0.7
SUPPORTED_AUDIO_SUFFIXES = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".wav"}


@dataclass(frozen=True)
class MusicTrack:
    clip_id: str
    path: Path
    title: str = ""
    source: str = ""
    start_seconds: float = 0.0
    duration_seconds: float = DEFAULT_SHORT_CLIP_SECONDS
    volume: float = DEFAULT_VOLUME
    fade_seconds: float = DEFAULT_FADE_SECONDS


def _string(value: object) -> str:
    return str(value or "").strip()


def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def short_clip_duration(value: object, default: float = DEFAULT_SHORT_CLIP_SECONDS) -> float:
    duration = _float(value, default)
    return min(MAX_SHORT_CLIP_SECONDS, max(MIN_SHORT_CLIP_SECONDS, duration))


def _resolve_track_path(raw_path: object, library_path: Path) -> Path:
    path_text = _string(raw_path)
    if not path_text:
        raise SystemExit(f"Music library track is missing path: {library_path}")
    path = Path(path_text)
    if not path.is_absolute():
        path = library_path.parent / path
    return path.resolve()


def _track_from_record(record: dict[str, Any], library_path: Path) -> MusicTrack:
    path = _resolve_track_path(record.get("path"), library_path)
    if path.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise SystemExit(f"Unsupported music clip file type: {path}")
    if not path.exists():
        raise SystemExit(f"Music clip does not exist: {path}")
    clip_id = _string(record.get("id")) or path.stem
    return MusicTrack(
        clip_id=clip_id,
        path=path,
        title=_string(record.get("title")) or clip_id,
        source=_string(record.get("source")),
        start_seconds=max(0.0, _float(record.get("start_seconds", record.get("start")), 0.0)),
        duration_seconds=short_clip_duration(record.get("duration_seconds", record.get("duration"))),
        volume=min(1.0, max(0.0, _float(record.get("volume"), DEFAULT_VOLUME))),
        fade_seconds=max(0.0, _float(record.get("fade_seconds", record.get("fade")), DEFAULT_FADE_SECONDS)),
    )


def load_music_library(library_path: Path | None = None) -> list[MusicTrack]:
    library_path = (library_path or DEFAULT_MUSIC_LIBRARY).resolve()
    if not library_path.exists():
        return []
    try:
        payload = json.loads(library_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse music library JSON in {library_path}: {exc}") from exc
    records = payload.get("tracks") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise SystemExit(f"Music library must contain a tracks array: {library_path}")
    tracks = [_track_from_record(record, library_path) for record in records if isinstance(record, dict)]
    return [track for track in tracks if track.clip_id]


def select_music_track(
    *,
    library_path: Path | None,
    carousel_id: str,
    requested_clip_id: str = "",
) -> MusicTrack | None:
    tracks = load_music_library(library_path)
    if not tracks:
        return None
    requested_clip_id = _string(requested_clip_id)
    if requested_clip_id:
        for track in tracks:
            if track.clip_id == requested_clip_id:
                return track
        raise SystemExit(f"Music clip id not found in library: {requested_clip_id}")
    key = carousel_id or tracks[0].clip_id
    index = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % len(tracks)
    return tracks[index]


def music_manifest(track: MusicTrack, *, duration_seconds: float) -> dict[str, Any]:
    data = asdict(track)
    data["path"] = str(track.path)
    data["duration_seconds"] = round(duration_seconds, 3)
    return data


def add_music_to_video(
    video_path: Path,
    track: MusicTrack,
    out_path: Path,
    *,
    duration_seconds: float | None = None,
    run_command: Callable[[list[str]], object],
    loop_video: bool = False,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required to add carousel music clips")

    duration = short_clip_duration(duration_seconds, track.duration_seconds)
    fade = min(max(0.0, track.fade_seconds), duration / 2)
    fade_out_start = max(0.0, duration - fade)
    filters = [
        f"volume={track.volume:.3f}",
        f"afade=t=in:st=0:d={fade:.3f}",
        f"afade=t=out:st={fade_out_start:.3f}:d={fade:.3f}",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y"]
    if loop_video:
        command.extend(["-stream_loop", "-1"])
    command.extend(["-i", str(video_path), "-stream_loop", "-1"])
    if track.start_seconds > 0:
        command.extend(["-ss", f"{track.start_seconds:.3f}"])
    command.extend(
        [
            "-i",
            str(track.path),
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264" if loop_video else "copy",
            *([] if not loop_video else ["-pix_fmt", "yuv420p"]),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-af",
            ",".join(filters),
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )
    run_command(command)
    return out_path
