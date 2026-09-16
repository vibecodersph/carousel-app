#!/usr/bin/env python3
"""CLI for the recurring AI Brief production and feedback loop."""
from __future__ import annotations
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import aibrief_learning_loop as loop
from moneyball_analytics import atomic_write_text
from moneyball_record_history import record_history_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=loop.STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    health = sub.add_parser("health")
    health.add_argument("--db", type=Path, default=ROOT / "state/reels.db")
    health.add_argument("--feedback", type=Path, default=ROOT / "out/reel_report.moneyball.source_feedback.json")
    plan = sub.add_parser("plan")
    plan.add_argument("--input", type=Path, required=True)
    plan.add_argument("--batches-dir", type=Path, default=ROOT / "state/source_recommendations/aibrief_jp")
    seal = sub.add_parser("seal")
    seal.add_argument("--plan-id", required=True)
    seal.add_argument("--media", type=Path, required=True)
    seal.add_argument("--qa", required=True)
    event = sub.add_parser("event")
    event.add_argument("--run-id", required=True)
    event.add_argument("--status", choices=["started", "completed", "blocked", "failed"], required=True)
    event.add_argument("--detail", required=True)
    event.add_argument("--trigger", choices=["manual", "scheduled"], required=True)
    args = parser.parse_args(argv)
    with record_history_lock(args.state / "operation"):
        if args.command == "health":
            result = loop.health(args.db, json.loads(args.feedback.read_text()), args.state)
            atomic_write_text(ROOT / "out/aibrief_learning_loop.health.json", json.dumps(result, indent=2) + "\n")
            atomic_write_text(ROOT / "out/aibrief_learning_loop.health.md", "# AI Brief learning loop health\n\n" + "\n".join(f"- **{k}**: {v}" for k, v in result.items()) + "\n")
        elif args.command == "plan":
            result = loop.plan_production(json.loads(args.input.read_text()), args.batches_dir, args.state)
        elif args.command == "seal":
            result = loop.seal_production(args.plan_id, args.media, args.qa, args.state)
        else:
            result = {"run_id": args.run_id, "at": datetime.now(timezone.utc).isoformat(),
                      "status": args.status, "detail": args.detail, "trigger": args.trigger}
            loop.save_immutable(args.state / "runs" / f"{uuid.uuid4()}.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.command == "health" and result["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
