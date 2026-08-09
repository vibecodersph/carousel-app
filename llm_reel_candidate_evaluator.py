"""Gemini-backed semantic evaluation of Reel candidates against measured winners.

This module deliberately does not use lexical similarity as a fallback.  It
uses a two-pass review:

1. A blind semantic pass sees the Top-10 membership of winner assets, but not
   rank or metric values.
2. A verifier sees exact 24-hour evidence only for the analogues selected in
   the first pass and makes the bounded editorial recommendation.

Exact links and metrics in the report are joined from the winner library after
the model responds; they are never trusted as model-generated facts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

import moneyball_analytics as moneyball
import reel_candidate_evaluator as diagnostic
from fetch_tweet_data import load_env_file


ROOT = Path(__file__).resolve().parent
SCHEMA_VERSION = 1
PROMPT_VERSION = "moneyball-semantic-v2-primary-hook-only"

CATEGORIES = (
    "total_interactions_per_reach",
    "watch_depth",
    "three_second_skip_rate",
    "saves_per_1000_reach",
    "views_per_reached_account",
    "aggregate_top_10",
)

CATEGORY_DETAILS: dict[str, dict[str, str]] = {
    "total_interactions_per_reach": {
        "label": "Total interactions / reach",
        "signal_family": "INTENT_ACTION",
        "question": (
            "Does the content create a concrete reason to react, discuss, "
            "share, save, or otherwise interact?"
        ),
        "caveat": (
            "Meta total_interactions includes correlated components such as "
            "saves; it is not a pure high-intent measure."
        ),
    },
    "watch_depth": {
        "label": "Watch depth",
        "signal_family": "ATTENTION_REPLAY",
        "question": (
            "Does the script sustain progression, escalate evidence, and "
            "deliver the promised payoff without dead setup?"
        ),
        "caveat": "Duration materially affects watch depth.",
    },
    "three_second_skip_rate": {
        "label": "3-second skip rate",
        "signal_family": "ATTENTION_REPLAY",
        "question": (
            "Is the first spoken idea immediately understandable, specific, "
            "and congruent with the hook?"
        ),
        "caveat": (
            "This supports an opening hypothesis, not a predicted skip rate. "
            "Lower measured values are stronger."
        ),
    },
    "saves_per_1000_reach": {
        "label": "Saves / 1,000 reach",
        "signal_family": "INTENT_ACTION",
        "question": (
            "Does the segment provide durable reference value: a method, "
            "workflow, reusable explanation, or memorable fact pattern?"
        ),
        "caveat": (
            "A save CTA is not evidence of reference value; raw saves and "
            "reach remain important."
        ),
    },
    "views_per_reached_account": {
        "label": "Views / reached account",
        "signal_family": "ATTENTION_REPLAY",
        "question": (
            "Is there a credible replay trigger such as density, a visual "
            "demonstration, surprising detail, or a loop?"
        ),
        "caveat": (
            "Views per reached account can be consistent with replay but does "
            "not prove that an individual viewer rewatched."
        ),
    },
    "aggregate_top_10": {
        "label": "Balanced aggregate Top 10",
        "signal_family": "AGGREGATE_SUMMARY",
        "question": (
            "Does the candidate combine attention and useful-action "
            "mechanisms instead of relying on one isolated strength?"
        ),
        "caveat": (
            "Aggregate is a summary of the five metrics, not a sixth "
            "independent vote."
        ),
    },
}

DECISIONS = (
    "ADVANCE",
    "ADVANCE_AS_TRIAL",
    "REVISE",
    "REJECT",
    "MANUAL_REVIEW",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SemanticProfile(StrictModel):
    target_viewer: str
    core_topic: str
    audience_promise: str
    hook_mechanisms: list[str]
    curiosity_driver: str
    payoff_type: str
    proof_form: str
    delivery_structure: str
    attention_hypothesis: str
    action_hypothesis: str
    content_risks: list[str]


class ClaimReview(StrictModel):
    claim: str
    status: Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED", "UNCLEAR"]
    source_excerpt: str
    start_seconds: float | None
    end_seconds: float | None
    explanation: str
    required_revision: str | None


class ClaimSupport(StrictModel):
    overall_status: Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED", "UNCLEAR"]
    claims: list[ClaimReview]


class BlindAnalogue(StrictModel):
    media_id: str
    relation: Literal[
        "CLOSE_MECHANISM",
        "PARTIAL_MECHANISM",
        "SURFACE_ONLY",
        "CONTRAST",
    ]
    candidate_evidence_excerpt: str
    winner_evidence_excerpt: str
    shared_mechanisms: list[str]
    material_differences: list[str]
    duration_caveat: str | None
    why_relevant: str


class BlindCategoryComparison(StrictModel):
    category: Literal[
        "total_interactions_per_reach",
        "watch_depth",
        "three_second_skip_rate",
        "saves_per_1000_reach",
        "views_per_reached_account",
        "aggregate_top_10",
    ]
    fit_hypothesis: Literal[
        "STRONG",
        "PLAUSIBLE",
        "WEAK",
        "NO_CLOSE_ANALOGUE",
        "UNASSESSABLE",
    ]
    analogues: list[BlindAnalogue] = Field(max_length=3)
    candidate_case: str
    counterevidence: str
    uncertainty: str


class BlindSemanticReview(StrictModel):
    candidate_id: str
    semantic_profile: SemanticProfile
    claim_support: ClaimSupport
    category_comparisons: list[BlindCategoryComparison]
    source_and_topic_saturation: str
    blind_review_summary: str


class EvidenceInterpretation(StrictModel):
    category: Literal[
        "total_interactions_per_reach",
        "watch_depth",
        "three_second_skip_rate",
        "saves_per_1000_reach",
        "views_per_reached_account",
        "aggregate_top_10",
    ]
    fit_after_metrics: Literal[
        "STRONG",
        "PLAUSIBLE",
        "WEAK",
        "NO_CLOSE_ANALOGUE",
        "UNASSESSABLE",
    ]
    supporting_analogue_ids: list[str] = Field(max_length=3)
    evidence_interpretation: str
    important_difference: str
    caveat: str
    conclusion: str


class VerifierAudit(StrictModel):
    blind_analogue_quality: Literal["CONFIRMED", "WEAKENED", "REJECTED"]
    claim_support_check: Literal["CONFIRMED", "WEAKENED", "REJECTED"]
    surface_match_risk: str
    citation_issues: list[str]
    duration_confounding: str
    causal_or_predictive_language_removed: str


class CrossCategorySynthesis(StrictModel):
    credible_categories: list[
        Literal[
            "total_interactions_per_reach",
            "watch_depth",
            "three_second_skip_rate",
            "saves_per_1000_reach",
            "views_per_reached_account",
            "aggregate_top_10",
        ]
    ]
    independent_signal_families_supported: list[
        Literal["ATTENTION_REPLAY", "INTENT_ACTION"]
    ]
    strongest_mechanisms: list[str]
    important_differences_from_winners: list[str]
    evidence_status: Literal[
        "STRONG_ANALOGUE_EVIDENCE",
        "DIRECTIONAL_ANALOGUE_EVIDENCE",
        "NOVEL_NO_CLOSE_ANALOGUE",
        "CONFLICTING",
        "INSUFFICIENT_INPUT",
    ]


class EditorialDecision(StrictModel):
    label: Literal[
        "ADVANCE",
        "ADVANCE_AS_TRIAL",
        "REVISE",
        "REJECT",
        "MANUAL_REVIEW",
    ]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    confidence_scope: str
    reason: str
    strong_points: list[str]
    blocking_risks: list[str]
    must_fix_before_use: list[str]
    revision_plan: str | None
    test_hypothesis: str | None
    primary_measurement: str | None
    non_prediction_statement: str


class VerifiedCandidateReview(StrictModel):
    candidate_id: str
    verifier_audit: VerifierAudit
    category_interpretations: list[EvidenceInterpretation]
    cross_category_synthesis: CrossCategorySynthesis
    decision: EditorialDecision


class FalseNegativeScreenItem(StrictModel):
    candidate_id: str
    verdict: Literal[
        "LIKELY_FALSE_NEGATIVE",
        "POSSIBLE_FALSE_NEGATIVE",
        "REJECTION_SUPPORTED",
        "MANUAL_REVIEW",
    ]
    claim_support_status: Literal[
        "SUPPORTED",
        "PARTIAL",
        "UNSUPPORTED",
        "UNCLEAR",
    ]
    distinctive_payoff_present: bool
    deep_review_priority: Literal[
        "TOP_5",
        "SECONDARY",
        "NO_DEEP_REVIEW",
    ]
    deep_review_rank: int | None = Field(ge=1, le=5)
    strongest_category_hypotheses: list[
        Literal[
            "total_interactions_per_reach",
            "watch_depth",
            "three_second_skip_rate",
            "saves_per_1000_reach",
            "views_per_reached_account",
            "aggregate_top_10",
        ]
    ]
    reason: str
    required_revision: str | None


class FalseNegativeScreenBatch(StrictModel):
    reviews: list[FalseNegativeScreenItem]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _opaque_ref(prefix: str, value: str) -> str:
    return f"{prefix}-{_sha256_text(value)[:16]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_metric(category: str, value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unavailable"
    if category in {"total_interactions_per_reach", "watch_depth"}:
        return f"{value * 100:.2f}%"
    if category == "three_second_skip_rate":
        return f"{value:.2f}%"
    if category == "saves_per_1000_reach":
        return f"{value:.2f}"
    if category == "views_per_reached_account":
        return f"{value:.3f}×"
    if category == "aggregate_top_10":
        return f"{value:.2f} directional-percentile average"
    return str(value)


GEMINI_OPENAI_COMPAT_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/"
)


def load_gemini_api_key() -> str:
    load_env_file(ROOT / ".env")
    load_env_file(Path.home() / ".hermes" / ".env")
    value = os.environ.get("GEMINI_API_KEY") or os.environ.get(
        "GOOGLE_API_KEY"
    )
    if not value:
        raise ValueError(
            "LLM analysis requires GEMINI_API_KEY (or GOOGLE_API_KEY). "
            "No deterministic or lexical fallback was used."
        )
    return value


def _response_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for item in _sequence(content):
        mapping = _mapping(item)
        text = _text(mapping.get("text"))
        if not text:
            text = _text(getattr(item, "text", None))
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _parse_structured_content_fallback(
    schema: type[StrictModel],
    content: Any,
) -> StrictModel | None:
    """Validate JSON text when the compatibility SDK leaves ``parsed`` empty."""

    text = _response_content_text(content)
    if not text:
        return None
    candidates = [text]
    fenced = re.fullmatch(
        r"\s*```(?:json)?\s*(.*?)\s*```\s*",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        candidates.append(fenced.group(1).strip())
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1])
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return schema.model_validate_json(candidate)
        except (TypeError, ValueError):
            continue
    return None


class GeminiResponseAdapter:
    """Injectable adapter for Gemini structured output.

    The installed OpenAI Python package is used only as a protocol client for
    Google's documented OpenAI-compatible Gemini endpoint. Requests are sent
    to ``generativelanguage.googleapis.com``, never to OpenAI.
    """

    provider = "gemini"
    endpoint = GEMINI_OPENAI_COMPAT_BASE_URL

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: str,
        timeout_seconds: float,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.client = OpenAI(
            api_key=api_key or load_gemini_api_key(),
            base_url=self.endpoint,
        )

    def parse(
        self,
        *,
        schema: type[StrictModel],
        instructions: str,
        input_payload: list[dict[str, Any]],
        max_output_tokens: int,
        prompt_cache_key: str,
    ) -> tuple[StrictModel, dict[str, Any]]:
        developer_text = "\n\n".join(
            _text(row.get("content"))
            for row in input_payload
            if _text(row.get("role")) == "developer"
            and _text(row.get("content"))
        )
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    instructions
                    + (
                        "\n\nSUPPLIED REFERENCE MATERIAL:\n" + developer_text
                        if developer_text
                        else ""
                    )
                ),
            }
        ]
        messages.extend(
            {
                "role": (
                    "assistant"
                    if _text(row.get("role")) == "assistant"
                    else "user"
                ),
                "content": _text(row.get("content")),
            }
            for row in input_payload
            if _text(row.get("role")) != "developer"
            and _text(row.get("content"))
        )
        completion = self.client.beta.chat.completions.parse(
            model=self.model,
            messages=messages,
            response_format=schema,
            reasoning_effort=self.reasoning_effort,
            max_completion_tokens=max_output_tokens,
            timeout=self.timeout_seconds,
        )
        if not completion.choices:
            raise RuntimeError("Gemini returned no completion choices")
        message = completion.choices[0].message
        parsed = message.parsed
        structured_parse_source = "sdk_parsed"
        if parsed is None:
            parsed = _parse_structured_content_fallback(
                schema,
                getattr(message, "content", None),
            )
            structured_parse_source = "content_fallback"
        if parsed is None:
            content_text = _response_content_text(
                getattr(message, "content", None)
            )
            refusal_text = _response_content_text(
                getattr(message, "refusal", None)
            )
            finish_reason = getattr(
                completion.choices[0],
                "finish_reason",
                None,
            )
            raise RuntimeError(
                f"Gemini response {getattr(completion, 'id', '')} had no "
                "parseable structured output "
                f"(finish_reason={finish_reason!r}, "
                f"content_sha256={_sha256_text(content_text)}, "
                f"content_chars={len(content_text)}, "
                f"refusal_sha256={_sha256_text(refusal_text)}, "
                f"refusal_chars={len(refusal_text)})"
            )
        usage = getattr(completion, "usage", None)
        trace = {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "response_id": getattr(completion, "id", None),
            "model": getattr(completion, "model", self.model),
            "finish_reason": getattr(
                completion.choices[0],
                "finish_reason",
                None,
            ),
            "structured_parse_source": structured_parse_source,
            "usage": (
                usage.model_dump(mode="json")
                if hasattr(usage, "model_dump")
                else None
            ),
            "output_text_sha256": _sha256_text(
                _text(getattr(message, "content", None))
            ),
        }
        return parsed, trace


class RequestCache:
    def __init__(self, root: Path | None, *, enabled: bool) -> None:
        self.root = root
        self.enabled = enabled and root is not None

    def _path(self, request_hash: str) -> Path:
        assert self.root is not None
        return self.root / f"{request_hash}.json"

    def read(
        self,
        request_hash: str,
        schema: type[StrictModel],
    ) -> tuple[StrictModel, dict[str, Any]] | None:
        if not self.enabled:
            return None
        path = self._path(request_hash)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        parsed = schema.model_validate(payload["parsed"])
        trace = dict(_mapping(payload.get("trace")))
        trace["cache_status"] = "HIT"
        trace["cache_file"] = str(path.resolve())
        return parsed, trace

    def write(
        self,
        request_hash: str,
        *,
        schema: type[StrictModel],
        parsed: StrictModel,
        trace: Mapping[str, Any],
    ) -> None:
        if not self.enabled:
            return
        path = self._path(request_hash)
        payload = {
            "schema": schema.__name__,
            "prompt_version": PROMPT_VERSION,
            "parsed": parsed.model_dump(mode="json"),
            "trace": dict(trace),
        }
        moneyball.atomic_write_text(
            path,
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n",
        )


class LLMRunner:
    def __init__(
        self,
        adapter: GeminiResponseAdapter,
        *,
        cache: RequestCache,
        prompt_cache_key: str,
    ) -> None:
        self.adapter = adapter
        self.cache = cache
        self.prompt_cache_key = prompt_cache_key

    def run(
        self,
        *,
        schema: type[StrictModel],
        instructions: str,
        input_payload: list[dict[str, Any]],
        max_output_tokens: int,
    ) -> tuple[StrictModel, dict[str, Any]]:
        request_material = {
            "provider": getattr(self.adapter, "provider", "unknown"),
            "endpoint": getattr(self.adapter, "endpoint", None),
            "model": self.adapter.model,
            "reasoning_effort": self.adapter.reasoning_effort,
            "prompt_version": PROMPT_VERSION,
            "schema": schema.model_json_schema(),
            "instructions": instructions,
            "input": input_payload,
        }
        request_hash = _sha256_text(_json(request_material))
        cached = self.cache.read(request_hash, schema)
        if cached is not None:
            parsed, trace = cached
            trace["request_sha256"] = request_hash
            return parsed, trace
        parsed, trace = self.adapter.parse(
            schema=schema,
            instructions=instructions,
            input_payload=input_payload,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=self.prompt_cache_key,
        )
        trace = {
            **trace,
            "cache_status": "MISS",
            "request_sha256": request_hash,
            "prompt_version": PROMPT_VERSION,
        }
        self.cache.write(
            request_hash,
            schema=schema,
            parsed=parsed,
            trace=trace,
        )
        return parsed, trace


def _winner_asset(raw: Mapping[str, Any]) -> dict[str, Any]:
    identity = _mapping(raw.get("identity"))
    content = _mapping(raw.get("content"))
    source = _mapping(raw.get("source"))
    return {
        "media_id": _text(identity.get("media_id")),
        "permalink": _text(identity.get("permalink")),
        "source": {
            "video_id": _text(source.get("video_id")),
            "title": _text(source.get("title")),
            "uploader": _text(source.get("uploader")),
            "url": _text(source.get("url")),
        },
        "duration_seconds": content.get("duration_seconds"),
        "published_hook": _text(
            _mapping(content.get("published_hook")).get("value")
        ),
        "source_selection_hook": _text(content.get("source_selection_hook")),
        "source_transcript": _text(
            _mapping(content.get("source_transcript")).get("text")
        ),
        "japanese_script": _text(
            _mapping(content.get("japanese_script")).get("text")
        ),
        "script_asset_id": _text(content.get("script_asset_id")),
        "evidence_flags": [
            _text(value)
            for value in _sequence(raw.get("evidence_flags"))
            if _text(value)
        ],
    }


def _winner_prompt_asset(
    winner_ref: str,
    asset: Mapping[str, Any],
) -> dict[str, Any]:
    """Return only winner content covered by the approved Gemini transfer."""

    return {
        "winner_ref": winner_ref,
        "published_hook": _text(asset.get("published_hook")),
        "source_selection_hook": _text(asset.get("source_selection_hook")),
        "source_transcript": _text(asset.get("source_transcript")),
    }


def build_winner_context(
    winner_library: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    metadata = _mapping(winner_library.get("library_metadata"))
    if metadata.get("schema_version") != 1:
        raise ValueError(
            f"Unsupported winner library schema: {metadata.get('schema_version')!r}"
        )
    if _text(metadata.get("maturity_window")) != "24h":
        raise ValueError("LLM candidate review requires a fixed 24h winner library")

    assets: dict[str, dict[str, Any]] = {}
    leaderboards: dict[str, list[dict[str, Any]]] = {
        category: [] for category in CATEGORIES
    }
    for raw_value in _sequence(winner_library.get("winners")):
        raw = _mapping(raw_value)
        identity = _mapping(raw.get("identity"))
        media_id = _text(identity.get("media_id"))
        if not media_id:
            continue
        winner_ref = _opaque_ref("winner", media_id)
        assets[winner_ref] = _winner_asset(raw)
        evidence = _mapping(raw.get("winner_evidence"))
        for membership_value in _sequence(evidence.get("ranking_memberships")):
            membership = dict(_mapping(membership_value))
            category = _text(membership.get("leaderboard"))
            if category not in leaderboards:
                continue
            leaderboards[category].append(
                {
                    "winner_ref": winner_ref,
                    "media_id": media_id,
                    "rank": membership.get("rank"),
                    "value": membership.get("value"),
                    "direction": membership.get("direction"),
                    "directional_percentile": membership.get(
                        "directional_percentile"
                    ),
                    "cohort_size": membership.get("cohort_size"),
                    "actual_age_hours": membership.get("actual_age_hours"),
                    "supporting_metrics": dict(
                        _mapping(membership.get("supporting_metrics"))
                    ),
                }
            )
        aggregate = _mapping(evidence.get("aggregate"))
        aggregate_rank = aggregate.get("rank")
        if isinstance(aggregate_rank, (int, float)) and aggregate_rank <= 10:
            leaderboards["aggregate_top_10"].append(
                {
                    "winner_ref": winner_ref,
                    "media_id": media_id,
                    "rank": aggregate_rank,
                    "value": aggregate.get("average_directional_percentile"),
                    "direction": "higher",
                    "directional_percentile": aggregate.get(
                        "average_directional_percentile"
                    ),
                    "cohort_size": aggregate.get("cohort_size"),
                    "actual_age_hours": aggregate.get("actual_age_hours"),
                    "supporting_metrics": {
                        "components": dict(
                            _mapping(aggregate.get("components"))
                        )
                    },
                }
            )

    evidence_index: dict[str, dict[str, Any]] = {}
    blind_memberships: dict[str, list[str]] = {}
    for category, rows in leaderboards.items():
        rows.sort(
            key=lambda row: (
                row.get("rank")
                if isinstance(row.get("rank"), (int, float))
                else 999,
                row["media_id"],
            )
        )
        if len(rows) != 10:
            raise ValueError(
                f"Expected 10 fixed-24h entries for {category}, found {len(rows)}"
            )
        evidence_index[category] = {
            row["winner_ref"]: row for row in rows
        }
        # Stable ID ordering hides rank during the semantic pass.
        blind_memberships[category] = sorted(
            row["winner_ref"] for row in rows
        )

    blind_pack = {
        "evidence_boundary": (
            "All assets are members of at least one measured fixed-24h Top 10. "
            "Ranks and metric values are intentionally hidden for this semantic "
            "pass. Membership does not prove the content mechanism caused the result."
        ),
        "maturity_window": "24h",
        "categories": CATEGORY_DETAILS,
        "leaderboard_memberships_without_rank": blind_memberships,
        "winner_assets": {
            winner_ref: _winner_prompt_asset(
                winner_ref,
                assets[winner_ref],
            )
            for winner_ref in sorted(assets)
        },
    }
    return blind_pack, assets, evidence_index


def candidate_projection(
    candidate: Mapping[str, Any],
    *,
    origin: str,
) -> dict[str, Any]:
    source = _mapping(candidate.get("source"))
    projection = {
        "candidate_id": _text(candidate.get("candidate_id")),
        "candidate_origin": origin,
        "hook": _text(candidate.get("hook")),
        "transcript": _text(candidate.get("transcript")),
        "source_chapter": _text(candidate.get("source_chapter")),
        "start_seconds": candidate.get("start_seconds"),
        "end_seconds": candidate.get("end_seconds"),
        "duration_seconds": candidate.get("duration_seconds"),
        "source": {
            "video_id": _text(source.get("video_id")),
            "title": _text(source.get("title")),
            "uploader": _text(source.get("uploader")),
            "url": _text(source.get("url")),
        },
    }
    source_selection_hook = _text(candidate.get("source_selection_hook"))
    if source_selection_hook:
        projection["source_selection_hook"] = source_selection_hook
    schedule = _mapping(candidate.get("schedule"))
    if schedule:
        projection["schedule"] = dict(schedule)
    return projection


def candidate_prompt_projection(
    candidate: Mapping[str, Any],
    *,
    origin: str,
) -> dict[str, Any]:
    """Return only the primary hook/transcript and an opaque local join reference."""

    candidate_id = _text(candidate.get("candidate_id"))
    return {
        "candidate_id": _opaque_ref("candidate", candidate_id),
        "candidate_origin": origin,
        "hook": _text(candidate.get("hook")),
        "transcript": _text(candidate.get("transcript")),
    }


def _pass_a_instructions() -> str:
    return """\
