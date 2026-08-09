#!/usr/bin/env python3
"""Rebuild the measured winner hook/script library from a Moneyball JSON."""

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
import verified_winner_library as winner_library  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect the union of all Instagram Moneyball metric and aggregate "
            "Top 10 posts with their published hooks and grounded scripts."
        )
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.json",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.winner_library.md",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=ROOT / "out" / "reel_report.moneyball.winner_library.json",
    )
    return parser


def _read_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"Moneyball JSON not found: {path}")
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid Moneyball JSON: {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise SystemExit(f"Moneyball JSON root must be an object: {path}")
    return report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = _read_report(args.report_json)
    library = winner_library.build_winner_library(
        report,
        source_report_path=args.report_json,
    )
    moneyball.atomic_write_text(
        args.markdown_out,
        winner_library.render_winner_library_markdown(library),
    )
    moneyball.atomic_write_text(
        args.json_out,
        winner_library.render_winner_library_json(library),
    )
    metadata = library["library_metadata"]
    coverage = library["data_coverage"]
    print(
        "[winner-library] "
        f"posts={metadata['unique_winner_posts']} "
        f"placements={metadata['ranking_placement_count']} "
        f"scripts={coverage['japanese_scripts']['count']}/"
        f"{coverage['japanese_scripts']['total']}"
    )
    print(f"[winner-library] wrote {args.markdown_out}")
    print(f"[winner-library] wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
