#!/usr/bin/env python3
"""Refresh historical feedback or record a prospective source shortlist locally."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import moneyball_analytics as moneyball
import source_feedback as feedback
from moneyball_record_history import record_history_lock


def write_feedback(report: dict, batches_dir: Path, json_out: Path, *, result: dict | None = None) -> dict:
    result = result if result is not None else feedback.build_feedback(report, feedback.read_batches(batches_dir, report["report_metadata"]["account"]))
    if json_out.exists():
        previous = json.loads(json_out.read_text())
        if previous.get("account") != result["account"]:
            raise ValueError("Existing source feedback belongs to another account")
        if feedback.timestamp(previous["as_of"]) > feedback.timestamp(result["as_of"]):
            raise ValueError("Source feedback cannot move backwards in time; use a separate output")
    raw = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    markdown = feedback.render_markdown(result)
    # Immutable, content-addressed evidence preserves every policy input used by
    # later recommendations. The stable files are replaceable views.
    archive = batches_dir / "evidence" / f"{result['feedback_id']}.json"
    if archive.exists() and json.loads(archive.read_text()) != result:
        raise ValueError("Source feedback archive collision")
    if not archive.exists():
        moneyball.atomic_write_text(archive, raw)
    moneyball.atomic_write_text(json_out, raw)
    moneyball.atomic_write_text(json_out.with_suffix(".md"), markdown)
    return result


def render_batch(batch: dict) -> str:
    lines = ["# Source recommendations", "", f"Batch `{batch['batch_id']}` — {batch['status']}",
             f"Recorded {batch['recommended_at']}; evidence as of {batch['feedback_as_of']}.",
             f"Selected {batch['selected_count']}/{batch['requested_count']}; exploration allowance {batch['exploration_budget']}.", "",
             "| Selected | Source | Priority | Expected metric | Why / change from prior recommendation |",
             "| --- | --- | --- | --- | --- |"]
    for entry in batch["entries"]:
        reason = entry["selection_reason"] + ": " + entry["rationale"]
        previous = entry.get("previous_recommendation")
        if previous:
            reason += f"; previous priority {previous['priority']} → {entry['feedback']['priority']}"
        lines.append(f"| {'yes' if entry['selected'] else 'no'} | [{feedback._cell(entry['title'])}]({entry['source_url']}) | {entry['feedback']['priority']} | {entry['expected_metric']} | {feedback._cell(reason)} |")
    lines += ["", "Priority is a screening decision from measured 7d evidence, not a performance prediction. A partial shortlist needs better-supported sources; it must not be filled silently with untested or reduced-priority sources.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="aibrief_jp")
    parser.add_argument("--batches-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh")
    refresh.add_argument("--report", type=Path, default=ROOT / "out/reel_report.moneyball.json")
    refresh.add_argument("--json-out", type=Path, default=ROOT / "out/reel_report.moneyball.source_feedback.json")
    select = commands.add_parser("recommend")
    select.add_argument("--feedback", type=Path, default=ROOT / "out/reel_report.moneyball.source_feedback.json")
    select.add_argument("--input", type=Path, required=True, help="JSON object with candidates array, in editorial preference order")
    select.add_argument("--batch-id", required=True)
    select.add_argument("--count", type=int, default=5)
    select.add_argument("--markdown-out", type=Path, default=ROOT / "out/source_recommendations.latest.md")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.channel):
        parser.error("Invalid channel")
    batches_dir = args.batches_dir or ROOT / "state/source_recommendations" / args.channel
    if args.command == "refresh":
        report = json.loads(args.report.read_text())
        if report["report_metadata"]["account"] != args.channel:
            parser.error("Report account mismatch")
        with record_history_lock(batches_dir / "registry"):
            result = write_feedback(report, batches_dir, args.json_out)
        print(f"Feedback {result['feedback_id']}: {result['coverage']}")
    else:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", args.batch_id):
            parser.error("Batch ID must contain only letters, digits, hyphens or underscores")
        candidates = json.loads(args.input.read_text())["candidates"]
        data = json.loads(args.feedback.read_text())
        with record_history_lock(batches_dir / "registry"):
            path = batches_dir / f"{args.batch_id}.json"
            if path.exists():
                parser.error("Batch already exists; recommendation records are immutable")
            batches = feedback.read_batches(batches_dir, args.channel)
            batch = feedback.recommend(data, candidates, account=args.channel, batch_id=args.batch_id,
                                       count=args.count, now=datetime.now(timezone.utc))
            batch["exact_binding_required"] = True
            for entry in batch["entries"]:
                previous = [(b, e) for b in batches for e in b["entries"]
                            if e["selected"] and e["source_id"] == entry["source_id"]
                            and e["distribution_mode"] == entry["distribution_mode"]]
                if previous:
                    b, e = max(previous, key=lambda pair: pair[0]["recommended_at"])
                    entry["previous_recommendation"] = {"batch_id": b["batch_id"],
                                                         "priority": e["feedback"]["priority"]}
            batch["entries_sha256"] = feedback.digest(batch["entries"])
            batch["batch_sha256"] = feedback.digest({k: v for k, v in batch.items() if k != "batch_sha256"})
            archive = batches_dir / "evidence" / f"{data['feedback_id']}.json"
            if archive.exists() and json.loads(archive.read_text()) != data:
                raise ValueError("Archived feedback differs from current input")
            if not archive.exists():
                moneyball.atomic_write_text(archive, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
            moneyball.atomic_write_text(path, json.dumps(batch, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        moneyball.atomic_write_text(args.markdown_out, render_batch(batch))
        print(f"{batch['status']}: {batch['selected_count']}/{args.count} sources recorded in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
