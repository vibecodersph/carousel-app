"""Exact production lineage and local health checks for the AI Brief loop."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import reel_ledger
import source_feedback as sf
from moneyball_analytics import atomic_write_text

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state/aibrief_learning_loop"
POLICY = {"check_hours": 168, "replenish_below_days": 14, "target_buffer_days": 21,
          "regular_posts_per_day": 4, "max_new_sources_per_run": 1,
          "max_new_clips_per_run": 4, "stalled_hours": 168,
          "schedule_grace_hours": 12}


def run_health(state, now):
    """Reconcile immutable receipts by run identity, never by filename order."""
    events = [json.loads(p.read_text()) for p in (state / "runs").glob("*.json")]
    events = sorted(events, key=lambda e: sf.timestamp(e["at"]))
    latest = {}
    for event in events:
        latest[(event["trigger"], event["run_id"])] = event
    completed = [e for e in events if e["trigger"] == "scheduled" and e["status"] == "completed"]
    last_completed = completed[-1]["at"] if completed else None
    # The first real receipt establishes monitoring; an empty installation has
    # no trustworthy activation time and must not invent one.
    anchor = sf.timestamp(last_completed) if last_completed else sf.timestamp(events[0]["at"]) if events else None
    deadline = anchor + timedelta(hours=POLICY["check_hours"] + POLICY["schedule_grace_hours"]) if anchor else None
    alerts = []
    if deadline and now > deadline:
        alerts.append("SCHEDULED_LOOP_OVERDUE" if completed else "NO_COMPLETED_SCHEDULED_RUN")
    open_runs = [e for e in latest.values() if e["status"] == "started"]
    for event in open_runs:
        if now - sf.timestamp(event["at"]) > timedelta(hours=POLICY["stalled_hours"]):
            alerts.append(f"STALLED_RUN:{event['trigger']}:{event['run_id']}")
    scheduled = [e for e in events if e["trigger"] == "scheduled" and e["status"] in ("completed", "blocked", "failed")]
    if scheduled and scheduled[-1]["status"] in ("blocked", "failed"):
        event = scheduled[-1]
        alerts.append(f"SCHEDULED_RUN_{event['status'].upper()}:{event['run_id']}")
    return {"last_completed_scheduled_run": last_completed,
            "scheduled_completion_deadline": deadline.isoformat() if deadline else None,
            "schedule_monitoring_status": "TRACKING" if events else "NO_RUN_HISTORY",
            "open_runs": open_runs, "alerts": alerts}


def save_immutable(path, data):
    if path.exists():
        if json.loads(path.read_text()) != data:
            raise ValueError(f"Immutable record already exists: {path}")
        return
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def plan_production(spec, batches_dir, state=STATE, now=None):
    now = now or datetime.now(timezone.utc)
    batches = sf.read_batches(batches_dir, "aibrief_jp")
    batch = next((b for b in batches if b["batch_id"] == spec["batch_id"]), None)
    if not batch or sf.timestamp(batch["recommended_at"]) > now:
        raise ValueError("A recorded prospective recommendation is required")
    entry = next((e for e in batch["entries"] if e["source_id"] == spec["source_id"] and e["selected"]), None)
    if not entry or entry["distribution_mode"] != "regular":
        raise ValueError("Loop production requires a selected regular source")
    if now - sf.timestamp(batch["recommended_at"]) > timedelta(days=30):
        raise ValueError("Recommendation expired; source again with current evidence")
    candidates_path = Path(spec["candidates_path"]).resolve()
    candidates = json.loads(candidates_path.read_text())
    if candidates_path.parent.name != spec["source_id"]:
        raise ValueError("Candidate folder/source mismatch")
    slug = spec["clip_slug"]
    if not any(c.get("slug") == slug for c in candidates.get("clips", [])):
        raise ValueError("Clip must exist in reconciled candidates")
    for key in ("learning_applied", "selection_reason", "hook_or_payoff_change", "evidence_media_ids"):
        if not spec.get(key):
            raise ValueError(f"Production plan needs {key}")
    archive = json.loads((batches_dir / "evidence" / f"{batch['feedback_id']}.json").read_text())
    known = {o["media_id"] for o in archive["observations"]}
    if not set(spec["evidence_media_ids"]) <= known:
        raise ValueError("Learning cites unknown evidence")
    plan = {**spec, "account": "aibrief_jp", "created_at": now.isoformat(),
            "feedback_id": batch["feedback_id"], "expected_metric": entry["expected_metric"],
            "distribution_mode": "regular", "candidates_path": str(candidates_path),
            "candidates_sha256": reel_ledger.hash_file(candidates_path)}
    plan["plan_id"] = sf.digest(plan)
    save_immutable(state / "plans" / f"{plan['plan_id']}.json", plan)
    return plan


def seal_production(plan_id, media_path, qa, state=STATE, now=None):
    now = now or datetime.now(timezone.utc)
    plan = json.loads((state / "plans" / f"{plan_id}.json").read_text())
    if sf.digest({k: v for k, v in plan.items() if k != "plan_id"}) != plan_id:
        raise ValueError("Production plan changed")
    media_path = media_path.resolve()
    if media_path.parent.name != plan["clip_slug"] or media_path.parent.parent.parent.name != plan["source_id"]:
        raise ValueError("Rendered clip/source mismatch")
    if reel_ledger.hash_file(Path(plan["candidates_path"])) != plan["candidates_sha256"]:
        raise ValueError("Candidates changed since learning review; create a new plan")
    if not media_path.is_file() or not media_path.stat().st_size or not qa.strip():
        raise ValueError("Nonempty rendered media and concrete audiovisual QA are required")
    notes = media_path.parent / "notes.json"
    if not notes.is_file():
        raise ValueError("Rendered notes are missing")
    record = {**plan, "sealed_at": now.isoformat(), "media_path": str(media_path),
              "content_hash": reel_ledger.hash_file(media_path), "notes_sha256": reel_ledger.hash_file(notes),
              "qa": qa}
    record["binding_id"] = sf.digest(record)
    destination = state / "bindings" / f"{record['content_hash']}.json"
    if destination.exists():
        previous = json.loads(destination.read_text())
        if all(previous.get(k) == v for k, v in record.items() if k not in ("sealed_at", "binding_id")):
            return previous
    save_immutable(destination, record)
    return record


def binding_for_media(media_path, state=STATE):
    path = state / "bindings" / f"{reel_ledger.hash_file(media_path)}.json"
    if not path.exists():
        for plan_path in (state / "plans").glob("*.json"):
            plan = json.loads(plan_path.read_text())
            expected = Path(plan["candidates_path"]).parent / "clips" / plan["clip_slug"]
            if Path(media_path).resolve().parent == expected.resolve():
                raise ValueError("Learning-managed clip needs sealed QA before scheduling")
        if state == STATE:
            sid = sf.source_id(Path(media_path).resolve().parent.parent.parent.name)
            for batch in sf.read_batches(ROOT / "state/source_recommendations/aibrief_jp", "aibrief_jp"):
                if (batch.get("exact_binding_required") and sid
                    and Path(media_path).stat().st_mtime >= sf.timestamp(batch["recommended_at"]).timestamp()
                    and any(e["selected"] and e["source_id"] == sid and e["distribution_mode"] == "regular" for e in batch["entries"])):
                    raise ValueError("Recommended source requires a clip learning plan and sealed QA before scheduling")
        return None
    record = json.loads(path.read_text())
    if sf.digest({k:v for k,v in record.items() if k != "binding_id"}) != record.get("binding_id"):
        raise ValueError("Learning binding has changed")
    if record["content_hash"] != reel_ledger.hash_file(media_path):
        raise ValueError("Media no longer matches learning binding")
    if record["notes_sha256"] != reel_ledger.hash_file(Path(media_path).parent / "notes.json"):
        raise ValueError("Rendered notes changed after learning QA")
    return record


def attach_exact_bindings(report, state=STATE):
    for post in report.get("posts", []):
        path = state / "bindings" / f"{post['identity'].get('content_hash', '')}.json"
        if path.exists():
            record = json.loads(path.read_text())
            if sf.digest({k:v for k,v in record.items() if k != "binding_id"}) != record.get("binding_id"):
                raise ValueError("Learning binding has changed")
            if (record["account"] != post["identity"]["account"] or
                record["source_id"] != sf.source_id(post["content_metadata"].get("source", ""))):
                raise ValueError("Exact learning binding conflicts with published identity")
            post["source_recommendation_binding"] = record


def health(db, feedback, state=STATE, now=None):
    now = now or datetime.now(timezone.utc)
    with sqlite3.connect(f"file:{Path(db).resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM reels WHERE channel_id='aibrief_jp'")]
    future = [r for r in rows if not r["trial_reel"] and r["status"] in ("scheduled", "previewed")
              and r["scheduled_at"] and sf.timestamp(r["scheduled_at"]) > now]
    horizon = now + timedelta(days=POLICY["replenish_below_days"])
    replenishment_window = [r for r in future if sf.timestamp(r["scheduled_at"]) <= horizon]
    expected = POLICY["regular_posts_per_day"] * POLICY["replenish_below_days"]
    demand = len(replenishment_window) < expected - 1
    runs = run_health(state, now)
    alerts = list(runs.pop("alerts"))
    age = (now - sf.timestamp(feedback["as_of"])).total_seconds() / 3600
    if not 0 <= age <= 96:
        alerts.append("STALE_FEEDBACK")
    bindings = {p.stem: json.loads(p.read_text()) for p in (state / "bindings").glob("*.json")}
    known = {r["content_hash"]: r for r in rows}
    pending_handoffs = []
    for key, record in bindings.items():
        row = known.get(key)
        if not row or row["status"] in ("new", "ready", "imported"):
            pending_handoffs.append(key)
        if not row and now - sf.timestamp(record["sealed_at"]) > timedelta(hours=48):
            alerts.append(f"UNQUEUED:{key}")
        elif row and row["status"] == "failed":
            alerts.append(f"PUBLISH_FAILED:{key}")
    sealed_plans = {b["plan_id"] for b in bindings.values()}
    pending = []
    latest_plans = {}
    for path in (state / "plans").glob("*.json"):
        p = json.loads(path.read_text())
        key = (p["candidates_path"], p["clip_slug"])
        if key not in latest_plans or p["created_at"] > latest_plans[key]["created_at"]:
            latest_plans[key] = p
    for p in latest_plans.values():
        if p["plan_id"] not in sealed_plans:
            pending.append(p["plan_id"])
            if now - sf.timestamp(p["created_at"]) > timedelta(hours=POLICY["stalled_hours"]):
                alerts.append(f"STALLED_PRODUCTION:{p['plan_id']}")
    observations = {(o["media_id"], o["window"]) for o in feedback["observations"]}
    for key, record in bindings.items():
        row = known.get(key)
        if row and row.get("published_at"):
            for window, max_hours in (("24h", 28), ("7d", 192)):
                # Only evaluate gaps through the analytics source timestamp.
                if (sf.timestamp(feedback["as_of"]) - sf.timestamp(row["published_at"])).total_seconds() / 3600 > max_hours:
                    if (str(row["media_id"]), window) not in observations:
                        alerts.append(f"MISSING_{window}:{key}")
    return {"as_of": now.isoformat(), "feedback_as_of": feedback["as_of"], "policy": POLICY,
            **runs, "status": "NEEDS_ATTENTION" if alerts else "OK",
            "failed_regular_clips": [r["content_hash"] for r in rows if not r["trial_reel"] and r["status"] == "failed"],
            "regular_queued": len(future),
            "replenishment_window_days": POLICY["replenish_below_days"],
            "replenishment_window_queued": len(replenishment_window),
            "queue_end": max((r["scheduled_at"] for r in future), default=None),
            "source_search_due": demand and not pending and not pending_handoffs and not alerts,
            "pending_plans": pending, "exact_bound_clips": len(bindings), "alerts": alerts,
            "pending_handoffs": pending_handoffs,
            "next_action": "REPAIR" if alerts else "RESUME_HANDOFF" if pending_handoffs else "RESUME_PRODUCTION" if pending else "SOURCE" if demand else "BUFFER_HEALTHY"}
