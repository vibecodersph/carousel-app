#!/usr/bin/env python3
"""Merge isolated fail-closed retries into an LLM candidate report."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_reel_candidate_evaluator as evaluator  # noqa: E402
import moneyball_analytics as moneyball  # noqa: E402


def _read(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected object report: {path}")
    return payload


def _preserve_in_place_base(base: Path, json_out: Path) -> Path:
    if base.resolve() != json_out.resolve():
        return base
    raw = base.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:12]
    preserved = base.with_name(f"{base.stem}.premerge.{digest}{base.suffix}")
    if not preserved.exists():
        moneyball.atomic_write_text(preserved, raw.decode("utf-8"))
    return preserved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--patch", type=Path, action="append", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()

    provenance_base = _preserve_in_place_base(args.base, args.json_out)
    report = evaluator.merge_llm_candidate_evaluation_reports(
        _read(args.base),
        [_read(path) for path in args.patch],
    )
    report["report_metadata"]["merge_inputs"] = {
        "base": str(provenance_base.resolve()),
        "patches": [str(path.resolve()) for path in args.patch],
    }
    moneyball.atomic_write_text(
        args.json_out,
        evaluator.render_llm_candidate_evaluation_json(report),
    )
    moneyball.atomic_write_text(
        args.markdown_out,
        evaluator.render_llm_candidate_evaluation_markdown(report),
    )
    print(
        "[candidate-evaluator] "
        f"merged_retry_rows={report['report_metadata']['merged_retry_rows']} "
        f"api_errors={report['summary']['api_errors']}"
    )
    print(f"[candidate-evaluator] wrote {args.markdown_out}")
    print(f"[candidate-evaluator] wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