Role: You are an editorial research analyst evaluating short-form AI news clips.

Goal: Read the candidate's actual hook and transcript, then compare its content
mechanism with every fixed-24h Top-10 category in the supplied winner library.

Success criteria:
- analyze meaning, audience promise, tension, proof, payoff, and delivery structure
- check every material hook claim against the transcript
- return exactly one category_comparison for each of the six supplied categories
- choose zero to three genuine analogues per category from that category's IDs
- quote short exact excerpts from the supplied candidate and winner text
- explain material differences in promise, proof, payoff, and delivery

Constraints:
- Only the actual primary candidate hook is supplied. Evaluate that hook as
  written; do not invent, request, or judge alternate hook variants.
- Winner evidence contains the published hook and, where available, the
  selected source hook. It does not contain unselected hook variants.
- This is semantic analysis, not keyword, entity, or company-name matching.
- Topic/entity overlap alone is SURFACE_ONLY and cannot support advancement.
- Do not infer facts absent from the transcript.
- Do not force an analogue; an empty analogue list is valid.
- You cannot see ranks or metric values. Do not guess them.
- Candidate and winner identity/source metadata and candidate duration are
  intentionally withheld. Do not infer them.
- Do not predict views, ranking, conversion, or a probability of success.
- The aggregate category is correlated summary evidence, not an independent vote.
- Existing candidate-generator scores and prior reasons are intentionally absent.

