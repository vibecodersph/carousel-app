#!/usr/bin/env python3
"""CLI for the Python carousel idea engine."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .curation import run_idea_engine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate carousel-ready curation JSON, one carousel per idea.",
    )
    parser.add_argument("--lens", required=True, choices=["jp_business", "ph_builder"])
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--llm-provider", default="local", choices=["local", "gemini"])
    parser.add_argument("--from-question", help="Generate ideas from a real audience question")
    parser.add_argument(
        "--from-stories",
        type=Path,
        help="Convert legacy {stories:[...]} idea JSON into one carousel per story",
    )
    parser.add_argument("--set-category", help="Optional SET category constraint")
    parser.add_argument("--axis", help="Optional AXIS constraint")
    parser.add_argument("--twist", help="Optional TWIST constraint")
    parser.add_argument("--candidate-pool", type=int, help="Combinations to try before ranking")
    parser.add_argument("--candidates-per-combination", type=int, default=3)
    parser.add_argument(
        "--carousel-out",
        "--out",
        dest="out",
        type=Path,
        help="Output path for carousel-ready JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_idea_engine(
        lens=args.lens,
        count=max(1, args.count),
        provider=args.llm_provider,
        out_path=args.out,
        from_stories=args.from_stories,
        from_question=args.from_question,
        candidate_pool=args.candidate_pool,
        candidates_per_combination=args.candidates_per_combination,
        set_category=args.set_category,
        axis=args.axis,
        twist=args.twist,
    )
    summary = {
        "lens": result["lens"],
        "channel_id": result["channel_id"],
        "provider": result["provider"],
        "carousel_count": result["carousel_count"],
        "killed_count": result["killed_count"],
        "carousel_json_path": result["carousel_json_path"],
        "validation_errors": result["validation_errors"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
