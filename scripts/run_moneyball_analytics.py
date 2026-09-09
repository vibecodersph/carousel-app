#!/usr/bin/env python3
"""Generate additive Moneyball reports from the existing Reel ledger."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import moneyball_analytics as moneyball  # noqa: E402
from moneyball_record_content import build_record_holder_content  # noqa: E402
import moneyball_record_history as record_history  # noqa: E402
import moneyball_records as account_records  # noqa: E402
import moneyball_records_render as records_render  # noqa: E402
import manual_follow_conversion as manual_follows  # noqa: E402
import verified_winner_library as winner_library  # noqa: E402


def aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def link_manual_follow_conversion(report: dict[str, Any], manual: dict[str, Any]) -> None:
    """Attach supplied manual context by stable Reel ID without changing API data.

    A custom ledger or historical export may omit a previously resolved Reel;
    retain such source observations and explicitly report the missing link.
    A conflicting account, platform or permalink is an error, never a fuzzy match.
    """
    account = report.get("report_metadata", {}).get("account")
    if manual.get("account") != account or manual.get("platform") != "instagram":
        raise ValueError("Manual follow linkage account/platform mismatch")
    posts = {}
    for post in report.get("posts", []):
        identity = post.get("identity", {})
        media_id = str(identity.get("media_id") or "")
        if media_id:
            if media_id in posts:
                raise ValueError(f"Duplicate canonical media_id for manual follow linkage: {media_id}")
            posts[media_id] = post

    def permalink_key(value: Any) -> tuple[str, str] | None:
        if not isinstance(value, str) or not value:
            return None
        parsed = urlsplit(value)
        return parsed.netloc.removeprefix("www."), parsed.path.rstrip("/")

    linked: dict[str, dict[str, Any]] = {}
    unmatched = []
    linked_observations = 0
    linked_latest_rows = 0
    for collection in ("observations", "rows"):
        for row in manual.get(collection, []):
            media_id = str(row.get("media_id") or "")
            reason = None
            if row.get("identity_status") not in {"MATCHED", "VERIFIED"} or not media_id:
                reason = "IDENTITY_UNRESOLVED"
            elif media_id not in posts:
                reason = "MEDIA_ID_NOT_IN_REPORT"
            else:
                identity = posts[media_id]["identity"]
                if identity.get("account", account) != account or identity.get("platform", "instagram") != "instagram":
                    raise ValueError(f"Manual follow linkage account/platform mismatch for media_id {media_id}")
                if row.get("account", account) != account or row.get("platform", "instagram") != "instagram":
                    raise ValueError(f"Manual follow source account/platform mismatch for media_id {media_id}")
                if not identity.get("permalink"):
                    reason = "CANONICAL_PERMALINK_UNAVAILABLE"
                elif permalink_key(row.get("permalink")) != permalink_key(identity.get("permalink")):
                    raise ValueError(f"Manual follow linkage permalink mismatch for media_id {media_id}")
            if reason:
                if collection == "observations":
                    unmatched.append({"creative_id": row.get("creative_id"), "media_id": media_id or None,
                                      "reason": reason})
                continue
            context = linked.setdefault(media_id, {
                "status": "LINKED", "join_key": "identity.media_id", "account": account,
                "platform": "instagram", "observations": [], "latest_rows": [],
                "scope": "User-supplied manual observations; separate from API raw/derived metrics and automatic rankings.",
            })
            context["observations" if collection == "observations" else "latest_rows"].append(copy.deepcopy(row))
            if collection == "observations":
                linked_observations += 1
            else:
                linked_latest_rows += 1
    # Validate the complete join before adding context to any post.
    for media_id, context in linked.items():
        posts[media_id]["manual_follow_conversion"] = context
    manual["linkage"] = {
        "join_key": "identity.media_id", "linked_post_count": len(linked),
        "linked_observation_count": linked_observations, "linked_latest_row_count": linked_latest_rows,
        "unmatched_observation_count": len(unmatched), "unmatched_observations": unmatched,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build read-only, age-matched Moneyball analytics without changing "
            "the existing Reel reports"
        )
    )
    parser.add_argument("--channel", default="aibrief_jp")
    parser.add_argument("--db", type=Path, default=ROOT / "state" / "reels.db")
    parser.add_argument(
        "--facebook-db",
        type=Path,
        default=None,
        help=(
            "Independent Facebook Reel ledger. Defaults to state/facebook.db "
            "for the standard state/reels.db run, or a sibling facebook.db for "
            "custom ledgers."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "moneyball_analytics.json",
    )
    parser.add_argument(
        "--annotations",
        type=Path,
        default=ROOT / "data" / "reel_annotations.json",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.md",
    )
    parser.add_argument(
        "--html-out",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.html",
        help="Self-contained visual dashboard output",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.json",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.csv",
    )
    parser.add_argument(
        "--facebook-csv-out",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.facebook.csv",
        help="Flat export for the separate Facebook analytics lane",
    )
    parser.add_argument(
        "--audit-out",
        type=Path,
        default=ROOT / "out" / "moneyball_data_audit.md",
    )
    parser.add_argument(
        "--winner-library-markdown-out",
        type=Path,
        default=None,
        help=(
            "Deduplicated fixed-window Top-10 hook/script library. Defaults to "
            "<json-out stem>.winner_library.md."
        ),
    )
    parser.add_argument(
        "--winner-library-json-out",
        type=Path,
        default=None,
        help=(
            "Machine-readable winner library. Defaults to "
            "<json-out stem>.winner_library.json."
        ),
    )
    parser.add_argument(
        "--manual-follow-data",
        type=Path,
        default=None,
        help="Appendable manual follows/viewers observations; defaults to <annotations directory>/<channel>_manual_follow_conversion.json.",
    )
    parser.add_argument(
        "--record-history",
        type=Path,
        default=None,
        help="Persistent record/former-winner history: state/moneyball_records/<channel>.history.json for canonical output, or beside a custom --json-out.",
    )
    parser.add_argument(
        "--as-of",
        type=aware_datetime,
        default=None,
        help="Ignore snapshots captured after this timestamp (ISO-8601 with offset)",
    )
    parser.add_argument(
        "--generated-at",
        type=aware_datetime,
        default=None,
        help="Freeze the report timestamp for deterministic validation",
    )
    return parser


def _run(args: argparse.Namespace, history_path: Path) -> int:
    if not args.db.is_file():
        raise SystemExit(f"Moneyball ledger not found: {args.db}")
    facebook_db = (
        args.facebook_db
        if args.facebook_db is not None
        else (
            ROOT / "state" / "facebook.db"
            if args.db.expanduser().resolve()
            == (ROOT / "state" / "reels.db").resolve()
            else args.db.expanduser().resolve().parent / "facebook.db"
        )
    )
    report = moneyball.build_moneyball_report(
        db_path=args.db,
        channel=args.channel,
        config_path=args.config,
        annotations_path=args.annotations,
        generated_at=args.generated_at,
        as_of=args.as_of,
        facebook_db_path=facebook_db,
    )
    records_markdown_out = args.json_out.with_name(f"{args.json_out.stem}.records.md")
    records_json_out = args.json_out.with_name(f"{args.json_out.stem}.records.json")
    previous_history = record_history.load_record_history(history_path, account=args.channel)
    records = account_records.build_account_records(report)
    policy = records["ranking_policy"]
    baseline, fingerprint = record_history.comparison_history(
        report, previous_history, ranking_policy=policy,
    )
    if baseline is not None:
        records = account_records.build_account_records(report, previous_history=baseline)
    if previous_history and policy["signature"] != (previous_history.get("ranking_policy") or {}).get("signature"):
        records["status"] = "METHODOLOGY_BASELINE_ESTABLISHED"
    elif previous_history and records["as_of"] == previous_history["last_recorded_at"]:
        records["status"] = previous_history.get("last_report_status", records["status"])
    for event in records["events"]:
        event["ranking_policy_signature"] = policy["signature"]
    report["account_records"] = records
    manual_path = args.manual_follow_data or args.annotations.parent / f"{args.channel}_manual_follow_conversion.json"
    manual = manual_follows.build_manual_follow_conversion(manual_path, account=args.channel)
    link_manual_follow_conversion(report, manual)
    report["manual_follow_conversion"] = manual
    manual_markdown_out = args.json_out.with_name(f"{args.json_out.stem}.manual_follows.md")
    manual_json_out = args.json_out.with_name(f"{args.json_out.stem}.manual_follows.json")
    winner_markdown_out = args.winner_library_markdown_out or args.json_out.with_name(
        f"{args.json_out.stem}.winner_library.md"
    )
    winner_json_out = args.winner_library_json_out or args.json_out.with_name(
        f"{args.json_out.stem}.winner_library.json"
    )
    library = winner_library.build_winner_library(
        report,
        source_report_path=args.json_out,
    )
    history = record_history.build_record_history(
        records, library, previous_history=previous_history,
        baseline=baseline, input_fingerprint=fingerprint,
        holder_content=build_record_holder_content(report, records, library),
    )
    records_json = json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    history_json = json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    records_markdown = records_render.render_account_records_markdown(records)
    manual_json = json.dumps(manual, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    manual_markdown = manual_follows.render_manual_follow_conversion_markdown(manual)
    # Compute/validate the new state before replacing outputs. Commit history last
    # so a failed render cannot consume a record notification.
    moneyball.write_moneyball_outputs(
        report,
        markdown_path=args.markdown_out,
        json_path=args.json_out,
        csv_path=args.csv_out,
        audit_path=args.audit_out,
        html_path=args.html_out,
        facebook_csv_path=args.facebook_csv_out,
    )
    moneyball.atomic_write_text(
        winner_markdown_out,
        winner_library.render_winner_library_markdown(library),
    )
    moneyball.atomic_write_text(
        winner_json_out,
        winner_library.render_winner_library_json(library),
    )
    moneyball.atomic_write_text(records_markdown_out, records_markdown)
    moneyball.atomic_write_text(records_json_out, records_json)
    moneyball.atomic_write_text(manual_markdown_out, manual_markdown)
    moneyball.atomic_write_text(manual_json_out, manual_json)
    moneyball.atomic_write_text(history_path, history_json)
    facebook = report.get("platform_analytics", {}).get("facebook", {})
    facebook_coverage = facebook.get("data_coverage", {})
    print(
        "[moneyball] "
        f"facebook_status={facebook.get('status', 'UNAVAILABLE')} "
        f"reels={facebook_coverage.get('published_posts', 0)} "
        f"latest={facebook_coverage.get('latest_snapshot_posts', 0)}"
    )
    coverage = report["data_coverage"]
    print(
        "[moneyball] "
        f"account={args.channel} reels={coverage['published_posts']} "
        f"latest={coverage['latest_snapshot_posts']} "
        f"fixed_windows="
        + ",".join(
            f"{window}:{coverage['snapshot_maturity'][window]['count']}"
            for window in moneyball.WINDOW_ORDER
        )
    )
    print(f"[moneyball] wrote {args.markdown_out}")
    print(f"[moneyball] wrote {args.html_out}")
    print(f"[moneyball] wrote {args.json_out}")
    print(f"[moneyball] wrote {args.csv_out}")
    if facebook.get("status") in {"AVAILABLE", "NO_PUBLISHED_POSTS"}:
        print(f"[moneyball] wrote {args.facebook_csv_out}")
    print(f"[moneyball] wrote {args.audit_out}")
    print(f"[moneyball] wrote {winner_markdown_out}")
    print(f"[moneyball] wrote {winner_json_out}")
    print(f"[moneyball] wrote {records_markdown_out}")
    print(f"[moneyball] wrote {records_json_out}")
    print(f"[moneyball] wrote {manual_markdown_out}")
    print(f"[moneyball] wrote {manual_json_out}")
    print(f"[moneyball] preserved record and winner history in {history_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    history_path = args.record_history or args.json_out.with_name(
        f"{args.json_out.stem}.records.history.json"
    )
    if args.record_history is None and args.json_out.expanduser().resolve() == ROOT / "out" / "reel_report.moneyball.json":
        history_path = ROOT / "state" / "moneyball_records" / f"{args.channel}.history.json"
    with record_history.record_history_lock(history_path):
        return _run(args, history_path)


if __name__ == "__main__":
    raise SystemExit(main())
