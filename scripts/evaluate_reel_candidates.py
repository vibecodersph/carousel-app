#!/usr/bin/env python3
"""Evaluate reel-app candidates against measured Moneyball winners."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import moneyball_analytics as moneyball  # noqa: E402
import reel_candidate_evaluator as evaluator  # noqa: E402
import llm_reel_candidate_evaluator as llm_evaluator  # noqa: E402
import scheduled_reel_evaluator as scheduled_evaluator  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Rejudge Reel source candidates against the current measured "
            "24-hour winner hook/script library."
        )
    )
    parser.add_argument(
        "--analysis-mode",
        choices=("auto", "llm", "diagnostic"),
        default="auto",
        help=(
            "auto uses two-pass LLM semantic analysis for candidates and the "
            "legacy diagnostic rules for scheduled-ledger mode. diagnostic is "
            "the explicitly named non-LLM fallback."
        ),
    )
    parser.add_argument(
        "--model",
        default="gemini-3.6-flash",
        help="Gemini model used by --analysis-mode llm.",
    )
    parser.add_argument(
        "--approve-gemini-data-transfer",
        action="store_true",
        help=(
            "Required acknowledgement that LLM mode sends unpublished candidate "
            "hooks/transcripts and internal winner analytics to the configured "
            "Gemini API account."
        ),
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("minimal", "low", "medium", "high"),
        default="medium",
        help="Reasoning effort used by the LLM evaluator.",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=240.0,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Concurrent candidate reviews in LLM mode.",
    )
    parser.add_argument(
        "--llm-cache-dir",
        type=Path,
        default=ROOT / "state" / "llm_reel_candidate_evaluator_cache",
        help=(
            "Content-addressed structured-response cache. It makes identical "
            "model/prompt/library/candidate reruns reproducible."
        ),
    )
    parser.add_argument(
        "--no-llm-cache",
        action="store_true",
        help="Force fresh LLM responses for every request.",
    )
    parser.add_argument(
        "--no-false-negative-audit",
        action="store_true",
        help=(
            "Skip independent LLM screening of discriminator rejections when a "
            "source has no reconciled candidates."
        ),
    )
    parser.add_argument(
        "--max-false-negative-deep-reviews",
        type=int,
        default=5,
        help=(
            "Maximum LLM-screened rejected rows that receive the full two-pass "
            "six-category comparison. Every rejection is still screened."
        ),
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        action="append",
        default=[],
        help=(
            "Path to candidates.json or its source folder. Repeat for multiple "
            "sources."
        ),
    )
    parser.add_argument(
        "--candidate-slug",
        action="append",
        default=[],
        help=(
            "Evaluate only the exact candidate slug. Repeat for multiple "
            "candidates. Available in LLM candidates.json mode and recorded "
            "in the report selection audit."
        ),
    )
    parser.add_argument(
        "--latest-from",
        type=Path,
        default=None,
        help=(
            "Select newest candidates.json files recursively from this outputs "
            "folder. Requires --latest-count."
        ),
    )
    parser.add_argument(
        "--latest-count",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--scheduled-db",
        type=Path,
        default=None,
        help=(
            "Read only the exact active Reel rows from this ledger and evaluate "
            "their scheduled clip artifacts. Mutually exclusive with "
            "--candidates/--latest-from."
        ),
    )
    parser.add_argument(
        "--scheduled-status",
        action="append",
        default=[],
        help=(
            "Ledger status included by --scheduled-db. Repeat as needed; "
            "defaults to scheduled."
        ),
    )
    parser.add_argument(
        "--exclude-source-video",
        action="append",
        default=[],
        help=(
            "Exclude an exact source video ID from --scheduled-db. Repeat for "
            "multiple previously reviewed source folders. The exclusion is "
            "recorded in the report."
        ),
    )
    parser.add_argument(
        "--scheduled-content-hash",
        action="append",
        default=[],
        help=(
            "Evaluate only an exact scheduled content hash. Repeat for "
            "multiple rows. Available only with --scheduled-db and recorded "
            "in the report; useful for fail-closed LLM retries."
        ),
    )
    parser.add_argument(
        "--channel",
        default="aibrief_jp",
        help="Ledger channel used by --scheduled-db.",
    )
    parser.add_argument(
        "--facebook-db",
        type=Path,
        default=ROOT / "state" / "facebook.db",
        help=(
            "Read-only Facebook ledger used by the capacity-aware Trial "
            "selector in scheduled mode."
        ),
    )
    parser.add_argument(
        "--insights-report",
        type=Path,
        default=ROOT / "out" / "reel_report.insights.json",
        help=(
            "Existing Instagram insight report used by the official daily "
            "Trial selector in scheduled mode."
        ),
    )
    parser.add_argument(
        "--winner-library",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.winner_library.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=evaluator.DEFAULT_CONFIG_PATH,
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "out" / "reel_candidate_evaluation.json",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=ROOT / "out" / "reel_candidate_evaluation.md",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Optional flat schedule-triage CSV (scheduled mode only).",
    )
    return parser


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"{label} root must be an object: {path}")
    return payload


def _candidate_file(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    return resolved / "candidates.json" if resolved.is_dir() else resolved


def _latest_candidates(root: Path, count: int) -> list[Path]:
    if count <= 0:
        raise SystemExit("--latest-count must be positive")
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise SystemExit(f"--latest-from is not a directory: {resolved}")
    candidates = sorted(
        resolved.glob("*/candidates.json"),
        key=lambda path: (-path.stat().st_mtime_ns, str(path)),
    )
    if len(candidates) < count:
        raise SystemExit(
            f"Requested {count} candidate files but found {len(candidates)} in {resolved}"
        )
    return candidates[:count]


def resolve_candidate_paths(
    args: argparse.Namespace,
    *,
    require_input: bool = True,
) -> list[Path]:
    paths = [_candidate_file(path) for path in args.candidates]
    if args.latest_from is not None:
        if args.latest_count is None:
            raise SystemExit("--latest-from requires --latest-count")
        paths.extend(_latest_candidates(args.latest_from, args.latest_count))
    elif args.latest_count is not None:
        raise SystemExit("--latest-count requires --latest-from")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        if not path.is_file():
            raise SystemExit(f"candidates.json not found: {path}")
        seen.add(path)
        unique.append(path)
    if not unique and require_input:
        raise SystemExit(
            "Provide at least one --candidates path or --latest-from/--latest-count"
        )
    return unique


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scheduled_mode = args.scheduled_db is not None
    analysis_mode = args.analysis_mode
    if analysis_mode == "auto":
        analysis_mode = "diagnostic" if scheduled_mode else "llm"
    candidate_paths = resolve_candidate_paths(
        args,
        require_input=not scheduled_mode,
    )
    if scheduled_mode and candidate_paths:
        raise SystemExit(
            "--scheduled-db is mutually exclusive with "
            "--candidates/--latest-from"
        )
    if args.csv_out is not None and not scheduled_mode:
        raise SystemExit("--csv-out is currently available only with --scheduled-db")
    if args.csv_out is not None and analysis_mode == "llm":
        raise SystemExit(
            "--csv-out is not yet available for scheduled LLM reports"
        )
    if args.candidate_slug and (scheduled_mode or analysis_mode != "llm"):
        raise SystemExit(
            "--candidate-slug is available only with LLM candidates.json mode"
        )
    if args.exclude_source_video and not scheduled_mode:
        raise SystemExit(
            "--exclude-source-video is available only with --scheduled-db"
        )
    if args.scheduled_content_hash and not scheduled_mode:
        raise SystemExit(
            "--scheduled-content-hash is available only with --scheduled-db"
        )
    if analysis_mode == "llm" and not args.approve_gemini_data_transfer:
        raise SystemExit(
            "LLM mode sends unpublished candidate hooks/transcripts and internal "
            "Top-10 winner analytics to the configured Gemini API account. "
            "Rerun with --approve-gemini-data-transfer only after that transfer "
            "has been explicitly approved."
        )
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")
    if args.max_false_negative_deep_reviews < 0:
        raise SystemExit("--max-false-negative-deep-reviews cannot be negative")
    winner_path = args.winner_library.expanduser().resolve()
    config_path = args.config.expanduser().resolve()
    winner_library = _read_json_object(winner_path, "winner library")
    try:
        config = evaluator.load_config(config_path)
        if scheduled_mode:
            statuses = args.scheduled_status or ["scheduled"]
            scheduled_db = args.scheduled_db.expanduser().resolve()
            rows = scheduled_evaluator.load_scheduled_rows(
                scheduled_db,
                channel_id=args.channel,
                statuses=statuses,
            )
            excluded_source_videos = sorted(
                {
                    str(value).strip()
                    for value in args.exclude_source_video
                    if str(value).strip()
                }
            )
            rows_before_exclusions = len(rows)
            excluded_rows = [
                row
                for row in rows
                if str(row.get("source_video") or "").strip()
                in excluded_source_videos
            ]
            rows = [
                row
                for row in rows
                if str(row.get("source_video") or "").strip()
                not in excluded_source_videos
            ]
            requested_content_hashes = sorted(
                {
                    str(value).strip()
                    for value in args.scheduled_content_hash
                    if str(value).strip()
                }
            )
            if requested_content_hashes:
                available_content_hashes = {
                    str(row.get("content_hash") or "").strip()
                    for row in rows
                }
                missing_content_hashes = sorted(
                    set(requested_content_hashes)
                    - available_content_hashes
                )
                if missing_content_hashes:
                    raise ValueError(
                        "Requested scheduled content hash(es) not found: "
                        + ", ".join(missing_content_hashes)
                    )
                rows_before_hash_filter = len(rows)
                rows = [
                    row
                    for row in rows
                    if str(row.get("content_hash") or "").strip()
                    in requested_content_hashes
                ]
            else:
                rows_before_hash_filter = len(rows)
            sources, input_audit = (
                scheduled_evaluator.normalize_scheduled_sources(
                    rows,
                    config,
                    db_path=scheduled_db,
                )
            )
            input_audit["source_exclusions"] = {
                "requested_source_video_ids": excluded_source_videos,
                "rows_before_exclusions": rows_before_exclusions,
                "rows_excluded": len(excluded_rows),
                "rows_after_exclusions": len(rows),
                "matched_source_video_ids": sorted(
                    {
                        str(row.get("source_video") or "").strip()
                        for row in excluded_rows
                        if str(row.get("source_video") or "").strip()
                    }
                ),
            }
            input_audit["content_hash_selection"] = {
                "requested_content_hashes": requested_content_hashes,
                "rows_before_filter": rows_before_hash_filter,
                "rows_selected": len(rows),
            }
            if analysis_mode == "llm":
                llm_sources = (
                    scheduled_evaluator.prepare_scheduled_sources_for_llm(
                        sources
                    )
                )
                report = llm_evaluator.build_llm_candidate_evaluation(
                    [],
                    winner_library,
                    config,
                    winner_library_path=winner_path,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    timeout_seconds=args.llm_timeout_seconds,
                    workers=args.workers,
                    cache_dir=args.llm_cache_dir.expanduser().resolve(),
                    use_cache=not args.no_llm_cache,
                    audit_false_negatives=False,
                    max_false_negative_deep_reviews=0,
                    normalized_sources=llm_sources,
                    candidate_origin="SCHEDULED_CANDIDATE",
                    input_scope="scheduled_pipeline_exact_clips",
                )
                report["report_metadata"].update(
                    {
                        "scheduled_db": str(scheduled_db),
                        "scheduled_statuses": sorted(set(statuses)),
                        "excluded_source_videos": excluded_source_videos,
                        "scheduled_content_hash_filter": (
                            requested_content_hashes
                        ),
                    }
                )
                report["scheduled_scope"] = {
                    "input_audit": input_audit,
                    "scheduled_rows": len(rows),
                    "source_folders": len(sources),
                    "trial_reels": sum(
                        bool(row.get("trial_reel")) for row in rows
                    ),
                    "regular_reels": sum(
                        not bool(row.get("trial_reel")) for row in rows
                    ),
                    "first_scheduled_at": (
                        min(
                            str(row.get("scheduled_at") or "")
                            for row in rows
                        )
                        if rows
                        else None
                    ),
                    "last_scheduled_at": (
                        max(
                            str(row.get("scheduled_at") or "")
                            for row in rows
                        )
                        if rows
                        else None
                    ),
                    "automatic_schedule_changes": 0,
                }
            else:
                report = evaluator.build_candidate_evaluation_from_sources(
                    sources,
                    winner_library,
                    config,
                    winner_library_path=winner_path,
                )
                from scripts import select_aibrief_jp_trial_candidates as trial_selector

                generated_at = str(
                    winner_library.get("library_metadata", {}).get(
                        "generated_at"
                    )
                    or ""
                )
                as_of = (
                    trial_selector.parse_aware_datetime(
                        generated_at,
                        field="winner_library.generated_at",
                    )
                    if generated_at
                    else trial_selector.default_as_of()
                )
                full_trial_selection = trial_selector.build_selection(
                    db_path=scheduled_db,
                    report_path=args.insights_report,
                    facebook_db=args.facebook_db,
                    channel_id=args.channel,
                    as_of=as_of,
                )
                recommendation = full_trial_selection.get(
                    "recommendation",
                    {},
                )
                conversion = recommendation.get("lanes", {}).get(
                    trial_selector.LANE_SCHEDULED_CONVERSION,
                    {},
                )
                official_trial_selection = {
                    "policy_version": full_trial_selection.get(
                        "policy_version"
                    ),
                    "as_of": full_trial_selection.get("as_of"),
                    "status": conversion.get("status"),
                    "target_date": recommendation.get("target_date"),
                    "experiment_id": conversion.get("experiment_id"),
                    "content_hash": conversion.get("content_hash"),
                    "expected_scheduled_at": conversion.get(
                        "expected_scheduled_at"
                    ),
                    "selected": conversion.get("scheduled_target"),
                    "dry_run_argv": conversion.get("dry_run_argv"),
                    "facebook_effect": conversion.get("facebook_effect"),
                    "manual_approval_required": recommendation.get(
                        "manual_approval_required"
                    ),
                    "auto_apply": recommendation.get("auto_apply"),
                }
                report = scheduled_evaluator.apply_schedule_triage(
                    report,
                    input_audit=input_audit,
                    official_trial_selection=official_trial_selection,
                )
        else:
            if analysis_mode == "llm":
                report = llm_evaluator.build_llm_candidate_evaluation(
                    candidate_paths,
                    winner_library,
                    config,
                    winner_library_path=winner_path,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    timeout_seconds=args.llm_timeout_seconds,
                    workers=args.workers,
                    cache_dir=args.llm_cache_dir.expanduser().resolve(),
                    use_cache=not args.no_llm_cache,
                    audit_false_negatives=not args.no_false_negative_audit,
                    max_false_negative_deep_reviews=(
                        args.max_false_negative_deep_reviews
                    ),
                    candidate_slugs=args.candidate_slug,
                )
            else:
                report = evaluator.build_candidate_evaluation(
                    candidate_paths,
                    winner_library,
                    config,
                    winner_library_path=winner_path,
                )
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    json_text = (
        llm_evaluator.render_llm_candidate_evaluation_json(report)
        if analysis_mode == "llm"
        else evaluator.render_candidate_evaluation_json(report)
    )
    moneyball.atomic_write_text(args.json_out, json_text)
    markdown = (
        llm_evaluator.render_llm_candidate_evaluation_markdown(report)
        if analysis_mode == "llm"
        else (
            scheduled_evaluator.render_scheduled_markdown(report)
            if scheduled_mode
            else evaluator.render_candidate_evaluation_markdown(report)
        )
    )
    moneyball.atomic_write_text(args.markdown_out, markdown)
    if args.csv_out is not None:
        moneyball.atomic_write_text(
            args.csv_out,
            scheduled_evaluator.render_scheduled_csv(report),
        )
    summary = report["summary"]
    if scheduled_mode and analysis_mode != "llm":
        print(
            "[candidate-evaluator] "
            f"scope=scheduled sources={summary['candidate_sources']} "
            f"candidates={summary['candidates']} "
            f"actions={summary['schedule_action_counts']}"
        )
    else:
        print(
            "[candidate-evaluator] "
            f"analysis_mode={analysis_mode} "
            f"scope={'scheduled' if scheduled_mode else 'candidate_files'} "
            f"sources={summary['candidate_sources']} "
            f"candidates={summary['candidates']} "
            f"empty_sources={summary['empty_sources']}"
        )
    print(f"[candidate-evaluator] wrote {args.markdown_out}")
    print(f"[candidate-evaluator] wrote {args.json_out}")
    if args.csv_out is not None:
        print(f"[candidate-evaluator] wrote {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
