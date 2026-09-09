"""Read available content for every account record holder, without inventing rankings."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import verified_winner_library as winner_library


def build_record_holder_content(
    report: Mapping[str, Any], records: Mapping[str, Any], library: Mapping[str, Any],
) -> dict[str, Any]:
    """Return content/provenance snapshots for current fixed-window and lifetime leaders.

    Library content is reused exactly when available. Other record holders use
    the same artifact resolution, subtitle parsing and published-media hash
    checks as the library; no Top-10 membership or evidence tier is fabricated.
    """
    mapping, text = winner_library._mapping, winner_library._text
    holders = {
        str(row["media_id"]): row
        for record in mapping(records.get("current_records")).values()
        for row in mapping(record).get("leaders", []) if row.get("media_id")
    }
    winners = {str(mapping(row.get("identity")).get("media_id")): row
               for row in library.get("winners", [])}
    posts = {str(mapping(post.get("identity")).get("media_id")): post
             for post in report.get("posts", [])}
    result = {}
    for media_id in sorted(holders):
        if media_id in winners:
            result[media_id] = {
                key: copy.deepcopy(winners[media_id].get(key))
                for key in ("identity", "content", "source", "asset_provenance", "evidence_flags")
            }
            continue
        post = posts.get(media_id, {"identity": holders[media_id]})
        identity, metadata = mapping(post.get("identity")), mapping(post.get("content_metadata"))
        artifact = mapping(post.get("generation_artifact"))
        resolution = winner_library.resolve_clip_dir(post)
        directory = Path(resolution["path"]) if resolution.get("path") else None
        notes_path = directory / "notes.json" if directory else None
        notes = winner_library._read_json(notes_path)
        japanese_path = directory / "subtitles.ja.ass" if directory else None
        english_path = directory / "subtitles.en.ass" if directory else None
        japanese = winner_library.read_ass_segments(japanese_path)
        english = winner_library.read_ass_segments(english_path)
        japanese_text = " ".join(segment["text"] for segment in japanese)
        transcript = text(notes.get("transcript")) or " ".join(segment["text"] for segment in english)
        transcript_path = notes_path if text(notes.get("transcript")) else english_path if english else None
        caption = text(identity.get("caption"))
        published_hook = winner_library._first_caption_line(caption)
        hook_source = "published_caption_first_line" if published_hook else "generation_pipeline_hook_text"
        hook = published_hook or text(metadata.get("hook_text"))
        verification = winner_library.verify_published_asset(post, directory)
        confidence = resolution.get("confidence", "medium") if japanese else "unavailable"
        script_basis = japanese_text or transcript or text(identity.get("content_hash"))
        flags = []
        if not japanese:
            flags.append("JAPANESE_SCRIPT_UNAVAILABLE")
        elif confidence != "high":
            flags.append("TRANSCRIPT_MEDIUM_CONFIDENCE")
        if verification["status"] in {"MISMATCH", "UNAVAILABLE"}:
            flags.append("PUBLISHED_ASSET_HASH_MISMATCH" if verification["status"] == "MISMATCH"
                         else "PUBLISHED_ASSET_UNAVAILABLE")
        source_url = text(metadata.get("source"))
        result[media_id] = {
            "identity": {key: identity.get(key) for key in
                         ("media_id", "permalink", "published_at", "content_hash", "caption")},
            "source": {
                "url": source_url or None, "video_id": winner_library._youtube_video_id(source_url) or None,
                "title": text(artifact.get("source_title")) or None,
                "uploader": text(artifact.get("source_uploader")) or None,
                "chapter": text(notes.get("source_chapter")) or None,
            },
            "content": {
                "published_hook": {"value": hook or None, "source": hook_source,
                                   "confidence": "high" if hook else "unavailable"},
                "opening_japanese_script": [segment["text"] for segment in japanese
                    if segment.get("start_seconds") is None or segment["start_seconds"] < 3.1]
                    or ([japanese[0]["text"]] if japanese else []),
                "japanese_script": {"status": "AVAILABLE" if japanese else "UNAVAILABLE",
                    "text": japanese_text or None, "segments": japanese,
                    "source_path": str(japanese_path.resolve()) if japanese else None,
                    "confidence": confidence},
                "source_transcript": {"status": "AVAILABLE" if transcript else "UNAVAILABLE",
                    "text": transcript or None,
                    "source_path": str(transcript_path.resolve()) if transcript_path else None},
                "script_asset_id": hashlib.sha256(script_basis.encode("utf-8")).hexdigest()[:16]
                    if script_basis else None,
            },
            "asset_provenance": {"clip_resolution": resolution, "published_asset": verification},
            "evidence_flags": flags,
        }
    return result