Relationship meanings:
- CLOSE_MECHANISM: same audience promise, hook mechanism, payoff form, and broadly comparable delivery
- PARTIAL_MECHANISM: at least two meaningful structural similarities with important divergence
- SURFACE_ONLY: shared topic, entity, speaker, or vocabulary without the same promise/payoff
- CONTRAST: useful counterexample, not supporting evidence
"""


def _pass_b_instructions() -> str:
    return """\
Role: You are the second-pass verifier for an editorial Reel decision.

Goal: Challenge the blind semantic review using exact fixed-24h evidence for
only the analogues selected before ranks and values were visible, then make one
bounded editorial recommendation.

Success criteria:
- return exactly one category_interpretation for each of the six categories
- use only analogue IDs selected for that category in the blind pass
- distinguish transferable content mechanisms from mere topic/entity overlap
- audit claim support, citations, duration confounding, and surface-match risk
- state both the case for and the important differences from measured winners
- make a decision and a concrete revision/test plan when applicable

Decision rules:
- ADVANCE: central claims are supported, payoff is delivered, and evidence spans
  both independent signal families or multiple distinct-source close analogues
- ADVANCE_AS_TRIAL: the idea is strong and supported, but evidence is thin,
  single-family, novel, or materially different in duration/delivery
- REVISE: valuable material exists, but the hook overclaims, context arrives too
  late, payoff is buried, or a specific structural fix is required
- REJECT: no distinctive supported payoff, the central hook cannot be repaired
  without invention, or the segment is redundant without a new hypothesis
- MANUAL_REVIEW: input is incomplete, attribution is ambiguous, or evidence conflicts

Constraints:
- Do not add new analogue IDs after metrics are revealed.
- Exact metrics are observational evidence, not causal proof or prediction.
- ATTENTION_REPLAY contains watch depth, 3s skip, and views/reached.
- INTENT_ACTION contains interactions/reach and saves/reach.
- Aggregate Top 10 is not a third family or a sixth vote.
- Confidence means confidence in editorial interpretation only.
- Do not invent a candidate metric, expected lift, or winning probability.
"""


def _false_negative_instructions() -> str:
    return """\
Role: You are independently screening discriminator rejections for editorial
false negatives.

Goal: Read every rejected hook and transcript without seeing its previous score
or rejection reason. Decide whether it deserves a full semantic comparison
against the measured Top-10 library.

Success criteria:
- return one review for every supplied candidate ID, with no omissions or extras
- assess claim support, distinctive payoff, and plausible category mechanisms
- flag only genuinely promising rows; do not promote anything automatically
- if at least five rows are LIKELY_FALSE_NEGATIVE or POSSIBLE_FALSE_NEGATIVE,
  assign TOP_5 to exactly the strongest five; otherwise assign TOP_5 to every
  such row
- give TOP_5 rows unique deep_review_rank values starting at 1; all other rows
  must use null for deep_review_rank

Constraints:
- Use semantic reading, not word/entity overlap.
- Do not guess ranks, metrics, or future performance.
- A generic thesis without a concrete supported payoff can remain rejected.
- A statistic, survey result, company name, or AI topic is not enough by itself.
- LIKELY_FALSE_NEGATIVE means the rejection probably discarded a distinctive,
  transcript-supported candidate that could survive full review without
  rewriting its central claim.
- POSSIBLE_FALSE_NEGATIVE means a targeted rewrite or fuller comparison may
  rescue it, but uncertainty remains.
- REJECTION_SUPPORTED means the supplied segment lacks enough distinctive,
  supported payoff for a full review.
