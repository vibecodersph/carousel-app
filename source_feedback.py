"""Local, outcome-grounded source feedback; never changes publishing state.

Historical source evidence is not a reconstructed recommendation. Prospective
recommendations are immutable batches, attributed once to subsequent releases.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import parse_qs, urlsplit

POLICY = {
    "version": 1,
    "baseline_days": 28,
    "minimum_baseline_posts": 10,
    "decision_lookback_days": 90,
    "minimum_source_clips": 3,
    "minimum_uploader_clips": 6,
    "minimum_uploader_sources": 3,
    "minimum_topic_clips": 6,
    "minimum_topic_sources": 3,
    "minimum_comparable_fraction": 0.7,
    "low_percentile": 40,
    "high_percentile": 60,
    "consistent_fraction": 2 / 3,
    "minimum_action_count": 5,
    "minimum_rate_base": 100,
    "maximum_feedback_age_hours": 96,
    "recommendation_horizon_days": 30,
    "exploration_fraction": 0.2,
}
METRICS = ("skip", "looping", "save_rate", "share_rate", "reach")
WINDOWS = {"24h": (24, 28), "7d": (168, 192)}


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include a timezone")
    return result.astimezone(timezone.utc)


def source_id(value: str) -> str | None:
    """Exact YouTube IDs only; no fuzzy title or speaker joins."""
    import re
    value = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    parsed = urlsplit(value)
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    candidate = None
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif host == "youtube.com":
        candidate = parse_qs(parsed.query).get("v", [None])[0]
        if not candidate and parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
            candidate = parsed.path.split("/")[2]
    return candidate if candidate and re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


def digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def read_batches(root: Path, account: str) -> list[dict]:
    batches = []
    for path in sorted(root.glob("*.json")):
        batch = json.loads(path.read_text())
        if batch.get("schema_version") != 1 or batch.get("account") != account:
            raise ValueError(f"Invalid source recommendation batch: {path}")
        timestamp(batch["recommended_at"])
        if digest(batch["entries"]) != batch["entries_sha256"]:
            raise ValueError(f"Source recommendation batch has changed: {path}")
        if digest({k: v for k, v in batch.items() if k != "batch_sha256"}) != batch.get("batch_sha256"):
            raise ValueError(f"Source recommendation metadata has changed: {path}")
        batches.append(batch)
    ids = [b["batch_id"] for b in batches]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate source recommendation batch IDs")
    return batches


def _mode(post: dict) -> str:
    value = post.get("content_metadata", {}).get("trial_reel")
    return "trial" if value is True else "regular" if value is False else "unknown"


def _observation(post: dict, window: str, as_of: datetime) -> dict | None:
    snap = post.get("maturity_windows", {}).get(window)
    if not snap:
        return None
    published = timestamp(post["identity"]["published_at"])
    captured = timestamp(snap["captured_at"])
    age = (captured - published).total_seconds() / 3600
    if captured > as_of or not WINDOWS[window][0] <= age <= WINDOWS[window][1]:
        return None
    graduated = (post.get("trial_experiment") or {}).get("graduated_at")
    if _mode(post) == "trial" and graduated and timestamp(graduated) <= captured:
        return None  # mixed Trial/regular exposure cannot be a Trial comparison
    raw = snap.get("raw_metrics", {})
    r, v = number(raw.get("reach")), number(raw.get("views"))
    s, h = number(raw.get("saves")), number(raw.get("shares"))
    skip = number(raw.get("reels_skip_rate"))
    skip = skip if skip is not None and skip <= 100 else None
    metrics = {"skip": skip, "looping": v / r if v is not None and r else None,
               "save_rate": s / r if s is not None and r else None,
               "share_rate": h / v if h is not None and v else None, "reach": r}
    flags = []
    if r is not None and r < POLICY["minimum_rate_base"]:
        flags.append("LOW_BASE_REACH")
    if v is not None and v < POLICY["minimum_rate_base"]:
        flags.append("LOW_BASE_VIEWS")
    if s is not None and s < POLICY["minimum_action_count"]:
        flags.append("LOW_COUNT_SAVES")
    if h is not None and h < POLICY["minimum_action_count"]:
        flags.append("LOW_COUNT_SHARES")
    return {"age_hours": age, "captured_at": snap["captured_at"], "metrics": metrics,
            "raw": {"reach": r, "views": v, "saves": s, "shares": h}, "flags": flags}


def _percentile(value: float, peers: list[float], metric: str) -> float:
    less = sum(x < value for x in peers)
    equal = sum(x == value for x in peers)
    result = 100 * (less + equal / 2) / len(peers)
    return 100 - result if metric == "skip" else result


def _summary(rows: list[dict], kind: str) -> dict:
    """Count one earliest publication per underlying clip within each mode."""
    unique = {}
    for row in sorted(rows, key=lambda x: (x["published_at"], x["media_id"])):
        unique.setdefault(row["clip_key"], row)
    clips = list(unique.values())
    complete = [x for x in clips if x["balanced_percentile"] is not None]
    sources = len({x["source_id"] for x in complete})
    min_clips = POLICY[f"minimum_{kind}_clips"]
    min_sources = 1 if kind == "source" else POLICY[f"minimum_{kind}_sources"]
    scores = [x["balanced_percentile"] for x in complete]
    ratios = [x["reach_vs_baseline"] for x in complete if x["reach_vs_baseline"] is not None]
    low = sum(x["balanced_percentile"] < POLICY["low_percentile"]
              and x["reach_vs_baseline"] is not None and x["reach_vs_baseline"] < 1 for x in complete)
    high = sum(x["balanced_percentile"] > POLICY["high_percentile"]
               and x["reach_vs_baseline"] is not None and x["reach_vs_baseline"] > 1 for x in complete)
    comparable_fraction = len(complete) / len(clips) if clips else 0
    enough = (len(complete) >= min_clips and sources >= min_sources
              and comparable_fraction >= POLICY["minimum_comparable_fraction"])
    status = "INSUFFICIENT_EVIDENCE"
    if enough:
        status = "MIXED"
        if low / len(complete) >= POLICY["consistent_fraction"]:
            status = "REDUCE"
        elif high / len(complete) >= POLICY["consistent_fraction"]:
            status = "PREFER"
    return {"status": status, "publications": len(rows), "distinct_clips": len(clips),
            "comparable_clips": len(complete), "distinct_sources": sources,
            "comparable_fraction": comparable_fraction,
            "median_balanced_percentile": median(scores) if scores else None,
            "median_reach_vs_baseline": median(ratios) if ratios else None,
            "below_baseline_clips": low, "above_baseline_clips": high,
            "evidence_media_ids": [x["media_id"] for x in complete],
            "minimum_clips_required": min_clips, "minimum_sources_required": min_sources}


def _recommendation_for(post: dict, batches: list[dict]) -> tuple[dict, dict] | None:
    matches = []
    published = timestamp(post["identity"]["published_at"])
    sid = source_id(post.get("content_metadata", {}).get("source", ""))
    binding = post.get("source_recommendation_binding")
    for batch in batches:
        if binding and batch["batch_id"] != binding["batch_id"]:
            continue
        if batch.get("exact_binding_required") and not binding:
            continue
        start = timestamp(batch["recommended_at"])
        if binding and (binding["content_hash"] != post["identity"].get("content_hash")
                        or timestamp(binding["sealed_at"]) > published):
            raise ValueError("Invalid exact production attribution")
        if not start <= published or (not binding and published > start + timedelta(days=POLICY["recommendation_horizon_days"])):
            continue
        for entry in batch["entries"]:
            if entry["selected"] and entry["source_id"] == sid and entry["distribution_mode"] == _mode(post):
                matches.append((batch, entry))
    return max(matches, key=lambda x: (x[0]["recommended_at"], x[0]["batch_id"])) if matches else None


def build_feedback(report: dict, batches: list[dict] | None = None) -> dict:
    batches = batches or []
    meta = report["report_metadata"]
    account, as_of = meta["account"], timestamp(meta["as_of"])
    batches = [b for b in batches if timestamp(b["recommended_at"]) <= as_of]
    posts, seen, coverage = [], set(), Counter()
    for post in report.get("posts", []):
        ident = post.get("identity", {})
        if ident.get("platform") != "instagram" or ident.get("account") != account:
            continue
        if timestamp(ident["published_at"]) > as_of:
            continue
        media = str(ident.get("media_id") or "")
        if not media or media in seen:
            raise ValueError("Missing or duplicate media identity in source feedback")
        seen.add(media)
        coverage["published_posts"] += 1
        sid = source_id(post.get("content_metadata", {}).get("source", ""))
        if not sid:
            coverage["unresolved_source_posts"] += 1
            continue
        posts.append(post)
    observations = []
    for window in WINDOWS:
        measured = []
        for post in posts:
            obs = _observation(post, window, as_of)
            if obs is None:
                continue
            ident, content = post["identity"], post.get("content_metadata", {})
            artifact = post.get("generation_artifact", {})
            linked = _recommendation_for(post, batches)
            sid = source_id(content.get("source", ""))
            obs.update({"media_id": str(ident["media_id"]), "permalink": ident.get("permalink"),
                        "published_at": ident["published_at"], "source_id": sid,
                        "source_url": content["source"], "title": artifact.get("source_title") or sid,
                        "uploader": artifact.get("source_uploader") or None,
                        "hook": content.get("hook_text"), "distribution_mode": _mode(post),
                        "duration_bucket": content.get("duration_bucket") or None,
                        "clip_key": str(artifact.get("clip_dir") or ident.get("content_hash") or ident["media_id"]),
                        "window": window, "recommendation_id": linked[0]["batch_id"] if linked else None,
                        "topic": linked[1]["topic"] if linked else None,
                        "speaker": linked[1]["speaker"] if linked else None})
            measured.append(obs)
        # Baselines use only earlier publications, exclude the source being tested,
        # and never mix windows, distribution modes, or known duration buckets.
        for obs in measured:
            published = timestamp(obs["published_at"])
            peers = [x for x in measured
                     if published - timedelta(days=POLICY["baseline_days"]) <= timestamp(x["published_at"]) < published
                     and x["source_id"] != obs["source_id"]
                     and x["distribution_mode"] == obs["distribution_mode"]
                     and x["duration_bucket"] == obs["duration_bucket"]]
            peer_clips = {}
            for peer in sorted(peers, key=lambda x: x["published_at"]):
                peer_clips.setdefault(peer["clip_key"], peer)
            peers = list(peer_clips.values())
            percentiles, counts, baselines = {}, {}, {}
            for metric in METRICS:
                values = [x["metrics"][metric] for x in peers if x["metrics"][metric] is not None]
                counts[metric] = len(values)
                baselines[metric] = median(values) if values else None
                value = obs["metrics"][metric]
                percentiles[metric] = (_percentile(value, values, metric)
                    if value is not None and len(values) >= POLICY["minimum_baseline_posts"]
                    and obs["distribution_mode"] != "unknown" and obs["duration_bucket"] else None)
            complete = all(x is not None for x in percentiles.values()) and not any(
                flag.startswith("LOW_BASE") for flag in obs["flags"])
            obs.update({"baseline_counts": counts, "baseline_medians": baselines,
                        "directional_percentiles": percentiles,
                        "balanced_percentile": sum(percentiles.values()) / 5 if complete else None,
                        "reach_vs_baseline": (obs["metrics"]["reach"] / baselines["reach"]
                            if obs["metrics"]["reach"] is not None and baselines["reach"]
                            and percentiles["reach"] is not None else None)})
            observations.append(obs)
        coverage[f"measured_{window}"] = len(measured)
    grouped = defaultdict(list)
    cutoff = as_of - timedelta(days=POLICY["decision_lookback_days"])
    for obs in observations:
        if timestamp(obs["published_at"]) < cutoff:
            continue
        keys = [("source", obs["source_id"])]
        if obs["uploader"]:
            keys.append(("uploader", obs["uploader"]))
        if obs["topic"]:
            keys.append(("topic", obs["topic"]))
        for kind, key in keys:
            grouped[(kind, key, obs["distribution_mode"], obs["window"])].append(obs)
    groups = []
    for (kind, key, mode, window), rows in sorted(grouped.items()):
        groups.append({"kind": kind, "key": key, "distribution_mode": mode, "window": window,
                       "title": rows[0]["title"] if kind == "source" else key,
                       "source_url": rows[0]["source_url"] if kind == "source" else None,
                       **_summary(rows, kind)})
    audit = []
    for batch in batches:
        for entry in batch["entries"]:
            if not entry["selected"]:
                continue
            matched = [x for x in observations if x["recommendation_id"] == batch["batch_id"]
                       and x["source_id"] == entry["source_id"]]
            # A publication with no fixed checkpoint is not "never published".
            releases = [p for p in posts if (link := _recommendation_for(p, batches))
                        and link[0]["batch_id"] == batch["batch_id"]
                        and link[1]["source_id"] == entry["source_id"]]
            by_window = {}
            for window in WINDOWS:
                rows = [x for x in matched if x["window"] == window]
                summary = _summary(rows, "source")
                metric = entry["expected_metric"]
                eligible = [x for x in rows if x["directional_percentiles"][metric] is not None]
                if metric != "reach":
                    eligible = [x for x in eligible if not any(flag.startswith("LOW_BASE") for flag in x["flags"])]
                if metric in ("save_rate", "share_rate"):
                    flag = "LOW_COUNT_SAVES" if metric == "save_rate" else "LOW_COUNT_SHARES"
                    base = "LOW_BASE_REACH" if metric == "save_rate" else "LOW_BASE_VIEWS"
                    eligible = [x for x in eligible if flag not in x["flags"] and base not in x["flags"]]
                unique = {}
                for row in sorted(eligible, key=lambda x: x["published_at"]):
                    unique.setdefault(row["clip_key"], row)
                vals = [x["directional_percentiles"][metric] for x in unique.values()]
                result = "INSUFFICIENT_EVIDENCE"
                if len(vals) >= POLICY["minimum_source_clips"]:
                    result = "MIXED"
                    if sum(x < 40 for x in vals) / len(vals) >= POLICY["consistent_fraction"]:
                        result = "MISS"
                    elif sum(x > 60 for x in vals) / len(vals) >= POLICY["consistent_fraction"]:
                        result = "SUPPORTED"
                by_window[window] = {**summary, "expected_metric_result": result,
                                     "expected_metric_eligible_clips": len(vals)}
            audit.append({"batch_id": batch["batch_id"], "source_id": entry["source_id"],
                          "source_url": entry["source_url"], "recommended_at": batch["recommended_at"],
                          "rationale": entry["rationale"], "expected_metric": entry["expected_metric"],
                          "original_priority": entry["feedback"]["priority"],
                          "published_reels": len(releases), "windows": by_window,
                          "status": "PUBLISHED" if releases else "NO_PUBLICATION_OBSERVED"})
    coverage["historical_posts_without_recommendation"] = sum(_recommendation_for(p, batches) is None for p in posts)
    coverage["tracked_recommendation_batches"] = len(batches)
    output = {"schema_version": 1, "account": account, "platform": "instagram",
              "as_of": meta["as_of"], "policy": POLICY, "coverage": dict(coverage),
              "groups": groups, "observations": observations, "recommendation_audit": audit,
              "limitations": [
                  "Observational source outcomes, not source causation or predicted performance.",
                  "Historical publications are backfilled as outcomes, never invented recommendations.",
                  "24h evidence is preliminary; only eligible 7d groups change sourcing priority.",
                  "Unknown distribution/duration and missing checkpoints cannot qualify a decision.",
                  "Low-count actions remain labeled; no follower-conversion or production-efficiency claim.",
                  "Uploader is not speaker identity. Topic and speaker are explicit prospective labels.",
                  "No keyword-based topic inference; untracked historical topics remain unknown."]}
    output["feedback_id"] = digest(output)
    return output


def source_context(feedback: dict, sid: str, mode: str, *, uploader: str = "", topic: str = "") -> dict:
    groups = [x for x in feedback["groups"] if x["distribution_mode"] == mode
              and ((x["kind"] == "source" and x["key"] == sid)
                   or (x["kind"] == "uploader" and uploader and x["key"] == uploader)
                   or (x["kind"] == "topic" and topic and x["key"] == topic))]
    mature = [x for x in groups if x["window"] == "7d"]
    # Direct source evidence dominates broader priors; conflicting broader
    # signals stay mixed. One uploader hit cannot endorse a new episode.
    direct = [x for x in mature if x["kind"] == "source" and x["status"] != "INSUFFICIENT_EVIDENCE"]
    evidence = direct or [x for x in mature if x["status"] != "INSUFFICIENT_EVIDENCE"]
    statuses = {x["status"] for x in evidence}
    priority = "EXPLORE"
    if "REDUCE" in statuses and "PREFER" not in statuses:
        priority = "REDUCE"
    elif "PREFER" in statuses and "REDUCE" not in statuses:
        priority = "PREFER"
    return {"priority": priority, "evidence": groups,
            "reason": "7d matched outcomes" if evidence else "Insufficient mature evidence; bounded exploration",
            "feedback_id": feedback["feedback_id"], "feedback_as_of": feedback["as_of"]}


def recommend(feedback: dict, candidates: list[dict], *, account: str, batch_id: str,
              count: int, now: datetime) -> dict:
    """Deterministic portfolio selection; editorial order breaks tier ties."""
    if feedback["account"] != account or feedback["policy"] != POLICY:
        raise ValueError("Feedback account or policy mismatch; regenerate Moneyball")
    unsigned = {k: v for k, v in feedback.items() if k != "feedback_id"}
    if digest(unsigned) != feedback["feedback_id"]:
        raise ValueError("Feedback integrity mismatch; regenerate Moneyball")
    age = (now - timestamp(feedback["as_of"])).total_seconds() / 3600
    if not 0 <= age <= POLICY["maximum_feedback_age_hours"]:
        raise ValueError("Source feedback is stale or future-dated; regenerate Moneyball")
    if count < 1:
        raise ValueError("Recommendation count must be positive")
    entries, seen = [], set()
    for candidate in candidates:
        sid = source_id(candidate.get("source_url", ""))
        if not sid or sid in seen:
            raise ValueError("Every candidate needs a unique exact YouTube URL/ID")
        seen.add(sid)
        for field in ("rationale", "speaker", "topic", "uploader", "title"):
            if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                raise ValueError(f"Candidate {sid} needs {field}")
        mode, metric = candidate.get("distribution_mode"), candidate.get("expected_metric")
        if mode not in {"regular", "trial"} or metric not in METRICS:
            raise ValueError(f"Candidate {sid} needs a valid mode and expected_metric")
        context = source_context(feedback, sid, mode, uploader=candidate["uploader"], topic=candidate["topic"])
        entries.append({**candidate, "source_id": sid, "feedback": context,
                        "selected": False, "selection_reason": "Not selected"})
    # Reserve up to 20% (at least one) for exploration. With too little mature
    # evidence, return a smaller shortlist, never silently fill with weak sources.
    exploration_budget = max(1, math.ceil(count * POLICY["exploration_fraction"]))
    preferred = [x for x in entries if x["feedback"]["priority"] == "PREFER"]
    exploration = [x for x in entries if x["feedback"]["priority"] == "EXPLORE"]
    reduced_tests = [x for x in entries if x["feedback"]["priority"] == "REDUCE"
                     and isinstance(x.get("changed_hypothesis"), str) and x["changed_hypothesis"].strip()]
    probes = (exploration + reduced_tests)[:exploration_budget]
    selected = preferred[:max(0, count - len(probes))] + probes
    for entry in selected:
        entry["selected"] = True
        entry["selection_reason"] = ("Mature evidence supports priority" if entry in preferred
                                      else "Bounded exploration" if entry in exploration
                                      else "Bounded retest with explicit changed hypothesis")
    batch = {"schema_version": 1, "account": account, "batch_id": batch_id,
            "recommended_at": now.isoformat(), "policy_version": POLICY["version"],
            "feedback_id": feedback["feedback_id"], "feedback_as_of": feedback["as_of"],
            "requested_count": count, "selected_count": len(selected),
            "exploration_budget": exploration_budget, "entries": entries,
            "entries_sha256": digest(entries),
            "status": "READY" if len(selected) == count else "PARTIAL_EVIDENCE_LIMITED"}
    batch["batch_sha256"] = digest(batch)
    return batch


def attach_candidate_context(report: dict, feedback: dict) -> str:
    """Expose both positive and negative source evidence without changing clip scores."""
    account = report.get("report_metadata", {}).get("account")
    if account != feedback["account"]:
        raise ValueError("Candidate/source feedback account mismatch")
    report["source_feedback"] = {"as_of": feedback["as_of"], "feedback_id": feedback["feedback_id"]}
    lines = ["", "## Measured source feedback", "",
             f"Evidence as of {feedback['as_of']}. Use the source recommendation command before recommending a source; this context does not change clip-review scores.", "",
             "| Source | Mode | Source priority | Evidence |", "| --- | --- | --- | --- |"]
    for source in report.get("sources", []):
        sid = source_id(source.get("video_id") or source.get("url") or "")
        if sid is None:
            source["source_feedback"] = {"status": "SOURCE_ID_UNRESOLVED"}
            continue
        source["source_feedback"] = {}
        for mode in ("regular", "trial"):
            context = source_context(feedback, sid, mode, uploader=source.get("uploader") or "")
            source["source_feedback"][mode] = context
            evidence = "; ".join(f"{g['kind']} {g['window']}: {g['status']} ({g['comparable_clips']} clips)" for g in context["evidence"])
            lines.append(f"| {sid} | {mode} | {context['priority']} | {evidence or 'No comparable historical evidence'} |")
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def render_markdown(feedback: dict) -> str:
    lines = ["# Source recommendation feedback", "", f"Source data as of **{feedback['as_of']}**.",
             f"Evidence revision: `{feedback['feedback_id']}`.", "",
             "Historical source outcomes are backfilled. Recommendation accuracy starts with recorded prospective batches.",
             "24h and 7d remain separate; only 7d evidence changes priority. Baselines use earlier, other-source reels from the preceding 28 days, in the same distribution mode and duration bucket.", "",
             "## Coverage", "", *[f"- {k}: {v}" for k, v in feedback["coverage"].items()], "",
             "## Source, uploader and topic decisions", "",
             "REDUCE requires at least two thirds of eligible distinct clips below P40 and below their matched reach baseline; PREFER requires two thirds above P60 and above baseline. Source decisions need three clips; uploader/topic decisions need six clips across three sources. At least 70% of measured distinct clips must be comparable. These are screening rules, not significance tests.", "",
             "| Window | Mode | Level | Source / group | Decision | Comparable clips / distinct clips | Sources | Median balanced percentile | Reach / baseline |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    order = {"REDUCE": 0, "PREFER": 1, "MIXED": 2, "INSUFFICIENT_EVIDENCE": 3}
    for g in sorted(feedback["groups"], key=lambda x: (x["window"] != "7d", order[x["status"]], x["kind"], x["key"])):
        label = f"[{_cell(g['title'])}]({g['source_url']})" if g["source_url"] else _cell(g["title"])
        score, ratio = g["median_balanced_percentile"], g["median_reach_vs_baseline"]
        lines.append(f"| {g['window']} | {g['distribution_mode']} | {g['kind']} | {label} | {g['status']} | {g['comparable_clips']}/{g['distinct_clips']} | {g['distinct_sources']} | {score:.1f} | {ratio:.2f}× |" if score is not None and ratio is not None else f"| {g['window']} | {g['distribution_mode']} | {g['kind']} | {label} | {g['status']} | {g['comparable_clips']}/{g['distinct_clips']} | {g['distinct_sources']} | — | — |")
    lines += ["", "## Recommendation accountability", ""]
    if not feedback["recommendation_audit"]:
        lines += ["No recorded prospective recommendations have matured. Historical source performance does not establish which past recommendations failed."]
    for item in feedback["recommendation_audit"]:
        lines.append(f"- `{item['batch_id']}` [{item['source_id']}]({item['source_url']}): {item['published_reels']} published; expected {item['expected_metric']}; 24h {item['windows']['24h']['expected_metric_result']}; 7d {item['windows']['7d']['expected_metric_result']}.")
    lines += ["", "## Per-reel evidence", "", "Skip is the direct API percentage; raw skip-event counts are unavailable. Saves and shares below five are LOW COUNT. Reach sums across posts are not unique audience counts.", "",
              "| Window | Mode | Reel | Source | Age | Reach | Skip ↓ | Views/reach | Saves/reach | Shares/views | Balanced percentile | Flags |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in feedback["observations"]:
        r, m = row["raw"], row["metrics"]
        def rate(name, numerator, denominator, pct=False):
            value = m[name]
            return "—" if value is None else f"{r[numerator]:g}/{r[denominator]:g} = {value * (100 if pct else 1):.3f}{'%' if pct else '×'}"
        score = row["balanced_percentile"]
        lines.append(f"| {row['window']} | {row['distribution_mode']} | [{_cell(row['hook'] or row['media_id'])}]({row['permalink']}) | {row['source_id']} | {row['age_hours']:.2f}h | {_cell(r['reach'])} | {_cell(m['skip'])} | {rate('looping', 'views', 'reach')} | {rate('save_rate', 'saves', 'reach', True)} | {rate('share_rate', 'shares', 'views', True)} | {score if score is not None else '—'} | {', '.join(row['flags'])} |")
    lines += ["", "## Limits", "", *[f"- {x}" for x in feedback["limitations"]], ""]
    return "\n".join(lines)
