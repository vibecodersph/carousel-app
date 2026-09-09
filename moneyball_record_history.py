"""Durable, local history for measured account records and winner examples."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


@contextmanager
def record_history_lock(path: Path):
    """Serialize the complete read/compare/write transaction, including reports."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Moneyball record history is being updated by another run: {lock_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Record history timestamps must include a timezone")
    return parsed


def load_record_history(path: Path, *, account: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(history, dict) or history.get("schema_version") != 1:
            raise ValueError("unsupported record history schema")
        if history.get("account") != account or history.get("platform") != "instagram":
            raise ValueError("record history account/platform does not match this report")
        for key in ("current_records", "observations", "winner_archive", "champion_archive", "record_holder_content"):
            if not isinstance(history.get(key), dict):
                raise ValueError(f"record history {key} must be an object")
        if not isinstance(history.get("record_events"), list):
            raise ValueError("record history record_events must be a list")
        _timestamp(history["last_recorded_at"])
        _timestamp(history["first_recorded_at"])
        if not isinstance(history.get("input_fingerprint"), str):
            raise ValueError("record history input fingerprint is missing")
        baseline = history.get("comparison_baseline")
        if baseline is not None and not isinstance(baseline, dict):
            raise ValueError("record history comparison baseline must be an object or null")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Cannot safely read record history {path}: {exc}") from exc
    return history


def comparison_history(
    report: Mapping[str, Any], history: Mapping[str, Any] | None,
    *, ranking_policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Reject time reversal and replay the same source without duplicate alerts."""
    fingerprint = _digest(report.get("posts", []))
    if not history:
        return None, fingerprint
    as_of = report["report_metadata"]["as_of"]
    current_time = _timestamp(as_of)
    previous_time = _timestamp(history["last_recorded_at"])
    if current_time < previous_time:
        raise ValueError(
            "Report as_of predates the saved record history. Use a separate "
            "--record-history path for historical analysis; existing outputs were preserved."
        )
    if ranking_policy is not None and ranking_policy.get("signature") != (
        history.get("ranking_policy") or {}
    ).get("signature"):
        # Changing the metric set is an explicit new benchmark, not a change
        # in measured performance. Keep old champions in the durable archive.
        return None, fingerprint
    if current_time == previous_time:
        if fingerprint != history["input_fingerprint"]:
            raise ValueError(
                "The source changed at the saved record timestamp. Use a later as_of "
                "or a separate --record-history path to keep the audit history intact."
            )
        return copy.deepcopy(history.get("comparison_baseline")), fingerprint
    comparison = {
        key: copy.deepcopy(history[key])
        for key in (
            "account", "platform", "current_records", "observations", "last_recorded_at"
        )
    }
    if history.get("ranking_policy"):
        comparison["ranking_policy"] = copy.deepcopy(history["ranking_policy"])
    return comparison, fingerprint


def build_record_history(
    records: Mapping[str, Any],
    library: Mapping[str, Any],
    *,
    previous_history: Mapping[str, Any] | None,
    baseline: Mapping[str, Any] | None,
    input_fingerprint: str,
    holder_content: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous_history or {}
    as_of = records["as_of"]
    events = copy.deepcopy(previous.get("record_events", []))
    policy = records.get("ranking_policy")
    policies = copy.deepcopy(previous.get("policy_history", []))
    if policy and (not policies or policies[-1].get("policy") != policy):
        policies.append({"as_of": as_of, "policy": copy.deepcopy(policy)})
    archived_policies = copy.deepcopy(previous.get("archived_methodologies", []))
    if previous and policy and policy != previous.get("ranking_policy"):
        archived_policies.append({
            "archived_at": as_of,
            "policy": copy.deepcopy(previous.get("ranking_policy")),
            "last_recorded_at": previous.get("last_recorded_at"),
            "current_records": copy.deepcopy(previous.get("current_records", {})),
        })
    known_ids = {event["event_id"] for event in events}
    for event in records.get("events", []):
        if event["event_id"] not in known_ids:
            events.append(copy.deepcopy(event))
            known_ids.add(event["event_id"])

    champions = copy.deepcopy(previous.get("champion_archive", {}))
    content_archive = copy.deepcopy(previous.get("record_holder_content", {}))
    for media_id, content in (holder_content or {}).items():
        entry = content_archive.setdefault(media_id, {"first_seen_at": as_of, "versions": []})
        entry["last_seen_at"] = as_of
        fingerprint = _digest(content)
        if not any(version["fingerprint"] == fingerprint for version in entry["versions"]):
            entry["versions"].append({
                "first_seen_at": as_of, "fingerprint": fingerprint,
                "snapshot": copy.deepcopy(content),
            })
    for record_key, record in records["current_records"].items():
        observed = champions.setdefault(record_key, [])
        for leader in record.get("leaders", []):
            identity = {key: leader.get(key) for key in ("media_id", "value", "captured_at")}
            fingerprint = _digest(identity)
            if not any(item["fingerprint"] == fingerprint for item in observed):
                observed.append({
                    "first_seen_at": as_of, "fingerprint": fingerprint,
                    "snapshot": copy.deepcopy(leader),
                })

    archive = copy.deepcopy(previous.get("winner_archive", {}))
    for entry in archive.values():
        entry["current_member"] = False
    for winner in library.get("winners", []):
        media_id = str(winner["identity"]["media_id"])
        entry = archive.setdefault(media_id, {
            "first_seen_at": as_of,
            "first_snapshot": copy.deepcopy(winner),
            "content_versions": [],
            "membership_history": [],
        })
        entry.update({
            "last_seen_at": as_of,
            "current_member": True,
            "latest_snapshot": copy.deepcopy(winner),
        })
        content = {key: winner.get(key) for key in ("identity", "content", "source")}
        content_digest = _digest(content)
        if not any(version["fingerprint"] == content_digest for version in entry["content_versions"]):
            entry["content_versions"].append({
                "first_seen_at": as_of, "fingerprint": content_digest,
                "snapshot": copy.deepcopy(content),
            })
        evidence = winner.get("winner_evidence", {})
        membership = {
            "rankings": evidence.get("ranking_memberships", []),
            "aggregate_rank": (evidence.get("aggregate") or {}).get("rank"),
        }
        # Preserve changes in membership without duplicating scripts every run.
        memberships = entry["membership_history"]
        if (
            not memberships
            or memberships[-1]["membership"] != membership
            or memberships[-1].get("ranking_policy_signature") != (policy or {}).get("signature")
        ):
            memberships.append({
                "as_of": as_of, "membership": copy.deepcopy(membership),
                "ranking_policy_signature": (policy or {}).get("signature"),
            })

    return {
        "schema_version": 1,
        "account": records["account"],
        "platform": records["platform"],
        "first_recorded_at": previous.get("first_recorded_at", as_of),
        "last_recorded_at": as_of,
        "input_fingerprint": input_fingerprint,
        "ranking_policy": copy.deepcopy(policy),
        "policy_history": policies,
        "archived_methodologies": archived_policies,
        "last_report_status": records.get("status", "BASELINE_ESTABLISHED"),
        "comparison_baseline": copy.deepcopy(baseline),
        "current_records": copy.deepcopy(records["current_records"]),
        "observations": copy.deepcopy(records["observations"]),
        "record_events": events,
        "champion_archive": champions,
        "record_holder_content": content_archive,
        "winner_archive": archive,
        "scope": (
            "Observed record holders and Top-10 winner examples retained since the "
            "baseline. This does not reconstruct unobserved past champions."
        ),
    }