- MANUAL_REVIEW means the text is incomplete or attribution is ambiguous.
"""


def validate_false_negative_screen(
    value: FalseNegativeScreenBatch,
    *,
    expected_ids: Sequence[str],
) -> list[str]:
    actual = [row.candidate_id for row in value.reviews]
    errors: list[str] = []
    if len(actual) != len(set(actual)):
        errors.append("false-negative screen returned duplicate candidate IDs")
    if set(actual) != set(expected_ids) or len(actual) != len(expected_ids):
        errors.append(
            "false-negative screen must return every input ID exactly once"
        )
    promising = [
        row
        for row in value.reviews
        if row.verdict
        in {
            "LIKELY_FALSE_NEGATIVE",
            "POSSIBLE_FALSE_NEGATIVE",
        }
    ]
    top_rows = [
        row
        for row in value.reviews
        if row.deep_review_priority == "TOP_5"
    ]
    target_count = min(5, len(promising))
    if len(top_rows) != target_count:
        errors.append(
            "false-negative screen must assign TOP_5 to exactly "
            f"{target_count} promising rows"
        )
    if any(
        row.verdict
        not in {
            "LIKELY_FALSE_NEGATIVE",
            "POSSIBLE_FALSE_NEGATIVE",
        }
        and row.deep_review_priority != "NO_DEEP_REVIEW"
        for row in value.reviews
    ):
        errors.append(
            "REJECTION_SUPPORTED and MANUAL_REVIEW rows must use "
            "NO_DEEP_REVIEW"
        )
    if any(
        row.deep_review_priority == "TOP_5"
        and row.deep_review_rank is None
        for row in value.reviews
    ):
        errors.append("every TOP_5 row must have a deep_review_rank")
    if any(
        row.deep_review_priority != "TOP_5"
        and row.deep_review_rank is not None
        for row in value.reviews
    ):
        errors.append("only TOP_5 rows may have a deep_review_rank")
    ranks = sorted(
        row.deep_review_rank
        for row in top_rows
        if row.deep_review_rank is not None
    )
    if ranks != list(range(1, target_count + 1)):
        errors.append(
            "TOP_5 deep_review_rank values must be unique and contiguous "
            "from 1"
        )
    return errors


def _category_set(rows: Sequence[Any], field: str) -> tuple[set[str], list[str]]:
    values = [
        _text(
            getattr(row, field, None)
            if isinstance(row, BaseModel)
            else _mapping(row).get(field)
        )
        for row in rows
    ]
    return set(values), values


def validate_blind_review(
    review: BlindSemanticReview,
    *,
    candidate_id: str,
    evidence_index: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    if review.candidate_id != candidate_id:
        errors.append(
            f"candidate_id must be {candidate_id!r}, got {review.candidate_id!r}"
        )
    categories, ordered = _category_set(
        review.category_comparisons,
        "category",
    )
    if categories != set(CATEGORIES) or len(ordered) != len(CATEGORIES):
        errors.append("category_comparisons must contain each category exactly once")
    for comparison in review.category_comparisons:
        allowed = evidence_index.get(comparison.category, {})
        seen: set[str] = set()
        for analogue in comparison.analogues:
            if analogue.media_id in seen:
                errors.append(
                    f"duplicate {analogue.media_id} in {comparison.category}"
                )
            seen.add(analogue.media_id)
            if analogue.media_id not in allowed:
                errors.append(
                    f"{analogue.media_id} is not in {comparison.category} Top 10"
                )
    return errors


def validate_verified_review(
    review: VerifiedCandidateReview,
    *,
    candidate_id: str,
    blind_review: BlindSemanticReview,
) -> list[str]:
    errors: list[str] = []
    if review.candidate_id != candidate_id:
        errors.append(
            f"candidate_id must be {candidate_id!r}, got {review.candidate_id!r}"
        )
    categories, ordered = _category_set(
        review.category_interpretations,
        "category",
    )
    if categories != set(CATEGORIES) or len(ordered) != len(CATEGORIES):
        errors.append(
            "category_interpretations must contain each category exactly once"
        )
    allowed_by_category = {
        row.category: {analogue.media_id for analogue in row.analogues}
        for row in blind_review.category_comparisons
    }
    for row in review.category_interpretations:
        allowed = allowed_by_category.get(row.category, set())
        if not set(row.supporting_analogue_ids).issubset(allowed):
            errors.append(
                f"{row.category} verifier added an ID not selected blind"
            )
    if (
        blind_review.claim_support.overall_status == "UNSUPPORTED"
        and review.decision.label in {"ADVANCE", "ADVANCE_AS_TRIAL"}
    ):
        errors.append("unsupported central claims cannot receive an advance decision")
    return errors


def _normalized_excerpt(value: str) -> str:
    return " ".join(
        re.sub(r"[^\w]+", " ", value.casefold()).split()
    )


def _excerpt_is_present(excerpt: str, corpus: str) -> bool:
    needle = _normalized_excerpt(excerpt)
    haystack = _normalized_excerpt(corpus)
    return bool(needle) and needle in haystack


def _candidate_corpus(candidate: Mapping[str, Any]) -> str:
    return " ".join(
        [
            _text(candidate.get("hook")),
            _text(candidate.get("transcript")),
            _text(candidate.get("source_chapter")),
        ]
    )


def _winner_corpus(asset: Mapping[str, Any]) -> str:
    return " ".join(
        [
            _text(asset.get("published_hook")),
            _text(asset.get("source_selection_hook")),
            _text(asset.get("source_transcript")),
            _text(asset.get("japanese_script")),
        ]
    )


def _humanize_model_narrative(
    value: Any,
    *,
    assets: Mapping[str, Mapping[str, Any]],
    candidate_ref: str | None = None,
    field_name: str | None = None,
) -> Any:
    """Replace opaque refs in prose while preserving machine ID fields."""

    identifier_fields = {
        "candidate_id",
        "llm_candidate_ref",
        "media_id",
        "winner_ref",
        "supporting_analogue_ids",
    }
    if isinstance(value, Mapping):
        return {
            key: _humanize_model_narrative(
                child,
                assets=assets,
                candidate_ref=candidate_ref,
                field_name=str(key),
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _humanize_model_narrative(
                child,
                assets=assets,
                candidate_ref=candidate_ref,
                field_name=field_name,
            )
            for child in value
        ]
    if not isinstance(value, str) or field_name in identifier_fields:
        return value
    text = value
    if candidate_ref:
        text = re.sub(
            re.escape(candidate_ref),
            "this candidate",
            text,
            flags=re.IGNORECASE,
        )
    for winner_ref, asset in assets.items():
        if not re.search(
            re.escape(winner_ref),
            text,
            flags=re.IGNORECASE,
        ):
            continue
        hook = _text(asset.get("published_hook")) or "linked winner"
        text = re.sub(
            re.escape(winner_ref),
            lambda _: f"“{hook}”",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _selected_evidence_pack(
    blind_review: BlindSemanticReview,
    *,
    assets: Mapping[str, Mapping[str, Any]],
    evidence_index: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    categories: dict[str, Any] = {}
    for comparison in blind_review.category_comparisons:
        selected: list[dict[str, Any]] = []
        for analogue in comparison.analogues:
            evidence = dict(
                _mapping(
                    evidence_index.get(comparison.category, {}).get(
                        analogue.media_id
                    )
                )
            )
            safe_evidence = {
                key: value
                for key, value in evidence.items()
                if key != "media_id"
            }
            selected.append(
                {
                    "winner_ref": analogue.media_id,
                    "blind_relation": analogue.relation,
                    "winner": _winner_prompt_asset(
                        analogue.media_id,
                        _mapping(assets.get(analogue.media_id)),
                    ),
                    "exact_24h_evidence": safe_evidence,
                }
            )
        categories[comparison.category] = {
            "definition": CATEGORY_DETAILS[comparison.category],
            "selected_before_metrics": selected,
        }
    return {
        "maturity_window": "24h",
        "evidence_boundary": (
            "Only preselected analogue evidence is shown. Values are observed "
            "near 24 hours and do not establish causality or predict a candidate."
        ),
        "categories": categories,
    }


def _join_exact_evidence(
    blind_review: BlindSemanticReview,
    verified_review: VerifiedCandidateReview,
    *,
    candidate: Mapping[str, Any],
    assets: Mapping[str, Mapping[str, Any]],
    evidence_index: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    joined: list[dict[str, Any]] = []
    citation_warnings: list[str] = []
    candidate_corpus = _candidate_corpus(candidate)
    verifier_by_category = {
        row.category: row for row in verified_review.category_interpretations
    }
    for comparison in blind_review.category_comparisons:
        analogues: list[dict[str, Any]] = []
        for analogue in comparison.analogues:
            asset = dict(_mapping(assets.get(analogue.media_id)))
            evidence = dict(
                _mapping(
                    evidence_index.get(comparison.category, {}).get(
                        analogue.media_id
                    )
                )
            )
            candidate_citation_verified = _excerpt_is_present(
                analogue.candidate_evidence_excerpt,
                candidate_corpus,
            )
            winner_citation_verified = _excerpt_is_present(
                analogue.winner_evidence_excerpt,
                _winner_corpus(asset),
            )
            published_hook = _text(asset.get("published_hook"))
            source_media_id = _text(asset.get("media_id"))
            winner_label = (
                f"“{published_hook}”"
                if published_hook
                else source_media_id or "selected winner"
            )
            if source_media_id:
                winner_label += f" ({source_media_id})"
            if not candidate_citation_verified:
                citation_warnings.append(
                    f"{comparison.category}/{winner_label}: "
                    "candidate excerpt was not verbatim"
                )
            if not winner_citation_verified:
                citation_warnings.append(
                    f"{comparison.category}/{winner_label}: "
                    "winner excerpt was not verbatim"
                )
            duration = asset.get("duration_seconds")
            candidate_duration = candidate.get("duration_seconds")
            candidate_source_video_id = _text(
                _mapping(candidate.get("source")).get("video_id")
            )
            winner_source_video_id = _text(
                _mapping(asset.get("source")).get("video_id")
            )
            duration_delta = (
                abs(float(candidate_duration) - float(duration))
                if isinstance(candidate_duration, (int, float))
                and isinstance(duration, (int, float))
                else None
            )
            analogues.append(
                _humanize_model_narrative(
                    {
                    **analogue.model_dump(mode="json"),
                    "winner_ref": analogue.media_id,
                    "media_id": asset.get("media_id"),
                    "candidate_citation_verified": candidate_citation_verified,
                    "winner_citation_verified": winner_citation_verified,
                    "permalink": asset.get("permalink"),
                    "published_hook": asset.get("published_hook"),
                    "source": asset.get("source"),
                    "same_source_as_candidate": bool(
                        candidate_source_video_id
                        and winner_source_video_id
                        and candidate_source_video_id
                        == winner_source_video_id
                    ),
                    "winner_duration_seconds": duration,
                    "candidate_duration_seconds": candidate_duration,
                    "duration_delta_seconds": duration_delta,
                    "rank": evidence.get("rank"),
                    "metric_value": evidence.get("value"),
                    "metric_value_display": _format_metric(
                        comparison.category,
                        evidence.get("value"),
                    ),
                    "direction": evidence.get("direction"),
                    "directional_percentile": evidence.get(
                        "directional_percentile"
                    ),
                    "cohort_size": evidence.get("cohort_size"),
                    "actual_age_hours": evidence.get("actual_age_hours"),
                    "supporting_metrics": evidence.get("supporting_metrics"),
                    "evidence_flags": asset.get("evidence_flags"),
                    },
                    assets=assets,
                )
            )
        verifier = verifier_by_category.get(comparison.category)
        joined.append(
            {
                "category": comparison.category,
                "label": CATEGORY_DETAILS[comparison.category]["label"],
                "signal_family": CATEGORY_DETAILS[comparison.category][
                    "signal_family"
                ],
                "semantic_fit_before_metrics": comparison.fit_hypothesis,
                "candidate_case": comparison.candidate_case,
                "counterevidence": comparison.counterevidence,
                "uncertainty": comparison.uncertainty,
                "analogues": analogues,
                "evidence_interpretation": (
                    _humanize_model_narrative(
                        verifier.model_dump(mode="json"),
                        assets=assets,
                    )
                    if verifier
                    else None
                ),
            }
        )
    return joined, citation_warnings


def _citation_coverage(
    joined_categories: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    analogues = [
        _mapping(analogue)
        for comparison in joined_categories
        for analogue in _sequence(_mapping(comparison).get("analogues"))
    ]
    total_checks = len(analogues) * 2
    candidate_verified = sum(
        analogue.get("candidate_citation_verified") is True
        for analogue in analogues
    )
    winner_verified = sum(
        analogue.get("winner_citation_verified") is True
        for analogue in analogues
    )
    verified_checks = candidate_verified + winner_verified
    fully_verified = sum(
        analogue.get("candidate_citation_verified") is True
        and analogue.get("winner_citation_verified") is True
        for analogue in analogues
    )
    return {
        "selected_analogues": len(analogues),
        "excerpt_checks": total_checks,
        "verified_excerpt_checks": verified_checks,
        "verified_excerpt_check_rate": (
            verified_checks / total_checks if total_checks else None
        ),
        "fully_verified_analogue_pairs": fully_verified,
        "all_excerpts_verified": bool(total_checks)
        and verified_checks == total_checks,
    }


def _analogue_independence(
    joined_categories: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    placements = [
        {
            "signal_family": _text(
                _mapping(comparison).get("signal_family")
            ),
            **dict(_mapping(analogue)),
        }
        for comparison in joined_categories
        for analogue in _sequence(_mapping(comparison).get("analogues"))
    ]
    unique_media = {
        _text(row.get("media_id"))
        for row in placements
        if _text(row.get("media_id"))
    }
    unique_sources = {
        _text(_mapping(row.get("source")).get("video_id"))
        for row in placements
        if _text(_mapping(row.get("source")).get("video_id"))
    }
    same_source_media = {
        _text(row.get("media_id"))
        for row in placements
        if row.get("same_source_as_candidate") is True
        and _text(row.get("media_id"))
    }
    independent_media = unique_media - same_source_media
    family_rows: dict[str, Any] = {}
    same_source_only_families: list[str] = []
    for family in ("ATTENTION_REPLAY", "INTENT_ACTION"):
        rows = [
            row
            for row in placements
            if row["signal_family"] == family
        ]
        family_media = {
            _text(row.get("media_id"))
            for row in rows
            if _text(row.get("media_id"))
        }
        independent_family_media = {
            _text(row.get("media_id"))
            for row in rows
            if row.get("same_source_as_candidate") is not True
            and _text(row.get("media_id"))
        }
        same_source_only = bool(family_media) and not independent_family_media
        if same_source_only:
            same_source_only_families.append(family)
        family_rows[family] = {
            "unique_analogue_posts": len(family_media),
            "independent_source_analogue_posts": len(
                independent_family_media
            ),
            "same_source_only": same_source_only,
        }
    warning = None
    if same_source_only_families:
        warning = (
            ", ".join(same_source_only_families)
            + " support comes only from a published sibling from the same "
            "source video; treat it as source-specific evidence, not "
            "repeatability."
        )
    elif same_source_media:
        warning = (
            f"{len(same_source_media)} selected analogue post(s) came from the "
            "same source video, but independent-source analogues are also "
            "present."
        )
    return {
        "unique_analogue_posts": len(unique_media),
        "unique_source_videos": len(unique_sources),
        "same_source_analogue_posts": len(same_source_media),
        "independent_source_analogue_posts": len(independent_media),
        "signal_families": family_rows,
        "same_source_only_signal_families": same_source_only_families,
        "warning": warning,
    }


def _adjust_confidence_for_citations(
    model_confidence: str,
    citation_coverage: Mapping[str, Any],
) -> tuple[str, str | None]:
    total = citation_coverage.get("excerpt_checks")
    verified = citation_coverage.get("verified_excerpt_checks")
    if not isinstance(total, int) or total <= 0:
        return "LOW", (
            "No selected analogue citation pair was available for deterministic "
            "verification."
        )
    if not isinstance(verified, int) or verified < total / 2:
        return "LOW", (
            f"Only {verified or 0}/{total} supplied excerpts matched the local "
            "source text; confidence was capped at LOW."
        )
    if verified < total and model_confidence == "HIGH":
        return "MEDIUM", (
            f"{verified}/{total} supplied excerpts matched the local source "
            "text; confidence was capped at MEDIUM."
        )
    return model_confidence, (
        None
        if verified == total
        else (
            f"{verified}/{total} supplied excerpts matched the local source "
            "text; the model confidence was already conservative."
        )
    )


def _sanitize_metric_language(value: Any) -> str:
    text = _text(value)
    text = re.sub(
        r";?\s*duration_caveat\\?\"?\s*:\s*null\s*,?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bwill maximize skip rate and watch depth potential\b",
        "is intended to reduce 3-second skip rate while preserving watch depth",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop[- ]decile performance\b",
        "fixed-24h Top-10 membership",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop[- ]decile\b",
        "fixed-24h Top-10",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-performing analogues\b",
        "fixed-24h Top-10 analogues",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-tier (?:rewatch )?analogues\b",
        "fixed-24h Top-10 analogues",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-tier repeat view performance in close analogue\b",
        "Top-10 repeat-view evidence in a close analogue",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-tier save analogues\b",
        "Top-10 save analogues",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-tier replay depth\b",
        "replay depth",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-tier performance\b",
        "Top-10 analogue evidence",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-performing developer reels\b",
        "Top-10 developer Reel analogues",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-performing AI agent reels\b",
        "Top-10 AI-agent Reel analogues",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bhigh-performing analogues\b",
        "fixed-24h Top-10 analogues",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\btop-performing balanced assets\b",
        "aggregate Top-10 assets",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bwill maximize skip rate\b",
        "is intended to reduce 3-second skip rate",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bdirectly solves the issue\b",
        "is the targeted revision to test",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _bound_hypothesis_language(value: Any) -> str:
    text = _text(value)
    text = re.sub(
        r"\bwill\s+significantly\b",
        "may",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\bwill\b",
        "may",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _sanitize_report_narrative(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _sanitize_report_narrative(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_report_narrative(child) for child in value]
    if isinstance(value, str):
        return _sanitize_metric_language(value)
    return value


def _normalize_test_hypothesis(
    value: Any,
) -> tuple[str | None, str | None]:
    text = _text(value)
    if not text:
        return None, None
    normalized = _bound_hypothesis_language(text)
    if not normalized.casefold().startswith("test"):
        normalized = "Test whether " + normalized[0].lower() + normalized[1:]
    design_warning = None
    if re.search(
        r"\b(alongside|together with|as well as)\b|\s\+\s",
        normalized,
        flags=re.IGNORECASE,
    ):
        design_warning = (
            "This hypothesis changes multiple elements; isolate one declared "
            "variable before making a causal interpretation."
        )
    return normalized, design_warning


def _run_with_validation(
    runner: LLMRunner,
    *,
    schema: type[StrictModel],
    instructions: str,
    base_input: list[dict[str, Any]],
    validator: Any,
    max_output_tokens: int,
) -> tuple[StrictModel, list[dict[str, Any]]]:
    traces: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    for attempt in range(1, 3):
        input_payload = list(base_input)
        if validation_errors:
            input_payload.append(
                {
                    "role": "user",
                    "content": (
                        "Your prior structured response failed these relational "
                        "checks. Return a corrected complete response:\n- "
                        + "\n- ".join(validation_errors)
                    ),
                }
            )
        parsed, trace = runner.run(
            schema=schema,
            instructions=instructions,
            input_payload=input_payload,
            max_output_tokens=max_output_tokens,
        )
        trace = {**trace, "attempt": attempt}
        traces.append(trace)
        validation_errors = validator(parsed)
        if not validation_errors:
            return parsed, traces
    raise ValueError(
        "LLM structured output failed relational validation: "
        + "; ".join(validation_errors)
    )


def evaluate_candidate_with_llm(
    candidate: Mapping[str, Any],
    *,
    origin: str,
    runner: LLMRunner,
    blind_pack: Mapping[str, Any],
    assets: Mapping[str, Mapping[str, Any]],
    evidence_index: Mapping[str, Mapping[str, Any]],
    screen_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    projection = candidate_projection(candidate, origin=origin)
    prompt_projection = candidate_prompt_projection(candidate, origin=origin)
    candidate_id = prompt_projection["candidate_id"]
    pass_a_input = [
        {
            "role": "developer",
            "content": "BLIND WINNER LIBRARY:\n" + _json(blind_pack),
        },
        {
            "role": "user",
            "content": "CANDIDATE TO REVIEW:\n" + _json(prompt_projection),
        },
    ]
    blind_model, pass_a_traces = _run_with_validation(
        runner,
        schema=BlindSemanticReview,
        instructions=_pass_a_instructions(),
        base_input=pass_a_input,
        validator=lambda value: validate_blind_review(
            value,
            candidate_id=candidate_id,
            evidence_index=evidence_index,
        ),
        max_output_tokens=40000,
    )
    assert isinstance(blind_model, BlindSemanticReview)

    candidate_corpus = _candidate_corpus(candidate)
    citation_audit: list[dict[str, Any]] = []
    for comparison in blind_model.category_comparisons:
        for analogue in comparison.analogues:
            asset = _mapping(assets.get(analogue.media_id))
            citation_audit.append(
                {
                    "category": comparison.category,
                    "media_id": analogue.media_id,
                    "candidate_excerpt_verified": _excerpt_is_present(
                        analogue.candidate_evidence_excerpt,
                        candidate_corpus,
                    ),
                    "winner_excerpt_verified": _excerpt_is_present(
                        analogue.winner_evidence_excerpt,
                        _winner_corpus(asset),
                    ),
                }
            )

    evidence_pack = _selected_evidence_pack(
        blind_model,
        assets=assets,
        evidence_index=evidence_index,
    )
    pass_b_payload = {
        "candidate": prompt_projection,
        "blind_semantic_review": blind_model.model_dump(mode="json"),
        "programmatic_citation_audit": citation_audit,
        "exact_evidence_for_preselected_analogues": evidence_pack,
        "false_negative_screen_context": {
            key: value
            for key, value in _mapping(screen_context).items()
            if key
            in {
                "verdict",
                "claim_support_status",
                "distinctive_payoff_present",
                "strongest_category_hypotheses",
                "reason",
                "required_revision",
            }
        },
    }
    pass_b_input = [
        {
            "role": "user",
            "content": "VERIFY THIS LOCKED BLIND REVIEW:\n" + _json(pass_b_payload),
        }
    ]
    verified_model, pass_b_traces = _run_with_validation(
        runner,
        schema=VerifiedCandidateReview,
        instructions=_pass_b_instructions(),
        base_input=pass_b_input,
        validator=lambda value: validate_verified_review(
            value,
            candidate_id=candidate_id,
            blind_review=blind_model,
        ),
        max_output_tokens=24000,
    )
    assert isinstance(verified_model, VerifiedCandidateReview)

    joined_categories, citation_warnings = _join_exact_evidence(
        blind_model,
        verified_model,
        candidate=candidate,
        assets=assets,
        evidence_index=evidence_index,
    )
    joined_categories = _sanitize_report_narrative(joined_categories)
    semantic_profile = _sanitize_report_narrative(
        _humanize_model_narrative(
            blind_model.semantic_profile.model_dump(mode="json"),
            assets=assets,
            candidate_ref=candidate_id,
        )
    )
    semantic_profile["attention_hypothesis"] = _bound_hypothesis_language(
        semantic_profile.get("attention_hypothesis")
    )
    semantic_profile["action_hypothesis"] = _bound_hypothesis_language(
        semantic_profile.get("action_hypothesis")
    )
    claim_support = _sanitize_report_narrative(
        _humanize_model_narrative(
            blind_model.claim_support.model_dump(mode="json"),
            assets=assets,
            candidate_ref=candidate_id,
        )
    )
    verifier_audit = _sanitize_report_narrative(
        _humanize_model_narrative(
            verified_model.verifier_audit.model_dump(mode="json"),
            assets=assets,
            candidate_ref=candidate_id,
        )
    )
    synthesis = _sanitize_report_narrative(
        _humanize_model_narrative(
            verified_model.cross_category_synthesis.model_dump(mode="json"),
            assets=assets,
            candidate_ref=candidate_id,
        )
    )
    decision = _sanitize_report_narrative(
        _humanize_model_narrative(
            verified_model.decision.model_dump(mode="json"),
            assets=assets,
            candidate_ref=candidate_id,
        )
    )
    citation_coverage = _citation_coverage(joined_categories)
    analogue_independence = _analogue_independence(joined_categories)
    model_confidence = _text(_mapping(decision).get("confidence"))
    adjusted_confidence, confidence_adjustment = (
        _adjust_confidence_for_citations(
            model_confidence,
            citation_coverage,
        )
    )
    normalized_hypothesis, test_design_warning = (
        _normalize_test_hypothesis(
            _mapping(decision).get("test_hypothesis")
        )
    )
    model_reason = _text(_mapping(decision).get("reason"))
    safe_reason = _bound_hypothesis_language(
        _sanitize_metric_language(model_reason)
    )
    narrative_adjustments = []
    if safe_reason != model_reason:
        narrative_adjustments.append(
            "Replaced an unsupported leaderboard superlative with the exact "
            "fixed-24h Top-10 evidence boundary."
        )
    return {
        "candidate": {
            **projection,
            "llm_candidate_ref": candidate_id,
            "slug": _text(candidate.get("slug")),
            "index": candidate.get("index"),
            "source_timestamp_url": _text(
                candidate.get("source_timestamp_url")
            ),
            "generation_pipeline_priors": dict(
                _mapping(candidate.get("selection_scores"))
            ),
        },
        "review_status": "COMPLETE",
        "analysis_method": "TWO_PASS_LLM_SEMANTIC_REVIEW",
        "semantic_profile": semantic_profile,
        "claim_support": claim_support,
        "category_comparisons": joined_categories,
        "source_and_topic_saturation": _sanitize_report_narrative(
            _humanize_model_narrative(
                blind_model.source_and_topic_saturation,
                assets=assets,
                candidate_ref=candidate_id,
            )
        ),
        "blind_review_summary": _sanitize_report_narrative(
            _humanize_model_narrative(
                blind_model.blind_review_summary,
                assets=assets,
                candidate_ref=candidate_id,
            )
        ),
        "verifier_audit": verifier_audit,
        "citation_warnings": citation_warnings,
        "citation_coverage": citation_coverage,
        "analogue_independence": analogue_independence,
        "cross_category_synthesis": synthesis,
        "decision": {
            **decision,
            "reason": safe_reason,
            "model_confidence": model_confidence,
            "confidence": adjusted_confidence,
            "confidence_adjustment": confidence_adjustment,
            "confidence_scope": "editorial interpretation only",
            "test_hypothesis": normalized_hypothesis,
            "test_design_warning": test_design_warning,
            "narrative_adjustments": narrative_adjustments,
            "non_prediction_statement": (
                "Analogue evidence is observational and does not predict "
                "candidate performance."
            ),
        },
        "llm_trace": {
            "pass_a_blind_semantic": pass_a_traces,
            "pass_b_metric_verifier": pass_b_traces,
        },
        "automatic_schedule_change": False,
    }


def _load_discriminator_rejections(
    candidates_path: Path,
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], Path | None]:
    discriminator_path = candidates_path.parent / "work" / "ai_candidate_discriminator.json"
    if not discriminator_path.is_file():
        return [], {}, None
    payload = diagnostic.load_json_object(
        discriminator_path,
        label="AI candidate discriminator JSON",
    )
    metadata_path = candidates_path.parent / "metadata.json"
    metadata = (
        diagnostic.load_json_object(metadata_path, label="Reel source metadata")
        if metadata_path.is_file()
        else {}
    )
    video_id = candidates_path.parent.name
    source_url = _text(metadata.get("webpage_url"))
    rejected: list[dict[str, Any]] = []
    original: dict[str, dict[str, Any]] = {}
    for position, value in enumerate(_sequence(payload.get("judgments")), start=1):
        row = _mapping(value)
        if row.get("keep") is True:
            continue
        candidate = diagnostic.normalize_clip_candidate(
            row,
            position=position,
            video_id=video_id,
            source_url=source_url,
            metadata=metadata,
            config=config,
            origin="discriminator_rejection",
        )
        candidate_id = _text(candidate.get("candidate_id"))
        rejected.append(candidate)
        original[candidate_id] = {
            "prior_discriminator_reason": _text(row.get("reason")),
            "prior_discriminator_scores": {
                "overall": row.get("score"),
                "hook": row.get("hook_score"),
                "value": row.get("value_score"),
                "opening": _mapping(row.get("opening_assessment")).get("score"),
            },
        }
    return rejected, original, discriminator_path


def screen_false_negatives(
    rejected: Sequence[Mapping[str, Any]],
    *,
    runner: LLMRunner,
    blind_pack: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    projections = [
        candidate_prompt_projection(
            candidate,
            origin="DISCRIMINATOR_REJECTION",
        )
        for candidate in rejected
    ]
    base_input = [
        {
            "role": "developer",
            "content": "BLIND WINNER LIBRARY:\n" + _json(blind_pack),
        },
        {
            "role": "user",
            "content": "REJECTED ROWS TO SCREEN:\n" + _json(projections),
        },
    ]
    expected_ids = [row["candidate_id"] for row in projections]

    model, traces = _run_with_validation(
        runner,
        schema=FalseNegativeScreenBatch,
        instructions=_false_negative_instructions(),
        base_input=base_input,
        validator=lambda value: validate_false_negative_screen(
            value,
            expected_ids=expected_ids,
        ),
        max_output_tokens=40000,
    )
    assert isinstance(model, FalseNegativeScreenBatch)
    by_id = {row.candidate_id: row for row in model.reviews}
    return [
        by_id[candidate_id].model_dump(mode="json")
        for candidate_id in expected_ids
    ], traces


def select_false_negative_deep_rows(
    screens: Sequence[Mapping[str, Any]],
    *,
    source_order: Mapping[str, int],
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    flagged = [
        dict(row)
        for row in screens
        if row.get("verdict")
        in {
            "LIKELY_FALSE_NEGATIVE",
            "POSSIBLE_FALSE_NEGATIVE",
        }
    ]
    verdict_priority = {
        "LIKELY_FALSE_NEGATIVE": 0,
        "POSSIBLE_FALSE_NEGATIVE": 1,
    }
    class_priority = {
        "TOP_5": 0,
        "SECONDARY": 1,
        "NO_DEEP_REVIEW": 2,
    }
    flagged.sort(
        key=lambda row: (
            class_priority.get(_text(row.get("deep_review_priority")), 99),
            (
                int(row["deep_review_rank"])
                if isinstance(row.get("deep_review_rank"), int)
                else 99
            ),
            verdict_priority.get(_text(row.get("verdict")), 99),
            source_order.get(_text(row.get("candidate_id")), 999),
        )
    )
    return flagged, flagged[: max(0, limit)]


def _evaluate_many(
    candidates: Sequence[Mapping[str, Any]],
    *,
    origin: str,
    workers: int,
    runner: LLMRunner,
    blind_pack: Mapping[str, Any],
    assets: Mapping[str, Mapping[str, Any]],
    evidence_index: Mapping[str, Mapping[str, Any]],
    screen_context_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not candidates:
        return []
    indexed = list(enumerate(candidates))
    results: dict[int, dict[str, Any]] = {}
    total = len(indexed)

    def report_progress(completed: int) -> None:
        if completed == 1 or completed == total or completed % 10 == 0:
            print(
                "[candidate-evaluator] "
                f"llm_candidates_completed={completed}/{total}",
                flush=True,
            )

    def evaluate(position: int, candidate: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        candidate_id = _text(candidate.get("candidate_id"))
        try:
            result = evaluate_candidate_with_llm(
                candidate,
                origin=origin,
                runner=runner,
                blind_pack=blind_pack,
                assets=assets,
                evidence_index=evidence_index,
                screen_context=(
                    _mapping(screen_context_by_id.get(candidate_id))
                    if screen_context_by_id
                    else None
                ),
            )
        except Exception as exc:  # fail closed; never substitute lexical analysis
            result = {
                "candidate": {
                    **candidate_projection(candidate, origin=origin),
                    "slug": _text(candidate.get("slug")),
                    "index": candidate.get("index"),
                    "source_timestamp_url": _text(
                        candidate.get("source_timestamp_url")
                    ),
                    "generation_pipeline_priors": dict(
                        _mapping(candidate.get("selection_scores"))
                    ),
                },
                "review_status": "API_ERROR",
                "analysis_method": "LLM_REQUIRED_NO_FALLBACK",
                "error": f"{type(exc).__name__}: {exc}",
                "decision": {
                    "label": "MANUAL_REVIEW",
                    "confidence": "LOW",
                    "confidence_scope": "editorial interpretation only",
                    "reason": (
                        "The required LLM review did not complete. No lexical "
                        "or deterministic substitute was used."
                    ),
                    "strong_points": [],
                    "blocking_risks": ["LLM review unavailable"],
                    "must_fix_before_use": ["Rerun the LLM evaluation"],
                    "revision_plan": None,
                    "test_hypothesis": None,
                    "primary_measurement": None,
                    "non_prediction_statement": (
                        "Analogue evidence is observational and does not predict "
                        "candidate performance."
                    ),
                },
                "automatic_schedule_change": False,
            }
        return position, result

    max_workers = max(1, workers)
    if max_workers == 1:
        for position, candidate in indexed:
            _, result = evaluate(position, candidate)
            results[position] = result
            report_progress(len(results))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(evaluate, position, candidate): position
                for position, candidate in indexed
            }
            for future in as_completed(futures):
                position, result = future.result()
                results[position] = result
                report_progress(len(results))
    return [results[position] for position, _ in indexed]


def build_llm_candidate_evaluation(
    candidate_paths: Sequence[Path],
    winner_library: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    winner_library_path: Path | None,
    model: str,
    reasoning_effort: str,
    timeout_seconds: float,
    workers: int,
    cache_dir: Path | None,
    use_cache: bool,
    audit_false_negatives: bool,
    max_false_negative_deep_reviews: int,
    adapter: GeminiResponseAdapter | None = None,
    candidate_slugs: Sequence[str] | None = None,
    normalized_sources: Sequence[Mapping[str, Any]] | None = None,
    candidate_origin: str = "RECONCILED_CANDIDATE",
    input_scope: str = "candidate_files",
) -> dict[str, Any]:
    blind_pack, assets, evidence_index = build_winner_context(winner_library)
    evaluation_context_hash = _sha256_text(
        _json(
            {
                "blind_pack": blind_pack,
                "evidence_index": evidence_index,
            }
        )
    )
    winner_hash = (
        diagnostic.sha256_file(winner_library_path)
        if winner_library_path is not None and winner_library_path.is_file()
        else _sha256_text(_json(winner_library))
    )
    response_adapter = adapter or GeminiResponseAdapter(
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
    )
    cache = RequestCache(cache_dir, enabled=use_cache)
    runner = LLMRunner(
        response_adapter,
        cache=cache,
        prompt_cache_key=(
            f"reel-moneyball-{PROMPT_VERSION}-"
            f"{evaluation_context_hash[:16]}"
        ),
    )

    if normalized_sources is None:
        source_rows = [
            diagnostic.normalize_candidate_source(
                path.expanduser().resolve(),
                config,
            )
            for path in candidate_paths
        ]
    else:
        source_rows = [
            {
                **dict(source),
                "candidates": [
                    dict(_mapping(candidate))
                    for candidate in _sequence(source.get("candidates"))
                    if _mapping(candidate)
                ],
            }
            for source in normalized_sources
        ]
    requested_slugs = {
        _text(slug)
        for slug in _sequence(candidate_slugs)
        if _text(slug)
    }
    if requested_slugs:
        available_slugs = {
            _text(_mapping(candidate).get("slug"))
            for source in source_rows
            for candidate in _sequence(source.get("candidates"))
            if _text(_mapping(candidate).get("slug"))
        }
        missing_slugs = sorted(requested_slugs - available_slugs)
        if missing_slugs:
            raise ValueError(
                "Requested candidate slug(s) not found: "
                + ", ".join(missing_slugs)
            )
        filtered_sources: list[dict[str, Any]] = []
        for source in source_rows:
            original_candidates = [
                dict(_mapping(candidate))
                for candidate in _sequence(source.get("candidates"))
                if _mapping(candidate)
            ]
            selected_candidates = [
                candidate
                for candidate in original_candidates
                if _text(candidate.get("slug")) in requested_slugs
            ]
            filtered_source = dict(source)
            filtered_source["candidates"] = selected_candidates
            filtered_source["candidate_count"] = len(selected_candidates)
            filtered_source["candidate_selection"] = {
                "method": "EXACT_SLUG_ALLOWLIST",
                "requested_slugs": sorted(requested_slugs),
                "source_candidates_before_filter": len(original_candidates),
                "source_candidates_selected": len(selected_candidates),
            }
            if not selected_candidates:
                filtered_source["status"] = "NO_SELECTED_CANDIDATES"
            filtered_sources.append(filtered_source)
        source_rows = filtered_sources
    evaluated_sources: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    total_candidates = 0
    total_api_errors = 0
    total_rejections_screened = 0
    total_false_negative_flags = 0
    total_false_negative_deep = 0

    candidates_by_source: list[list[dict[str, Any]]] = [
        [
            dict(_mapping(value))
            for value in _sequence(source.get("candidates"))
            if _mapping(value)
        ]
        for source in source_rows
    ]
    flattened_candidates = [
        candidate
        for source_candidates in candidates_by_source
        for candidate in source_candidates
    ]
    flattened_evaluations = _evaluate_many(
        flattened_candidates,
        origin=candidate_origin,
        workers=workers,
        runner=runner,
        blind_pack=blind_pack,
        assets=assets,
        evidence_index=evidence_index,
    )
    evaluation_cursor = 0

    for source, source_candidates in zip(
        source_rows,
        candidates_by_source,
        strict=True,
    ):
        next_cursor = evaluation_cursor + len(source_candidates)
        evaluations = flattened_evaluations[
            evaluation_cursor:next_cursor
        ]
        evaluation_cursor = next_cursor
        for evaluation in evaluations:
            decision_counts[
                _text(_mapping(evaluation.get("decision")).get("label"))
                or "UNAVAILABLE"
            ] += 1
            if evaluation.get("review_status") == "API_ERROR":
                total_api_errors += 1
        total_candidates += len(evaluations)
        evaluated_source = {
            key: value
            for key, value in source.items()
            if key != "candidates"
        }
        evaluated_source["evaluations"] = evaluations
        evaluated_source["decision_counts"] = dict(
            Counter(
                _text(_mapping(row.get("decision")).get("label"))
                or "UNAVAILABLE"
                for row in evaluations
            )
        )

        if (
            audit_false_negatives
            and source.get("status") == "NO_RECONCILED_CANDIDATES"
        ):
            rejected, original_by_id, discriminator_path = (
                _load_discriminator_rejections(
                    Path(_text(source.get("candidate_file"))),
                    config,
                )
            )
            if rejected:
                try:
                    screens, screen_traces = screen_false_negatives(
                        rejected,
                        runner=runner,
                        blind_pack=blind_pack,
                    )
                except Exception as exc:  # no lexical false-negative fallback
                    total_api_errors += 1
                    evaluated_source["false_negative_audit"] = {
                        "method": "LLM_REQUIRED_NO_FALLBACK",
                        "status": "API_ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                        "discriminator_file": (
                            str(discriminator_path.resolve())
                            if discriminator_path is not None
                            else None
                        ),
                        "rejected_rows_available": len(rejected),
                        "rejected_rows_screened": 0,
                        "automatic_promotions": 0,
                    }
                    evaluated_sources.append(evaluated_source)
                    continue
                rejected_by_ref = {
                    _opaque_ref(
                        "candidate",
                        _text(candidate.get("candidate_id")),
                    ): candidate
                    for candidate in rejected
                }
                for row in screens:
                    llm_ref = row["candidate_id"]
                    rejected_candidate = _mapping(
                        rejected_by_ref.get(llm_ref)
                    )
                    local_id = _text(
                        rejected_candidate.get("candidate_id")
                    )
                    row["llm_candidate_ref"] = llm_ref
                    row["candidate_id"] = local_id
                    row["hook"] = _text(rejected_candidate.get("hook"))
                    row["source_timestamp_url"] = _text(
                        rejected_candidate.get("source_timestamp_url")
                    )
                    row.update(original_by_id.get(local_id, {}))
                    row["automatic_promotion"] = False
                total_rejections_screened += len(screens)
                source_order = {
                    _text(candidate.get("candidate_id")): index
                    for index, candidate in enumerate(rejected)
                }
                flagged, deep_rows = select_false_negative_deep_rows(
                    screens,
                    source_order=source_order,
                    limit=max_false_negative_deep_reviews,
                )
                total_false_negative_flags += len(flagged)
                candidate_by_id = {
                    _text(candidate.get("candidate_id")): candidate
                    for candidate in rejected
                }
                deep_candidates = [
                    candidate_by_id[row["candidate_id"]]
                    for row in deep_rows
                    if row["candidate_id"] in candidate_by_id
                ]
                screen_by_id = {row["candidate_id"]: row for row in deep_rows}
                deep_evaluations = _evaluate_many(
                    deep_candidates,
                    origin="DISCRIMINATOR_REJECTION",
                    workers=workers,
                    runner=runner,
                    blind_pack=blind_pack,
                    assets=assets,
                    evidence_index=evidence_index,
                    screen_context_by_id=screen_by_id,
                )
                total_false_negative_deep += len(deep_evaluations)
                total_api_errors += sum(
                    row.get("review_status") == "API_ERROR"
                    for row in deep_evaluations
                )
                evaluated_source["false_negative_audit"] = {
                    "method": "LLM_INDEPENDENT_SCREEN_THEN_DEEP_REVIEW",
                    "discriminator_file": (
                        str(discriminator_path.resolve())
                        if discriminator_path is not None
                        else None
                    ),
                    "rejected_rows_screened": len(screens),
                    "screen_verdict_counts": dict(
                        Counter(row["verdict"] for row in screens)
                    ),
                    "screen_priority_counts": dict(
                        Counter(
                            row["deep_review_priority"]
                            for row in screens
                        )
                    ),
                    "flagged_for_deep_review": len(flagged),
                    "deep_review_limit": max_false_negative_deep_reviews,
                    "deep_reviews_completed": len(deep_evaluations),
                    "screen_reviews": screens,
                    "deep_evaluations": deep_evaluations,
                    "llm_trace": {"screen": screen_traces},
                    "automatic_promotions": 0,
                }
            else:
                evaluated_source["false_negative_audit"] = {
                    "method": "LLM_INDEPENDENT_SCREEN_THEN_DEEP_REVIEW",
                    "status": "NO_DISCRIMINATOR_REJECTIONS_AVAILABLE",
                    "rejected_rows_screened": 0,
                    "automatic_promotions": 0,
                }
        evaluated_sources.append(evaluated_source)

    review_queue = _build_review_queue(evaluated_sources)

    metadata = _mapping(winner_library.get("library_metadata"))
    return {
        "report_metadata": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now_iso(),
            "account": _text(metadata.get("account")),
            "platform": _text(metadata.get("platform")),
            "maturity_window": "24h",
            "analysis_mode": "llm_semantic_two_pass",
            "provider": "gemini",
            "provider_endpoint": GEMINI_OPENAI_COMPAT_BASE_URL,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "prompt_version": PROMPT_VERSION,
            "winner_library": (
                str(winner_library_path.resolve())
                if winner_library_path is not None
                else None
            ),
            "winner_library_sha256": winner_hash,
            "evaluation_context_sha256": evaluation_context_hash,
            "winner_post_count": len(assets),
            "leaderboard_count": len(CATEGORIES),
            "candidate_slug_filter": sorted(requested_slugs),
            "input_scope": input_scope,
            "worker_count": workers,
            "cache_enabled": use_cache,
            "cache_directory": (
                str(cache_dir.resolve()) if cache_dir is not None else None
            ),
            "evidence_boundary": (
                "The LLM reads each candidate's primary hook/source transcript "
                "and winner published or selected source hooks/source "
                "transcripts, identified by opaque local references, then "
                "compares content mechanisms. Alternate hook variants are "
                "excluded. It does not predict performance or establish "
                "causality. Exact metrics and links are joined programmatically "
                "from the fixed-24h winner library."
            ),
        },
        "methodology": {
            "pass_a": (
                "Blind semantic comparison against all six Top-10 memberships; "
                "ranks and metric values are hidden."
            ),
            "pass_b": (
                "Verifier sees exact 24-hour evidence only for analogues locked "
                "in Pass A, challenges the match, and assigns the decision."
            ),
            "not_used": [
                "candidate hook variants",
                "winner hook variants",
                "lexical similarity",
                "keyword overlap scoring",
                "opaque combined performance score",
                "candidate-generator scores as LLM inputs",
                "automatic schedule mutation",
            ],
            "signal_families": {
                "ATTENTION_REPLAY": [
                    "watch_depth",
                    "three_second_skip_rate",
                    "views_per_reached_account",
                ],
                "INTENT_ACTION": [
                    "total_interactions_per_reach",
                    "saves_per_1000_reach",
                ],
                "aggregate_top_10": (
                    "correlated summary; never counted as an independent family"
                ),
            },
            "non_determinism": (
                "The model judgment is not mathematically deterministic. A cache "
                "keyed by model, prompt, schema, winner library, and candidate "
                "input makes identical reruns reproducible when enabled."
            ),
        },
        "summary": {
            "candidate_sources": len(evaluated_sources),
            "candidates": total_candidates,
            "empty_sources": sum(
                source.get("status") == "NO_RECONCILED_CANDIDATES"
                for source in evaluated_sources
            ),
            "decision_counts": dict(decision_counts),
            "api_errors": total_api_errors,
            "discriminator_rejections_screened": total_rejections_screened,
            "false_negative_flags": total_false_negative_flags,
            "false_negative_deep_reviews": total_false_negative_deep,
            "automatic_schedule_changes": 0,
        },
        "review_queue": review_queue,
        "sources": evaluated_sources,
    }


def _build_review_queue(
    evaluated_sources: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    review_queue: list[dict[str, Any]] = []
    priority = {name: index for index, name in enumerate(DECISIONS)}
    for source in evaluated_sources:
        for evaluation in _sequence(source.get("evaluations")):
            row = _mapping(evaluation)
            candidate = _mapping(row.get("candidate"))
            decision = _mapping(row.get("decision"))
            schedule = _mapping(candidate.get("schedule"))
            review_queue.append(
                {
                    "candidate_id": _text(candidate.get("candidate_id")),
                    "source_video_id": _text(source.get("video_id")),
                    "hook": _text(candidate.get("hook")),
                    "source_timestamp_url": _text(
                        candidate.get("source_timestamp_url")
                    ),
                    "decision": _text(decision.get("label")),
                    "confidence": _text(decision.get("confidence")),
                    "reason": _text(decision.get("reason")),
                    "review_status": _text(row.get("review_status")),
                    "scheduled_at": _text(schedule.get("scheduled_at"))
                    or None,
                    "current_lane": _text(schedule.get("current_lane"))
                    or None,
                    "content_hash": _text(schedule.get("content_hash"))
                    or None,
                }
            )
    review_queue.sort(
        key=lambda row: (
            priority.get(row["decision"], 99),
            row["candidate_id"],
        )
    )
    for index, row in enumerate(review_queue, start=1):
        row["review_order"] = index
    return review_queue


def merge_llm_candidate_evaluation_reports(
    base_report: Mapping[str, Any],
    patch_reports: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Replace matching fail-closed rows with explicitly sourced retry rows."""

    merged = copy.deepcopy(dict(base_report))
    sources = [
        dict(_mapping(source))
        for source in _sequence(merged.get("sources"))
    ]
    locations: dict[str, tuple[int, int]] = {}
    for source_index, source in enumerate(sources):
        evaluations = [
            dict(_mapping(evaluation))
            for evaluation in _sequence(source.get("evaluations"))
        ]
        source["evaluations"] = evaluations
        for evaluation_index, evaluation in enumerate(evaluations):
            candidate_id = _text(
                _mapping(evaluation.get("candidate")).get("candidate_id")
            )
            if candidate_id:
                locations[candidate_id] = (
                    source_index,
                    evaluation_index,
                )

    base_metadata = _mapping(merged.get("report_metadata"))
    base_effort = _text(base_metadata.get("reasoning_effort"))
    reasoning_exceptions: list[dict[str, Any]] = []
    replacements = 0
    for patch_report in patch_reports:
        patch_metadata = _mapping(patch_report.get("report_metadata"))
        patch_effort = _text(patch_metadata.get("reasoning_effort"))
        for patch_source in _sequence(patch_report.get("sources")):
            for patch_evaluation in _sequence(
                _mapping(patch_source).get("evaluations")
            ):
                replacement = copy.deepcopy(
                    dict(_mapping(patch_evaluation))
                )
                candidate_id = _text(
                    _mapping(replacement.get("candidate")).get(
                        "candidate_id"
                    )
                )
                location = locations.get(candidate_id)
                if location is None:
                    raise ValueError(
                        f"Patch candidate is absent from base report: {candidate_id}"
                    )
                source_index, evaluation_index = location
                if patch_effort and patch_effort != base_effort:
                    replacement["reasoning_effort_used"] = patch_effort
                    replacement["reasoning_effort_exception"] = {
                        "base_report_reasoning_effort": base_effort,
                        "reason": (
                            "Isolated retry after the base reasoning effort "
                            "returned no structured output."
                        ),
                    }
                    reasoning_exceptions.append(
                        {
                            "candidate_id": candidate_id,
                            "reasoning_effort_used": patch_effort,
                            "base_report_reasoning_effort": base_effort,
                        }
                    )
                sources[source_index]["evaluations"][
                    evaluation_index
                ] = replacement
                replacements += 1

    report_assets: dict[str, dict[str, Any]] = {}
    for source in sources:
        for evaluation in _sequence(source.get("evaluations")):
            for comparison in _sequence(
                _mapping(evaluation).get("category_comparisons")
            ):
                for analogue in _sequence(
                    _mapping(comparison).get("analogues")
                ):
                    row = _mapping(analogue)
                    winner_ref = _text(row.get("winner_ref"))
                    if winner_ref:
                        report_assets[winner_ref] = {
                            "published_hook": _text(
                                row.get("published_hook")
                            )
                        }
    for source in sources:
        cleaned_evaluations: list[dict[str, Any]] = []
        for evaluation in _sequence(source.get("evaluations")):
            row = dict(_mapping(evaluation))
            candidate_ref = _text(
                _mapping(row.get("candidate")).get("llm_candidate_ref")
            )
            cleaned = _humanize_model_narrative(
                row,
                assets=report_assets,
                candidate_ref=candidate_ref or None,
            )
            cleaned_evaluations.append(
                dict(_mapping(_sanitize_report_narrative(cleaned)))
            )
        source["evaluations"] = cleaned_evaluations

    for source in sources:
        evaluations = [
            _mapping(evaluation)
            for evaluation in _sequence(source.get("evaluations"))
        ]
        source["decision_counts"] = dict(
            Counter(
                _text(_mapping(row.get("decision")).get("label"))
                or "UNAVAILABLE"
                for row in evaluations
            )
        )

    all_evaluations = [
        _mapping(evaluation)
        for source in sources
        for evaluation in _sequence(source.get("evaluations"))
    ]
    summary = dict(_mapping(merged.get("summary")))
    summary["candidates"] = len(all_evaluations)
    summary["candidate_sources"] = len(sources)
    summary["decision_counts"] = dict(
        Counter(
            _text(_mapping(row.get("decision")).get("label"))
            or "UNAVAILABLE"
            for row in all_evaluations
        )
    )
    summary["api_errors"] = sum(
        row.get("review_status") == "API_ERROR"
        for row in all_evaluations
    )
    metadata = dict(base_metadata)
    metadata["generated_at"] = _now_iso()
    metadata["merged_retry_rows"] = replacements
    metadata["reasoning_effort_exceptions"] = reasoning_exceptions
    merged["report_metadata"] = metadata
    merged["summary"] = summary
    merged["review_queue"] = _build_review_queue(sources)
    merged["sources"] = sources
    return merged


