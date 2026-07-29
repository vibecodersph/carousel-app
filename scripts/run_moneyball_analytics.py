#!/usr/bin/env python3
"""Generate additive Moneyball reports from the existing Reel ledger."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import moneyball_analytics as moneyball  # noqa: E402


def aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a UTC offset")
    return parsed.astimezone(timezone.utc)


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
    moneyball.write_moneyball_outputs(
        report,
        markdown_path=args.markdown_out,
        json_path=args.json_out,
        csv_path=args.csv_out,
        audit_path=args.audit_out,
        html_path=args.html_out,
        facebook_csv_path=args.facebook_csv_out,
    )
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
