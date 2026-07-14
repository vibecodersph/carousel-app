#!/usr/bin/env python3
"""Generate validated v2 hooks for queued impeachment reels without rendering.

This maintenance command is intentionally read-only with respect to the scheduler
database and reel output tree.  It writes a review artifact that can be approved
before any media, captions, manifests, or ledger identities are changed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_REEL_APP = Path("/Users/aiagent/GitHub/reel-app")
DEFAULT_REEL_ROOT = DEFAULT_REEL_APP / "outputs" / "impeachments_news"
DEFAULT_DB = Path("state/facebook_impeachment.db")
DEFAULT_OUTPUT = Path("out/reel_schedules/facebook_impeachment/hook_refresh_review.json")


@dataclass(frozen=True)
class ScheduledItem:
    content_hash: str
    channel_id: str
    scheduled_at: str
    media_path: Path
    title: str
    caption: str
    source_video: str
    manifest_path: str
    source_id: str
    notes_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate formula-validated hooks for scheduled, unpublished impeachment reels. "
            "No reels are rendered and the queue is not modified."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reel-app", type=Path, default=DEFAULT_REEL_APP)
    parser.add_argument("--reel-root", type=Path, default=DEFAULT_REEL_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-count", type=int, default=29)
    parser.add_argument("--min-score", type=float, default=7.0)
    parser.add_argument(
        "--reuse-responses-from",
        type=Path,
        help=(
            "Revalidate the already-generated semantic repair responses in this review artifact "
            "without making another AI call."
        ),
    )
    parser.add_argument(
        "--editorial-overrides",
        type=Path,
        help=(
            "Optional reviewed hook/support-quote overrides. Each override is still required "
            "to pass the deterministic v2 hook gate."
        ),
    )
    return parser.parse_args()


def _atomic_json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _source_id_for(media_path: Path, reel_root: Path) -> str:
    try:
        relative = media_path.resolve().relative_to(reel_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Queued media is outside the impeachment output root: {media_path}") from exc
    if len(relative.parts) < 4 or relative.parts[1] != "clips":
        raise ValueError(f"Unexpected impeachment media layout: {media_path}")
    return relative.parts[0]


def load_scheduled_items(db_path: Path, reel_root: Path) -> list[ScheduledItem]:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT content_hash, channel_id, scheduled_at, media_path,
                   COALESCE(title, '') AS title,
                   COALESCE(caption, '') AS caption,
                   COALESCE(source_video, '') AS source_video,
                   COALESCE(manifest_path, '') AS manifest_path
              FROM reels
             WHERE status = 'scheduled'
               AND published_at IS NULL
             ORDER BY scheduled_at, content_hash
            """
        ).fetchall()
    finally:
        connection.close()

    items: list[ScheduledItem] = []
    for row in rows:
        media_path = Path(row["media_path"]).resolve()
        source_id = _source_id_for(media_path, reel_root)
        notes_path = media_path.parent / "notes.json"
        if not media_path.is_file():
            raise FileNotFoundError(f"Queued media is missing: {media_path}")
        if not notes_path.is_file():
            raise FileNotFoundError(f"Queued notes are missing: {notes_path}")
        items.append(
            ScheduledItem(
                content_hash=row["content_hash"],
                channel_id=row["channel_id"],
                scheduled_at=row["scheduled_at"],
                media_path=media_path,
                title=row["title"],
                caption=row["caption"],
                source_video=row["source_video"],
                manifest_path=row["manifest_path"],
                source_id=source_id,
                notes_path=notes_path,
            )
        )
    return items


def _load_candidate(notes_path: Path, candidate_type: type) -> Any:
    data = json.loads(notes_path.read_text(encoding="utf-8"))
    data.pop("duration", None)
    data.setdefault("hook_assessment", {})
    return candidate_type(**data)


def _load_video_title(reel_root: Path, source_id: str) -> str:
    metadata_path = reel_root / source_id / "metadata.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    title = str(payload.get("title") or source_id).strip()
    return title or source_id


def _word_count(text: str) -> int:
    return len(text.replace("—", " ").split())