def render_llm_candidate_evaluation_json(report: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )


def _md_escape(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", " ")


def _format_seconds(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unavailable"
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def _analogue_evidence_line(
    category: str,
    analogue: Mapping[str, Any],
) -> str:
    relation = _text(analogue.get("relation"))
    hook = _md_escape(analogue.get("published_hook"))
    permalink = _text(analogue.get("permalink"))
    link = f"[{hook}]({permalink})" if permalink else hook
    return (
        f"{link} — {relation}; rank #{analogue.get('rank')}, "
        f"{_md_escape(analogue.get('metric_value_display'))}; "
        f"age {float(analogue.get('actual_age_hours')):.2f}h"
        if isinstance(analogue.get("actual_age_hours"), (int, float))
        else (
            f"{link} — {relation}; rank #{analogue.get('rank')}, "
            f"{_md_escape(analogue.get('metric_value_display'))}"
        )
    )


def _display_timestamp_jst(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        return raw
    jst = timezone(timedelta(hours=9), name="JST")
    return parsed.astimezone(jst).isoformat()


def render_llm_candidate_evaluation_markdown(
    report: Mapping[str, Any],
) -> str:
    metadata = _mapping(report.get("report_metadata"))
    summary = _mapping(report.get("summary"))
    scheduled_scope = _mapping(report.get("scheduled_scope"))
    lines = [
        "# Reel candidate review — LLM semantic Moneyball comparison",
        "",
        f"- Account: `{_md_escape(metadata.get('account'))}`",
        f"- Generated (JST): "
        f"`{_md_escape(_display_timestamp_jst(metadata.get('generated_at')))}`",
        f"- Provider: `{_md_escape(metadata.get('provider'))}` "
        f"via `{_md_escape(metadata.get('provider_endpoint'))}`",
        f"- Model: `{_md_escape(metadata.get('model'))}` "
        f"({_md_escape(metadata.get('reasoning_effort'))} reasoning)",
        f"- Candidates: **{summary.get('candidates', 0)}** across "
        f"**{summary.get('candidate_sources', 0)}** source folders",
        f"- Decisions: `{_md_escape(_json(summary.get('decision_counts', {})))}`",
        f"- API errors: **{summary.get('api_errors', 0)}**",
    ]
    if scheduled_scope:
        exclusions = _mapping(
            _mapping(scheduled_scope.get("input_audit")).get(
                "source_exclusions"
            )
        )
        lines.extend(
            [
                f"- Scheduled scope: **{scheduled_scope.get('scheduled_rows', 0)}** "
                f"Reels from `{_md_escape(scheduled_scope.get('first_scheduled_at'))}` "
                f"through `{_md_escape(scheduled_scope.get('last_scheduled_at'))}`",
                f"- Current lanes: **{scheduled_scope.get('regular_reels', 0)}** "
                f"regular, **{scheduled_scope.get('trial_reels', 0)}** Trial",
                "- Source exclusions: "
                f"`{_md_escape(', '.join(_sequence(exclusions.get('requested_source_video_ids'))) or 'none')}` "
                f"({exclusions.get('rows_excluded', 0)} scheduled rows removed)",
                "- Automatic schedule changes: **0**",
            ]
        )
    lines.extend(
        [
        "",
        "## What this evaluation actually does",
        "",
        "This is a two-pass model review, not word matching. Pass A reads each "
        "candidate's primary hook/source transcript and the winner published "
        "or selected hooks/source "
        "transcripts, compares meaning and content mechanisms across all six "
        "Top-10 categories, and selects analogues while ranks and values are "
        "hidden. Pass B reveals exact 24-hour evidence only for those locked "
        "analogues and challenges the proposed match before assigning a "
        "decision.",
        "",
        "The five metrics represent two related evidence families. The balanced "
        "aggregate is a summary, not a sixth independent vote. These are "
        "editorial test recommendations, not performance predictions.",
        "",
        "## Decision summary",
        "",
        ]
    )
    if scheduled_scope:
        lines.extend(
            [
                "| # | Candidate | Scheduled | Lane | Decision | Confidence | Reason |",
                "|---:|---|---|---|---|---|---|",
            ]
        )
    else:
        lines.extend(
            [
                "| # | Candidate | Decision | Confidence | Reason |",
                "|---:|---|---|---|---|",
            ]
        )
    for row_value in _sequence(report.get("review_queue")):
        row = _mapping(row_value)
        hook = _md_escape(row.get("hook"))
        source_url = _text(row.get("source_timestamp_url"))
        linked = f"[{hook}]({source_url})" if source_url else hook
        if scheduled_scope:
            lines.append(
                f"| {row.get('review_order')} | {linked} | "
                f"{_md_escape(row.get('scheduled_at'))} | "
                f"{_md_escape(row.get('current_lane'))} | "
                f"**{_md_escape(row.get('decision'))}** | "
                f"{_md_escape(row.get('confidence'))} | "
                f"{_md_escape(row.get('reason'))} |"
            )
        else:
            lines.append(
                f"| {row.get('review_order')} | {linked} | "
                f"**{_md_escape(row.get('decision'))}** | "
                f"{_md_escape(row.get('confidence'))} | "
                f"{_md_escape(row.get('reason'))} |"
            )

    for source_value in _sequence(report.get("sources")):
        source = _mapping(source_value)
        lines.extend(
            [
                "",
                f"## Source `{_md_escape(source.get('video_id'))}`",
                "",
                f"- Title: {_md_escape(source.get('title')) or 'Unavailable'}",
                f"- Reconciled candidates: **{source.get('candidate_count', 0)}**",
                f"- Status: `{_md_escape(source.get('status'))}`",
            ]
        )
        selection = _mapping(source.get("candidate_selection"))
        if selection:
            lines.append(
                "- Candidate selection: "
                f"**{selection.get('source_candidates_selected', 0)}/"
                f"{selection.get('source_candidates_before_filter', 0)}** "
                "using an exact slug allowlist."
            )
        for evaluation_value in _sequence(source.get("evaluations")):
            evaluation = _mapping(evaluation_value)
            candidate = _mapping(evaluation.get("candidate"))
            decision = _mapping(evaluation.get("decision"))
            schedule = _mapping(candidate.get("schedule"))
            lines.extend(
                [
                    "",
                    f"### {_md_escape(candidate.get('hook'))}",
                    "",
                    f"- Decision: **{_md_escape(decision.get('label'))}** "
                    f"({_md_escape(decision.get('confidence'))})",
                    *(
                        [
                            f"- Confidence adjustment: "
                            f"{_md_escape(decision.get('confidence_adjustment'))}"
                        ]
                        if _text(decision.get("confidence_adjustment"))
                        else []
                    ),
                    f"- Reason: {_md_escape(decision.get('reason'))}",
                    f"- Candidate: "
                    + (
                        f"[source timestamp]({_text(candidate.get('source_timestamp_url'))})"
                        if _text(candidate.get("source_timestamp_url"))
                        else "source link unavailable"
                    ),
                    f"- Duration: "
                    f"`{_format_seconds(candidate.get('duration_seconds'))}` seconds",
                    *(
                        [
                            f"- Scheduled: `{_md_escape(schedule.get('scheduled_at'))}` "
                            f"as `{_md_escape(schedule.get('current_lane'))}`",
                            f"- Evaluated scheduled overlay hook: "
                            f"{_md_escape(candidate.get('hook'))}",
                            f"- Caption first line: "
                            f"{_md_escape(schedule.get('caption_hook'))}",
                            f"- Source-selection hook: "
                            f"{_md_escape(candidate.get('source_selection_hook'))}",
                        ]
                        if schedule
                        else []
                    ),
                    f"- Claim support: "
                    f"`{_md_escape(_mapping(evaluation.get('claim_support')).get('overall_status')) or 'unavailable'}`",
                ]
            )
            profile = _mapping(evaluation.get("semantic_profile"))
            if profile:
                lines.extend(
                    [
                        f"- Audience promise: {_md_escape(profile.get('audience_promise'))}",
                        f"- Payoff: {_md_escape(profile.get('payoff_type'))}",
                        f"- Attention hypothesis: {_md_escape(profile.get('attention_hypothesis'))}",
                        f"- Action hypothesis: {_md_escape(profile.get('action_hypothesis'))}",
                    ]
                )
            if evaluation.get("review_status") == "API_ERROR":
                lines.append(
                    f"- LLM error: `{_md_escape(evaluation.get('error'))}`"
                )
                continue
            lines.extend(["", "#### Top-10 category comparisons", ""])
            for comparison_value in _sequence(
                evaluation.get("category_comparisons")
            ):
                comparison = _mapping(comparison_value)
                interpretation = _mapping(
                    comparison.get("evidence_interpretation")
                )
                lines.append(
                    f"**{_md_escape(comparison.get('label'))} — "
                    f"{_md_escape(interpretation.get('fit_after_metrics'))}**"
                )
                analogues = [
                    _mapping(value)
                    for value in _sequence(comparison.get("analogues"))
                ]
                if analogues:
                    for analogue in analogues:
                        lines.append(
                            f"- {_analogue_evidence_line(_text(comparison.get('category')), analogue)}"
                        )
                        lines.append(
                            f"  - Shared mechanism: "
                            f"{_md_escape(', '.join(_sequence(analogue.get('shared_mechanisms'))))}"
                        )
                        lines.append(
                            f"  - Important difference: "
                            f"{_md_escape('; '.join(_sequence(analogue.get('material_differences'))))}"
                        )
                else:
                    lines.append("- No credible semantic analogue was forced.")
                if interpretation:
                    lines.append(
                        f"- Verifier: {_md_escape(interpretation.get('conclusion'))}"
                    )
                lines.append("")
            if _sequence(decision.get("must_fix_before_use")):
                lines.append(
                    "- Must fix: "
                    + "; ".join(
                        _md_escape(value)
                        for value in _sequence(
                            decision.get("must_fix_before_use")
                        )
                    )
                )
            if decision.get("test_hypothesis"):
                lines.append(
                    f"- Test hypothesis: {_md_escape(decision.get('test_hypothesis'))}"
                )
            if decision.get("test_design_warning"):
                lines.append(
                    f"- Test-design warning: "
                    f"{_md_escape(decision.get('test_design_warning'))}"
                )
            coverage = _mapping(evaluation.get("citation_coverage"))
            if coverage:
                lines.append(
                    "- Citation coverage: "
                    f"**{coverage.get('verified_excerpt_checks', 0)}/"
                    f"{coverage.get('excerpt_checks', 0)}** supplied excerpts "
                    "matched the local source text; "
                    f"**{coverage.get('fully_verified_analogue_pairs', 0)}/"
                    f"{coverage.get('selected_analogues', 0)}** analogue pairs "
                    "were fully verified."
                )
            independence = _mapping(
                evaluation.get("analogue_independence")
            )
            if independence:
                lines.append(
                    "- Analogue independence: "
                    f"**{independence.get('unique_analogue_posts', 0)}** "
                    "unique posts from "
                    f"**{independence.get('unique_source_videos', 0)}** "
                    "source videos; "
                    f"**{independence.get('same_source_analogue_posts', 0)}** "
                    "came from the candidate's source video."
                )
                if _text(independence.get("warning")):
                    lines.append(
                        "- Independence warning: "
                        f"{_md_escape(independence.get('warning'))}"
                    )
            warnings = [
                _text(value)
                for value in _sequence(evaluation.get("citation_warnings"))
                if _text(value)
            ]
            if warnings:
                lines.append(
                    f"- Citation audit warnings: {_md_escape('; '.join(warnings))}"
                )

        false_negative = _mapping(source.get("false_negative_audit"))
        if false_negative:
            lines.extend(
                [
                    "",
                    "### Empty-folder false-negative audit",
                    "",
                    f"- Rejections screened independently by LLM: "
                    f"**{false_negative.get('rejected_rows_screened', 0)}**",
                    f"- Screen verdicts: "
                    f"`{_md_escape(_json(false_negative.get('screen_verdict_counts', {})))}`",
                    f"- Deep comparisons completed: "
                    f"**{false_negative.get('deep_reviews_completed', 0)}**",
                    "- Automatic promotions: **0**",
                    "",
                    "| Rejected hook | Independent verdict | Deep-review priority | Reason |",
                    "|---|---|---|---|",
                ]
            )
            screen_rows = [
                (index, _mapping(row_value))
                for index, row_value in enumerate(
                    _sequence(false_negative.get("screen_reviews"))
                )
            ]
            screen_rows.sort(
                key=lambda indexed: (
                    {
                        "TOP_5": 0,
                        "SECONDARY": 1,
                        "NO_DEEP_REVIEW": 2,
                    }.get(
                        _text(
                            indexed[1].get("deep_review_priority")
                        ),
                        99,
                    ),
                    (
                        int(indexed[1]["deep_review_rank"])
                        if isinstance(
                            indexed[1].get("deep_review_rank"),
                            int,
                        )
                        else 99
                    ),
                    indexed[0],
                )
            )
            for _, row in screen_rows:
                candidate_id = _text(row.get("candidate_id"))
                hook = _text(row.get("hook"))
                source_url = _text(row.get("source_timestamp_url"))
                linked_hook = (
                    f"[{_md_escape(hook)}]({source_url})"
                    if source_url and hook
                    else _md_escape(hook or candidate_id)
                )
                priority_label = _md_escape(
                    row.get("deep_review_priority")
                )
                if isinstance(row.get("deep_review_rank"), int):
                    priority_label += f" #{row.get('deep_review_rank')}"
                lines.append(
                    f"| {linked_hook} | "
                    f"**{_md_escape(row.get('verdict'))}** | "
                    f"`{priority_label}` | "
                    f"{_md_escape(row.get('reason'))} |"
                )

    lines.extend(
        [
            "",
            "## Limits",
            "",
            "- Resemblance to a measured winner does not predict performance.",
            "- Candidate-generator scores are retained as provenance but were "
            "not shown to the LLM.",
            "- Exact metrics and Reel links were joined from the fixed-24h "
            "library after the model response.",
            "- The evaluator does not modify candidates, render, schedule, "
            "convert to Trial, remove, or publish anything.",
            "",
        ]
    )
    return "\n".join(lines)