def main() -> int:
    args = parse_args()
    reel_src = args.reel_app.resolve() / "src"
    if not reel_src.is_dir():
        raise FileNotFoundError(f"reelcut source directory is missing: {reel_src}")
    sys.path.insert(0, str(reel_src))

    # Import after inserting the separate reel-app source tree.  The current
    # worktree contains the v2 impeachment hook contract and deterministic gate.
    from reelcut.gemini import (  # pylint: disable=import-outside-toplevel
        DEFAULT_GEMINI_DISCRIMINATOR_MODEL,
        GeminiReelClient,
        _apply_ai_judgment,
        _impeachment_batch_surface_failures,
        _impeachment_hook_failures,
        _judgments_by_index,
    )
    from reelcut.models import Candidate  # pylint: disable=import-outside-toplevel
    from reelcut.reeltypes import (  # pylint: disable=import-outside-toplevel
        PH_IMPEACHMENT_REEL_TYPE,
        resolve_reel_type,
    )

    items = load_scheduled_items(args.db, args.reel_root)
    if len(items) != args.expected_count:
        raise RuntimeError(
            f"Expected {args.expected_count} scheduled unpublished reels, found {len(items)}; "
            "refusing to create a partial or stale review set."
        )

    grouped: dict[str, list[ScheduledItem]] = defaultdict(list)
    for item in items:
        grouped[item.source_id].append(item)

    editorial_overrides: dict[tuple[str, int], dict[str, Any]] = {}
    if args.editorial_overrides:
        override_payload = json.loads(args.editorial_overrides.read_text(encoding="utf-8"))
        for override in override_payload.get("overrides", []):
            if not isinstance(override, dict):
                raise TypeError("Every editorial override must be an object")
            key = (str(override.get("source_id") or ""), int(override["candidate_index"]))
            if not key[0] or key in editorial_overrides:
                raise ValueError(f"Invalid or duplicate editorial override key: {key}")
            editorial_overrides[key] = override
    used_editorial_overrides: set[tuple[str, int]] = set()

    profile = resolve_reel_type(PH_IMPEACHMENT_REEL_TYPE)
    reused_payload: dict[str, Any] | None = None
    reused_runs: dict[str, list[dict[str, Any]]] = {}
    if args.reuse_responses_from:
        reused_payload = json.loads(args.reuse_responses_from.read_text(encoding="utf-8"))
        for run in reused_payload.get("raw_runs", []):
            if not isinstance(run, dict) or not run.get("source_id"):
                continue
            reused_runs[str(run["source_id"])] = list(
                run.get("semantic_repair_responses") or []
            )
        client = None
        model_name = str(reused_payload.get("model") or DEFAULT_GEMINI_DISCRIMINATOR_MODEL)
    else:
        client = GeminiReelClient(model=DEFAULT_GEMINI_DISCRIMINATOR_MODEL)
        model_name = client.model
    review_by_hash: dict[str, dict[str, Any]] = {}
    raw_runs: list[dict[str, Any]] = []

    for source_id, source_items in sorted(grouped.items()):
        candidates: list[Candidate] = []
        item_by_index: dict[int, ScheduledItem] = {}
        originals: dict[int, dict[str, Any]] = {}
        for item in source_items:
            candidate = _load_candidate(item.notes_path, Candidate)
            if candidate.index in item_by_index:
                raise RuntimeError(f"Duplicate candidate index {candidate.index} in {source_id}")
            item_by_index[candidate.index] = item
            originals[candidate.index] = {
                "hook": candidate.one_liner,
                "score": candidate.score,
                "hook_score": candidate.hook_score,
                "value_score": candidate.value_score,
                "reason": candidate.reason,
                "slug": candidate.slug,
                "start": candidate.start,
                "end": candidate.end,
            }
            candidates.append(candidate)

        judgments: dict[int, dict[str, Any]] = {}
        source_responses: list[dict[str, Any]] = []
        # These reels already passed the original content discriminator.  Spend the
        # single v2 semantic-repair pass on their hooks only, then fail closed on the
        # exact same deterministic contract used by the current workflow.
        if reused_payload is not None:
            source_responses = reused_runs.get(source_id, [])
            if not source_responses:
                raise RuntimeError(f"No reusable semantic repair responses for {source_id}")
        else:
            assert client is not None
            for offset in range(0, len(candidates), 10):
                batch = candidates[offset : offset + 10]
                repair_targets = []
                for candidate in batch:
                    baseline = {
                        "keep": True,
                        "hook_action": "select",
                        "best_hook_id": 0,
                        "replacement_hook": "",
                        "hook_checks": {},
                    }
                    repair_targets.append(
                        (
                            candidate,
                            baseline,
                            _impeachment_hook_failures(candidate, baseline),
                        )
                    )
                response = client._repair_impeachment_hook_judgments(
                    _load_video_title(args.reel_root, source_id),
                    repair_targets,
                    min_score=args.min_score,
                    profile=profile,
                )
                source_responses.append(response)

        for response in source_responses:
            judgments.update(
                _judgments_by_index(
                    response,
                    allowed_indices={candidate.index for candidate in candidates},
                )
            )

        for candidate in candidates:
            item = item_by_index[candidate.index]
            original = originals[candidate.index]
            override_key = (source_id, candidate.index)
            editorial_override = editorial_overrides.get(override_key)
            if editorial_override is not None:
                used_editorial_overrides.add(override_key)
                judgment = {
                    "index": candidate.index,
                    "keep": True,
                    "hook_action": "rewrite",
                    "best_hook_id": None,
                    "replacement_hook": str(editorial_override["new_hook"]),
                    "hook_checks": {
                        "plain_language": True,
                        "challenge_or_contradiction": True,
                        "concrete_answer_anchor": True,
                        "open_loop": True,
                        "payoff_supported": True,
                        "jargon_first": False,
                        "answer_first": False,
                        "support_quote": str(editorial_override["support_quote"]),
                    },
                    "hook_score": editorial_override.get(
                        "hook_score", original["hook_score"] or original["score"]
                    ),
                    "value_score": editorial_override.get(
                        "value_score", original["value_score"] or original["score"]
                    ),
                    "score": editorial_override.get("score", original["score"]),
                    "reason": str(
                        editorial_override.get("reason")
                        or "Editorially tightened to the mandatory challenge plus answer-anchor formula."
                    ),
                    "payoff": str(editorial_override.get("payoff") or ""),
                }
            else:
                judgment = judgments.get(candidate.index, {})
            failures = (
                _impeachment_hook_failures(candidate, judgment)
                if judgment and bool(judgment.get("keep"))
                else ["missing_or_rejected_judgment"]
            )
            identity_before = (
                candidate.index,
                candidate.start,
                candidate.end,
                candidate.slug,
            )
            if not failures:
                _apply_ai_judgment(candidate, judgment, reel_type=profile)
            identity_after = (
                candidate.index,
                candidate.start,
                candidate.end,
                candidate.slug,
            )
            if identity_after != identity_before:
                raise RuntimeError(
                    f"Hook repair changed immutable clip identity for {item.content_hash}"
                )
            passed = (
                not failures
                and candidate.hook_assessment.get("formula_pass") is True
                and candidate.score >= args.min_score
            )
            new_hook = candidate.one_liner if passed else None
            if passed and (not new_hook or _word_count(new_hook) > 14 or "\n" in new_hook):
                raise RuntimeError(
                    f"Final hook failed local length/line validation for {item.content_hash}: {new_hook!r}"
                )
            review_by_hash[item.content_hash] = {
                "status": "approved" if passed else "rejected",
                "content_hash": item.content_hash,
                "channel_id": item.channel_id,
                "scheduled_at": item.scheduled_at,
                "source_id": source_id,
                "candidate_index": candidate.index,
                "start": original["start"],
                "end": original["end"],
                "slug": original["slug"],
                "media_path": str(item.media_path),
                "manifest_path": item.manifest_path,
                "current_title": item.title,
                "old_hook": original["hook"],
                "new_hook": new_hook,
                "hook_score": candidate.hook_score,
                "value_score": candidate.value_score,
                "score": candidate.score,
                "hook_assessment": candidate.hook_assessment,
                "validation_failures": failures,
                "decision_source": (
                    "editorial_override" if editorial_override is not None else "semantic_repair"
                ),
                "reason": judgment.get("reason") or candidate.reason,
                "payoff": judgment.get("payoff") or "",
            }

        raw_runs.append(
            {
                "source_id": source_id,
                "video_title": _load_video_title(args.reel_root, source_id),
                "semantic_repair_responses": source_responses,
            }
        )

    unused_overrides = set(editorial_overrides) - used_editorial_overrides
    if unused_overrides:
        raise RuntimeError(f"Editorial overrides did not match scheduled reels: {sorted(unused_overrides)}")

    ordered_reviews = [review_by_hash[item.content_hash] for item in items]
    surface_entries: list[dict[str, Any]] = []
    for global_index, review in enumerate(ordered_reviews, start=1):
        review["surface_validation_index"] = global_index
        surface_entries.append(
            {
                "index": global_index,
                "hook": review.get("new_hook") or "",
                "score": review.get("score"),
                "hook_score": review.get("hook_score"),
                "semantic_valid": (
                    review["status"] == "approved"
                    and review.get("hook_assessment", {}).get("formula_pass") is True
                ),
            }
        )
    surface_failures = _impeachment_batch_surface_failures(surface_entries)
    for review in ordered_reviews:
        failures = surface_failures.get(review["surface_validation_index"], [])
        if failures:
            review["status"] = "rejected"
            review["validation_failures"] = [
                *review.get("validation_failures", []),
                *failures,
            ]
        review.setdefault("hook_assessment", {})["surface_diversity_pass"] = (
            review["status"] == "approved" and not failures
        )
    approved_count = sum(item["status"] == "approved" for item in ordered_reviews)
    payload = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "scheduled-unpublished-hook-review-only",
        "selection_profile": profile.token,
        "selection_profile_version": profile.selection_profile_version,
        "model": model_name,
        "min_score": args.min_score,
        "queue_database": str(args.db.resolve()),
        "reel_root": str(args.reel_root.resolve()),
        "expected_count": args.expected_count,
        "approved_count": approved_count,
        "rejected_count": len(ordered_reviews) - approved_count,
        "rendered": False,
        "queue_modified": False,
        "revalidated_from": (
            str(args.reuse_responses_from.resolve()) if args.reuse_responses_from else None
        ),
        "editorial_overrides": (
            str(args.editorial_overrides.resolve()) if args.editorial_overrides else None
        ),
        "editorial_override_count": len(used_editorial_overrides),
        "items": ordered_reviews,
        "raw_runs": raw_runs,
    }
    _atomic_json_write(args.output, payload)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "approved": approved_count,
                "rejected": len(ordered_reviews) - approved_count,
                "rendered": False,
                "queue_modified": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
