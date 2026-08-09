"""Compare new Reel candidates with measured fixed-window winner evidence.

The evaluator is intentionally deterministic and transparent. It does not
predict performance and does not create a combined performance score. Candidate
retrieval uses separately exposed entity, topic, hook-pattern, lexical, and
duration comparisons. Decisions are bounded experiment recommendations rather
than publishing instructions.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "reel_candidate_evaluator.json"
SCHEMA_VERSION = 1

DECISION_REPLICATION = "ADVANCE AS REPLICATION TEST"
DECISION_NOVEL = "ADVANCE AS NOVEL TEST"
DECISION_REVISE = "REVISE"
DECISION_HOLD = "HOLD FOR DIVERSITY"
DECISION_INSUFFICIENT = "INSUFFICIENT EVIDENCE"

TIER_PRIORITY = {
    "BALANCED_REFERENCE": 0,
    "CROSS_FAMILY_REFERENCE": 1,
    "INTENT_ACTION_SPECIALIST": 2,
    "ATTENTION_REPLAY_SPECIALIST": 2,
}

STOPWORDS = frozenset(
    {
        "a",
        "about",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "each",
        "even",
        "for",
        "from",
        "get",
        "gets",
        "got",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "inside",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "made",
        "make",
        "more",
        "most",
        "my",
        "no",
        "not",
        "now",
        "of",
        "on",
        "one",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "really",
        "said",
        "say",
        "she",
        "so",
        "some",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "thing",
        "think",
        "this",
        "those",
        "through",
        "to",
        "too",
        "up",
        "us",
        "use",
        "used",
        "using",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

# Aliases intentionally roll up named products to the organization whose
# authority is used in the hook. These are retrieval concepts, not labels
# inferred into the source data.
ENTITY_CONCEPTS: dict[str, tuple[str, ...]] = {
    "anthropic_claude": (
        "anthropic",
        "claude",
        "cloud code",
        "cloud-code",
        "cloudcode",
    ),
    "openai_chatgpt_codex": (
        "openai",
        "open ai",
        "chatgpt",
        "chatbt",
        "codex",
        "codeex",
    ),
    "google_gemini": ("google", "gemini", "deepmind"),
    "meta_facebook": ("meta", "facebook"),
    "microsoft": ("microsoft", "github copilot", "copilot"),
    "palantir": ("palantir", "karp"),
    "y_combinator": ("y combinator", "yc"),
}

TOPIC_CONCEPTS: dict[str, tuple[str, ...]] = {
    "coding_development": (
        "code",
        "coding",
        "developer",
        "developers",
        "engineering",
        "engineer",
        "engineers",
        "software",
        "programming",
        "repository",
        "repos",
        "pull request",
        " pr ",
    ),
    "agents_autonomy": (
        "agent",
        "agents",
        "agentic",
        "autonomous",
        "unsupervised",
        "computer use",
        "takes over",
    ),
    "product_management": (
        "product manager",
        "product managers",
        " pm ",
        "product sense",
        "product process",
        "roadmap",
        "road map",
        "priorities",
    ),
    "management_organization": (
        "manager",
        "management",
        "organization",
        "workflow",
        "workflows",
        "planning",
        "leadership",
    ),
    "jobs_hiring_roles": (
        "hire",
        "hires",
        "hiring",
        "job",
        "jobs",
        "role",
        "roles",
        "career",
        "workforce",
    ),
    "design_taste": (
        "design",
        "designer",
        "designers",
        "taste",
        "creative",
        "creativity",
        "curation",
    ),
    "testing_verification_evals": (
        "test",
        "tests",
        "testing",
        "verify",
        "verification",
        "eval",
        "evals",
        "grade",
        "compile",
        "quality",
    ),
    "user_feedback_frustration": (
        "user feedback",
        "feedback",
        "frustrat",
        "swear",
        "delight",
    ),
    "automation_tools": (
        "automate",
        "automated",
        "automation",
        "extension",
        "plugin",
        "tool",
        "tools",
        "computer",
        "interface",
        "dashboard",
        "spreadsheet",
    ),
    "research_models": (
        "research",
        "model",
        "models",
        "llm",
        "llms",
        "frontier",
        "training",
        "intelligence",
    ),
    "security_deception": (
        "attack",
        "attacks",
        "security",
        "defense",
        "deception",
        "lie",
        "lying",
        "prompt injection",
    ),
    "military_government": (
        "military",
        "war",
        "battlefield",
        "government",
        "law",
        "bureaucr",
    ),
    "science_math_medicine": (
        "math",
        "mathemat",
        "science",
        "scientist",
        "medical",
        "medicine",
        "mri",
        "radiolog",
    ),
    "media_creative_work": (
        "video",
        "premiere pro",
        "music",
        "song",
        "art",
        "editor",
        "editing",
    ),
}

GENERIC_TOPICS = frozenset({"coding_development", "research_models"})
STEM_MARKERS = frozenset({"bureaucr", "frustrat", "mathemat", "radiolog"})
NUMBER_WORDS = frozenset(
    {
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "hundred",
        "thousand",
        "million",
        "billion",
    }
)
NUMBER_EQUIVALENTS: dict[str, frozenset[str]] = {
    "0": frozenset({"0", "zero"}),
    "1": frozenset({"1", "one"}),
    "2": frozenset({"2", "two"}),
    "3": frozenset({"3", "three"}),
    "4": frozenset({"4", "four"}),
    "5": frozenset({"5", "five"}),
    "6": frozenset({"6", "six"}),
    "7": frozenset({"7", "seven"}),
    "8": frozenset({"8", "eight"}),
    "9": frozenset({"9", "nine"}),
    "10": frozenset({"10", "ten"}),
    "100": frozenset({"100", "hundred"}),
    "1000": frozenset({"1000", "thousand"}),
    "1000000": frozenset({"1000000", "million"}),
    "1000000000": frozenset({"1000000000", "billion"}),
}

HOOK_PATTERNS: dict[str, tuple[str, ...]] = {
    "named_authority": tuple(
        alias
        for aliases in ENTITY_CONCEPTS.values()
        for alias in aliases
        if len(alias.strip()) >= 3
    ),
    "number_or_quantifier": (
        "100%",
        "two ",
        "three ",
        "five ",
        "10x",
        "only ",
        "all ",
        "every ",
        "zero ",
        "unlimited",
    ),
    "reversal_or_contradiction": (
        "but ",
        "instead",
        "no longer",
        "doesn t",
        "didn t",
        "isn t",
        "not ",
        "dead",
        "wrong",
        "failed",
        "failure",
        "without",
        "bypass",
        "backwards",
    ),
    "concrete_artifact": (
        "dashboard",
        "spreadsheet",
        "extension",
        "test",
        "code",
        "premiere pro",
        "tokens",
        "document",
        "mouse",
        "computer",
        "lunch",
    ),
    "question_or_explanation": ("why ", "how ", "what ", "?"),
    "direct_instruction": ("ask ", "use ", "build ", "write ", "steal "),
    "insider_access": (
        "inside ",
        "our ",
        "we ",
        "at anthropic",
        "at openai",
        "elite ",
    ),
    "risk_or_conflict": (
        "attack",
        "death",
        "dead",
        "kill",
        "lose",
        "fear",
        "angry",
        "frustrat",
        "swear",
        "police",
        "war",
    ),
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _number_or_infinity(value: Any) -> float:
    number = _number(value)
    return number if number is not None else math.inf


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {label}: {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} root must be an object: {path}")
    return parsed


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config = load_json_object(path, label="candidate evaluator config")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported candidate evaluator config schema: "
            f"{config.get('schema_version')!r}"
        )
    return config


def normalize_for_match(text: str) -> str:
    normalized = text.lower().replace("’", "'").replace("'", " ")
    normalized = re.sub(r"[^a-z0-9%+._-]+", " ", normalized)
    return f" {' '.join(normalized.split())} "


def tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z][a-z0-9+._-]{1,}|[0-9]+(?:%|x)?", text.lower())
    cleaned = [token.strip("._-") for token in raw]
    return [
        token
        for token in cleaned
        if token
        and token not in STOPWORDS
        and (len(token) >= 3 or token in {"ai", "pm"})
    ]


def marker_present(normalized: str, marker: str) -> bool:
    cleaned = " ".join(marker.lower().split())
    if not cleaned:
        return False
    if cleaned in {"?"}:
        return cleaned in normalized
    if cleaned in STEM_MARKERS:
        return re.search(rf"\b{re.escape(cleaned)}[a-z]*\b", normalized) is not None
    # ``normalize_for_match`` deliberately preserves punctuation that carries
    # product-name meaning (for example ``claude-code`` and ``gpt-4``). Exact
    # space padding therefore produces false negatives at ordinary sentence
    # boundaries such as ``Microsoft.``. Match on alphanumeric boundaries while
    # retaining literal punctuation inside the marker.
    pattern = re.escape(cleaned).replace(r"\ ", r"\s+")
    return (
        re.search(
            rf"(?<![a-z0-9]){pattern}(?![a-z0-9])",
            normalized,
        )
        is not None
    )


def extract_concepts(text: str) -> tuple[list[str], list[str]]:
    normalized = normalize_for_match(text)
    entities = [
        concept
        for concept, aliases in ENTITY_CONCEPTS.items()
        if any(marker_present(normalized, alias) for alias in aliases)
    ]
    topics = [
        concept
        for concept, aliases in TOPIC_CONCEPTS.items()
        if any(marker_present(normalized, alias) for alias in aliases)
    ]
    return sorted(entities), sorted(topics)


def extract_hook_patterns(text: str) -> list[str]:
    normalized = normalize_for_match(text)
    return sorted(
        pattern
        for pattern, markers in HOOK_PATTERNS.items()
        if any(marker_present(normalized, marker) for marker in markers)
        or (pattern == "question_or_explanation" and "?" in text)
    )


def duration_bucket(duration: float | None, config: Mapping[str, Any]) -> str | None:
    if duration is None or duration < 0:
        return None
    for raw in _sequence(config.get("duration_buckets_seconds")):
        bucket = _mapping(raw)
        minimum = _number(bucket.get("minimum"))
        maximum = _number(bucket.get("maximum"))
        if minimum is None:
            continue
        if duration >= minimum and (maximum is None or duration <= maximum):
            return _text(bucket.get("label")) or None
    return None


def timestamp_url(url: str, start_seconds: float | None) -> str:
    if not url or start_seconds is None:
        return url
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["t"] = f"{max(0, int(round(start_seconds)))}s"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def winner_text(winner: Mapping[str, Any]) -> tuple[str, str]:
    content = _mapping(winner.get("content"))
    source = _mapping(winner.get("source"))
    hook_parts = [
        _text(_mapping(content.get("published_hook")).get("value")),
        _text(content.get("source_selection_hook")),
        *[_text(value) for value in _sequence(content.get("source_hook_variants"))],
        *[
            _text(value)
            for value in _sequence(content.get("current_localized_hook_options"))
        ],
    ]
    body_parts = [
        _text(source.get("title")),
        _text(source.get("chapter")),
        _text(_mapping(content.get("source_transcript")).get("text")),
    ]
    return " ".join(filter(None, hook_parts)), " ".join(filter(None, body_parts))


def candidate_text(candidate: Mapping[str, Any]) -> tuple[str, str]:
    hook_parts = [
        _text(candidate.get("hook")),
        *[_text(value) for value in _sequence(candidate.get("hook_variants"))],
    ]
    body_parts = [
        _text(candidate.get("source_chapter")),
        _text(candidate.get("reason")),
        _text(candidate.get("transcript")),
        _text(_mapping(candidate.get("source")).get("title")),
    ]
    return " ".join(filter(None, hook_parts)), " ".join(filter(None, body_parts))


def candidate_retrieval_text(candidate: Mapping[str, Any]) -> str:
    hook_text, _ = candidate_text(candidate)
    transcript_excerpt = " ".join(
        tokens(_text(candidate.get("transcript")))[:120]
    )
    return " ".join(
        filter(
            None,
            [
                hook_text,
                _text(candidate.get("source_chapter")),
                _text(candidate.get("reason")),
                transcript_excerpt,
            ],
        )
    )


def inverse_document_frequencies(documents: Sequence[str]) -> dict[str, float]:
    document_count = len(documents)
    frequencies: Counter[str] = Counter()
    for document in documents:
        frequencies.update(set(tokens(document)))
    return {
        token: math.log((document_count + 1) / (frequency + 1)) + 1
        for token, frequency in frequencies.items()
    }


def tfidf_vector(text: str, idf: Mapping[str, float]) -> dict[str, float]:
    counts = Counter(tokens(text))
    if not counts:
        return {}
    maximum = max(counts.values())
    fallback = math.log(len(idf) + 2) + 1
    return {
        token: (count / maximum) * idf.get(token, fallback)
        for token, count in counts.items()
    }


def cosine(left: Mapping[str, float], right: Mapping[str, float]) -> float:
    if not left or not right:
        return 0.0
    shared = set(left) & set(right)
    numerator = sum(left[token] * right[token] for token in shared)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def shared_keywords(
    left: Mapping[str, float],
    right: Mapping[str, float],
    *,
    limit: int = 10,
) -> list[str]:
    return [
        token
        for token in sorted(
            set(left) & set(right),
            key=lambda token: (-(left[token] * right[token]), token),
        )[:limit]
    ]


def metric_summary(winner: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _mapping(winner.get("winner_evidence"))
    metrics = _mapping(evidence.get("all_metrics_at_window"))
    result: dict[str, Any] = {}
    for key in (
        "total_interactions_per_reach",
        "watch_depth",
        "three_second_skip_rate",
        "saves_per_1000_reach",
        "views_per_reached_account",
    ):
        metric = _mapping(metrics.get(key))
        result[key] = {
            "value": _number(metric.get("value")),
            "supporting_metrics": dict(_mapping(metric.get("supporting_metrics"))),
        }
    return result


def normalize_winner(winner: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    identity = _mapping(winner.get("identity"))
    content = _mapping(winner.get("content"))
    source = _mapping(winner.get("source"))
    evidence = _mapping(winner.get("winner_evidence"))
    hook_text, body_text = winner_text(winner)
    transcript_excerpt = " ".join(
        tokens(_text(_mapping(content.get("source_transcript")).get("text")))[:120]
    )
    match_text = " ".join(
        filter(
            None,
            [
                hook_text,
                _text(source.get("chapter")),
                transcript_excerpt,
            ],
        )
    )
    entities, _ = extract_concepts(f"{hook_text} {body_text}")
    _, topics = extract_concepts(
        f"{hook_text} {_text(source.get('chapter'))}"
    )
    duration = _number(content.get("duration_seconds"))
    return {
        "media_id": _text(identity.get("media_id")),
        "permalink": _text(identity.get("permalink")),
        "published_hook": _text(
            _mapping(content.get("published_hook")).get("value")
        ),
        "script_asset_id": _text(content.get("script_asset_id")),
        "opening_japanese_script": [
            _text(value)
            for value in _sequence(content.get("opening_japanese_script"))
            if _text(value)
        ],
        "source_hook": _text(content.get("source_selection_hook")),
        "source_video_id": _text(source.get("video_id")),
        "source_title": _text(source.get("title")),
        "source_uploader": _text(source.get("uploader")),
        "duration_seconds": duration,
        "duration_bucket": duration_bucket(duration, config),
        "tier": _text(evidence.get("tier")),
        "actual_age_hours": _number(evidence.get("actual_age_hours")),
        "signal_families": sorted(
            {_text(value) for value in _sequence(evidence.get("signal_families")) if _text(value)}
        ),
        "ranking_memberships": [
            dict(_mapping(row))
            for row in _sequence(evidence.get("ranking_memberships"))
            if _mapping(row)
        ],
        "aggregate_rank": _number(_mapping(evidence.get("aggregate")).get("rank")),
        "metrics_24h": metric_summary(winner),
        "evidence_flags": [
            _text(value)
            for value in _sequence(winner.get("evidence_flags"))
            if _text(value)
        ],
        "entities": entities,
        "topics": topics,
        "hook_patterns": extract_hook_patterns(hook_text),
        "_match_text": match_text,
    }


def normalize_clip_candidate(
    clip: Mapping[str, Any],
    *,
    position: int,
    video_id: str,
    source_url: str,
    metadata: Mapping[str, Any],
    config: Mapping[str, Any],
    origin: str,
) -> dict[str, Any]:
    index_number = int(_number(clip.get("index")) or position)
    duration = _number(clip.get("duration"))
    start_seconds = _number(clip.get("start"))
    end_seconds = _number(clip.get("end"))
    if (
        duration is None
        and start_seconds is not None
        and end_seconds is not None
        and end_seconds >= start_seconds
    ):
        duration = end_seconds - start_seconds
    interval_id = (
        f"{int(round(start_seconds * 1000))}-"
        f"{int(round(end_seconds * 1000))}"
        if start_seconds is not None and end_seconds is not None
        else f"index-{index_number:03d}"
    )
    candidate = {
        "candidate_id": f"{video_id}:{interval_id}",
        "candidate_origin": origin,
        "index": index_number,
        "slug": _text(clip.get("slug")),
        "hook": _text(clip.get("one_liner")),
        "hook_variants": [
            _text(value)
            for value in _sequence(clip.get("hook_variants"))
            if _text(value)
        ],
        "reason": _text(clip.get("reason")),
        "source_chapter": _text(clip.get("source_chapter")),
        "transcript": _text(clip.get("transcript")),
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "duration_seconds": duration,
        "duration_bucket": duration_bucket(duration, config),
        "source_timestamp_url": timestamp_url(source_url, start_seconds),
        "selection_scores": {
            "overall": _number(clip.get("score")),
            "hook": _number(clip.get("hook_score")),
            "value": _number(clip.get("value_score")),
            "opening": _number(
                _mapping(clip.get("opening_assessment")).get("score")
            ),
        },
        "opening_assessment": dict(_mapping(clip.get("opening_assessment"))),
        "source": {
            "video_id": video_id,
            "title": _text(metadata.get("title")),
            "uploader": _text(metadata.get("uploader")),
            "url": source_url,
        },
    }
    hook_text, body_text = candidate_text(candidate)
    entities, _ = extract_concepts(f"{hook_text} {body_text}")
    _, topics = extract_concepts(
        " ".join(
            [
                hook_text,
                _text(candidate.get("source_chapter")),
                _text(candidate.get("reason")),
            ]
        )
    )
    candidate["match_features"] = {
        "entities": entities,
        "topics": topics,
        "hook_patterns": extract_hook_patterns(hook_text),
    }
    return candidate


def normalize_candidate_source(
    candidates_path: Path,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    payload = load_json_object(candidates_path, label="Reel candidates JSON")
    metadata_path = candidates_path.parent / "metadata.json"
    metadata = (
        load_json_object(metadata_path, label="Reel source metadata")
        if metadata_path.is_file()
        else {}
    )
    video_id = candidates_path.parent.name
    source_url = _text(metadata.get("webpage_url"))
    normalized_candidates = [
        normalize_clip_candidate(
            clip,
            position=position,
            video_id=video_id,
            source_url=source_url,
            metadata=metadata,
            config=config,
            origin="reconciled_candidate",
        )
        for position, raw in enumerate(_sequence(payload.get("clips")), start=1)
        if (clip := _mapping(raw))
    ]
    return {
        "video_id": video_id,
        "title": _text(metadata.get("title")),
        "uploader": _text(metadata.get("uploader")),
        "url": source_url,
        "candidate_file": str(candidates_path.resolve()),
        "candidate_file_sha256": sha256_file(candidates_path),
        "metadata_file": str(metadata_path.resolve()) if metadata_path.is_file() else None,
        "selection_mode": _text(payload.get("selection_mode")),
        "caption_source": _text(payload.get("caption_source")),
        "selection_profile": _text(payload.get("selection_profile")),
        "selection_profile_version": payload.get("selection_profile_version"),
        "candidate_reconciliation_version": payload.get(
            "candidate_reconciliation_version"
        ),
        "prompt_versions": dict(_mapping(payload.get("prompt_versions"))),
        "prompt_lineage_sha256": _text(payload.get("prompt_lineage_sha256")),
        "candidate_count": len(normalized_candidates),
        "status": (
            "AVAILABLE"
            if normalized_candidates
            else "NO_RECONCILED_CANDIDATES"
        ),
        "candidates": normalized_candidates,
    }


def build_analogue(
    candidate: Mapping[str, Any],
    winner: Mapping[str, Any],
    *,
    idf: Mapping[str, float],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_vector = tfidf_vector(candidate_retrieval_text(candidate), idf)
    winner_vector = tfidf_vector(_text(winner.get("_match_text")), idf)
    lexical = cosine(candidate_vector, winner_vector)
    candidate_features = _mapping(candidate.get("match_features"))
    candidate_entities = set(_sequence(candidate_features.get("entities")))
    candidate_topics = set(_sequence(candidate_features.get("topics")))
    candidate_patterns = set(_sequence(candidate_features.get("hook_patterns")))
    winner_entities = set(_sequence(winner.get("entities")))
    winner_topics = set(_sequence(winner.get("topics")))
    winner_patterns = set(_sequence(winner.get("hook_patterns")))
    shared_entities = sorted(candidate_entities & winner_entities)
    shared_topics = sorted(candidate_topics & winner_topics)
    shared_specific_topics = sorted(
        (candidate_topics & winner_topics) - GENERIC_TOPICS
    )
    shared_patterns = sorted(candidate_patterns & winner_patterns)
    candidate_duration = _number(candidate.get("duration_seconds"))
    winner_duration = _number(winner.get("duration_seconds"))
    duration_delta = (
        abs(candidate_duration - winner_duration)
        if candidate_duration is not None and winner_duration is not None
        else None
    )
    relevance_config = _mapping(config.get("relevance"))
    minimum_concepts = int(
        _number(relevance_config.get("minimum_shared_concepts")) or 1
    )
    minimum_with_concept = _number(
        relevance_config.get("minimum_keyword_cosine_with_concept")
    )
    minimum_without_concept = _number(
        relevance_config.get("minimum_keyword_cosine_without_concept")
    )
    concept_count = len(shared_entities) + len(shared_topics)
    with_concept_threshold = minimum_with_concept or 0
    without_concept_threshold = minimum_without_concept or 1
    if shared_entities:
        relevant = (
            bool(shared_specific_topics)
            and lexical >= with_concept_threshold
        ) or (
            concept_count >= max(2, minimum_concepts)
            and lexical >= max(with_concept_threshold, 0.05)
        ) or lexical >= 0.10
    else:
        relevant = (
            len(shared_specific_topics) >= 2
            and lexical >= with_concept_threshold
        ) or (
            len(shared_topics) >= 3
            and lexical >= max(with_concept_threshold, 0.04)
        ) or lexical >= without_concept_threshold
    same_bucket = (
        bool(candidate.get("duration_bucket"))
        and candidate.get("duration_bucket") == winner.get("duration_bucket")
    )
    duration_tolerance = (
        max(5.0, candidate_duration * 0.25)
        if candidate_duration is not None
        else None
    )
    duration_comparable = bool(same_bucket) or (
        duration_delta is not None
        and duration_tolerance is not None
        and duration_delta <= duration_tolerance
    )
    close_for_replication = relevant and duration_comparable and (
        bool(shared_specific_topics) or len(shared_patterns) >= 2
    )
    return {
        "media_id": winner.get("media_id"),
        "script_asset_id": winner.get("script_asset_id"),
        "permalink": winner.get("permalink"),
        "published_hook": winner.get("published_hook"),
        "source_hook": winner.get("source_hook"),
        "source_video_id": winner.get("source_video_id"),
        "source_title": winner.get("source_title"),
        "source_uploader": winner.get("source_uploader"),
        "tier": winner.get("tier"),
        "actual_age_hours": winner.get("actual_age_hours"),
        "opening_japanese_script": winner.get("opening_japanese_script"),
        "signal_families": winner.get("signal_families"),
        "duration_seconds": winner_duration,
        "duration_bucket": winner.get("duration_bucket"),
        "ranking_memberships": winner.get("ranking_memberships"),
        "aggregate_rank": winner.get("aggregate_rank"),
        "metrics_24h": winner.get("metrics_24h"),
        "evidence_flags": winner.get("evidence_flags"),
        "comparison": {
            "relevant": relevant,
            "shared_entities": shared_entities,
            "shared_topics": shared_topics,
            "shared_specific_topics": shared_specific_topics,
            "shared_hook_patterns": shared_patterns,
            "keyword_cosine": _round(lexical),
            "shared_keywords": shared_keywords(
                candidate_vector, winner_vector, limit=10
            ),
            "same_duration_bucket": same_bucket,
            "duration_delta_seconds": _round(duration_delta, 2),
            "duration_comparable": duration_comparable,
            "duration_tolerance_seconds": _round(duration_tolerance, 2),
            "close_for_replication": close_for_replication,
            "retrieval_order": [
                "shared named entities",
                "shared topic concepts",
                "keyword cosine",
                "shared hook patterns",
                "same duration bucket",
                "duration delta",
                "winner evidence tier",
            ],
        },
        "_sort": (
            -len(shared_entities),
            -len(shared_specific_topics),
            -len(shared_topics),
            -lexical,
            -len(shared_patterns),
            -int(same_bucket),
            duration_delta if duration_delta is not None else math.inf,
            TIER_PRIORITY.get(_text(winner.get("tier")), 99),
            _text(winner.get("media_id")),
        ),
    }


def hook_support(candidate: Mapping[str, Any]) -> dict[str, Any]:
    hook = _text(candidate.get("hook"))
    source = _mapping(candidate.get("source"))
    evidence_text = " ".join(
        [
            _text(candidate.get("transcript")),
            _text(candidate.get("source_chapter")),
            _text(source.get("title")),
        ]
    )
    if not hook:
        return {
            "status": "UNAVAILABLE",
            "coverage": None,
            "supported_anchors": [],
            "unsupported_anchors": [],
            "hard_mismatches": ["primary hook missing"],
            "warning": "Primary hook is missing.",
        }
    hook_normalized = normalize_for_match(hook)
    evidence_normalized = normalize_for_match(evidence_text)
    hook_entities, _ = extract_concepts(hook)
    evidence_entities, _ = extract_concepts(evidence_text)
    number_anchors = sorted(
        {
            value.lower()
            for value in re.findall(
                r"(?<!\w)\d+(?:%|x)?(?!\w)|\b[a-z]+\b",
                hook.lower(),
            )
            if value.lower() in NUMBER_WORDS
            or re.fullmatch(r"\d+(?:%|x)?", value.lower())
        }
    )
    artifact_markers = sorted(
        {
            marker.strip()
            for marker in HOOK_PATTERNS["concrete_artifact"]
            if marker_present(hook_normalized, marker)
        }
    )
    anchors = (
        [f"entity:{value}" for value in hook_entities]
        + [f"number:{value}" for value in number_anchors]
        + [f"artifact:{value}" for value in artifact_markers]
    )
    supported: list[str] = []
    unsupported: list[str] = []
    for anchor in anchors:
        kind, value = anchor.split(":", 1)
        if kind == "entity":
            present = value in evidence_entities
        elif kind == "number":
            numeric_value = value.rstrip("%x")
            equivalents = NUMBER_EQUIVALENTS.get(
                numeric_value,
                frozenset({numeric_value}),
            )
            if value in NUMBER_WORDS:
                equivalents = next(
                    (
                        aliases
                        for aliases in NUMBER_EQUIVALENTS.values()
                        if value in aliases
                    ),
                    frozenset({value}),
                )
            evidence_number_tokens = {
                token.rstrip("%x")
                for token in re.findall(
                    r"(?<!\w)\d+(?:%|x)?(?!\w)|\b[a-z]+\b",
                    evidence_text.lower(),
                )
            }
            present = bool(set(equivalents) & evidence_number_tokens)
        else:
            present = marker_present(evidence_normalized, value)
        (supported if present else unsupported).append(anchor)
    coverage = len(supported) / len(anchors) if anchors else None
    return {
        "status": "ANCHOR_SCREEN_ONLY",
        "coverage": _round(coverage),
        "supported_anchors": supported,
        "unsupported_anchors": unsupported,
        "hard_mismatches": [
            anchor
            for anchor in unsupported
            if not anchor.startswith("artifact:")
        ],
        "semantic_claim_review": "REQUIRED",
        "warning": (
            "Exact entity, number, and artifact anchors are screened. Semantic "
            "claim support still requires review; this is not entailment."
        ),
    }


def evidence_status(
    analogues: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> tuple[str, list[str]]:
    relevant = [
        analogue
        for analogue in analogues
        if bool(_mapping(analogue.get("comparison")).get("relevant"))
    ]
    close = [
        analogue
        for analogue in relevant
        if bool(
            _mapping(analogue.get("comparison")).get(
                "close_for_replication"
            )
        )
    ]
    sources = {
        _text(analogue.get("source_video_id"))
        for analogue in close
        if _text(analogue.get("source_video_id"))
    }
    uploaders = {
        _text(analogue.get("source_uploader"))
        for analogue in close
        if _text(analogue.get("source_uploader"))
    }
    evidence = _mapping(config.get("evidence"))
    strong_n = int(_number(evidence.get("strong_minimum_analogues")) or 3)
    strong_sources = int(
        _number(evidence.get("strong_minimum_distinct_sources")) or 2
    )
    strong_uploaders = int(
        _number(evidence.get("strong_minimum_distinct_uploaders")) or 2
    )
    developing_n = int(
        _number(evidence.get("developing_minimum_analogues")) or 2
    )
    developing_sources = int(
        _number(evidence.get("developing_minimum_distinct_sources")) or 2
    )
    explanations = [
        f"{len(relevant)} relevant measured analogue(s)",
        f"{len(close)} duration/structure-comparable analogue(s)",
        f"{len(sources)} distinct close-analogue source video(s)",
        f"{len(uploaders)} distinct close-analogue uploader(s)",
    ]
    if (
        len(close) >= strong_n
        and len(sources) >= strong_sources
        and len(uploaders) >= strong_uploaders
    ):
        return "STRONG_ANALOGUE_SET", explanations
    if len(close) >= developing_n and len(sources) >= developing_sources:
        return "DEVELOPING_ANALOGUE_SET", explanations
    if relevant:
        return "THIN_ANALOGUE_SET", explanations
    return "NO_RELEVANT_ANALOGUES", explanations


def analogue_lane(analogues: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    close = [
        analogue
        for analogue in analogues
        if bool(
            _mapping(analogue.get("comparison")).get(
                "close_for_replication"
            )
        )
    ]
    eligible = close or [
        analogue
        for analogue in analogues
        if bool(_mapping(analogue.get("comparison")).get("relevant"))
    ]
    for analogue in eligible:
        counts.update(_sequence(analogue.get("signal_families")))
    intent = counts.get("intent_action", 0)
    attention = counts.get("attention_replay", 0)
    if not intent and not attention:
        lane = "UNESTABLISHED"
    elif intent == attention:
        lane = "MIXED"
    elif intent > attention:
        lane = "INTENT_ACTION"
    else:
        lane = "ATTENTION_REPLAY"
    return {
        "recommended_lane": lane,
        "analogue_family_counts": {
            "intent_action": intent,
            "attention_replay": attention,
        },
        "warning": (
            "This is the dominant family among retrieved measured analogues; "
            "it is not a prediction and should be confirmed before testing."
        ),
    }


def selection_score_assessment(
    candidate: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    thresholds = _mapping(config.get("candidate_readiness"))
    scores = _mapping(candidate.get("selection_scores"))
    checks = (
        ("hook", "minimum_hook_score"),
        ("value", "minimum_value_score"),
        ("opening", "minimum_opening_score"),
    )
    failures: list[str] = []
    unavailable: list[str] = []
    for score_name, threshold_name in checks:
        value = _number(scores.get(score_name))
        threshold = _number(thresholds.get(threshold_name))
        if value is None:
            unavailable.append(f"{score_name} selection score unavailable")
        elif threshold is not None and value < threshold:
            failures.append(f"{score_name} score {value:g} < {threshold:g}")
    return failures, unavailable


def initial_decision(
    candidate: Mapping[str, Any],
    *,
    status: str,
    analogues: Sequence[Mapping[str, Any]],
    support: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[str, str, str]:
    if not _text(candidate.get("hook")) or not _text(candidate.get("transcript")):
        return (
            DECISION_INSUFFICIENT,
            "LOW",
            "A primary hook and source transcript are both required.",
        )
    score_failures, unavailable_scores = selection_score_assessment(
        candidate,
        config,
    )
    hard_mismatches = [
        _text(value)
        for value in _sequence(support.get("hard_mismatches"))
        if _text(value)
    ]
    relevant = [
        analogue
        for analogue in analogues
        if bool(_mapping(analogue.get("comparison")).get("relevant"))
    ]
    close = [
        analogue
        for analogue in relevant
        if bool(
            _mapping(analogue.get("comparison")).get(
                "close_for_replication"
            )
        )
    ]
    balanced = sum(
        _text(analogue.get("tier"))
        in {"BALANCED_REFERENCE", "CROSS_FAMILY_REFERENCE"}
        for analogue in close
    )
    if score_failures or hard_mismatches:
        reasons = list(score_failures)
        if hard_mismatches:
            reasons.append(
                "unsupported exact hook anchor(s): "
                + ", ".join(hard_mismatches)
            )
        return DECISION_REVISE, "DEVELOPING", "; ".join(reasons) + "."
    if unavailable_scores:
        return (
            DECISION_INSUFFICIENT,
            "LOW",
            (
                "; ".join(unavailable_scores)
                + ". Missing historical metadata is unavailable evidence, "
                "not evidence that the candidate failed."
            ),
        )
    if status == "STRONG_ANALOGUE_SET" and balanced:
        return (
            DECISION_REPLICATION,
            "MEASURED_ANALOGUES",
            (
                f"{len(close)} close 24-hour analogues support a bounded "
                f"replication test; {balanced} are balanced or cross-family "
                "references."
            ),
        )
    if status == "STRONG_ANALOGUE_SET":
        return (
            DECISION_NOVEL,
            "DIRECTIONAL",
            (
                "Three relevant analogues exist, but the retrieved set contains "
                "specialist evidence only. Run it as a novel test rather than "
                "calling the pattern balanced."
            ),
        )
    if status in {"DEVELOPING_ANALOGUE_SET", "THIN_ANALOGUE_SET"}:
        return (
            DECISION_NOVEL,
            "DIRECTIONAL",
            (
                f"The candidate clears source-quality checks, but its {status.lower().replace('_', ' ')} "
                "supports only a bounded novel test—not a predicted winner."
            ),
        )
    return (
        DECISION_NOVEL,
        "EXPLORATORY",
        (
            "The candidate clears source-quality checks but has no sufficiently "
            "relevant measured analogue; treat it as exploration."
        ),
    )


def concept_jaccard(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_features = _mapping(left.get("match_features"))
    right_features = _mapping(right.get("match_features"))
    left_set = set(_sequence(left_features.get("topics"))) - GENERIC_TOPICS
    right_set = set(_sequence(right_features.get("topics"))) - GENERIC_TOPICS
    if not left_set and not right_set:
        return 0.0
    if not left_set & right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def select_analogue_roles(
    matches: Sequence[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    relevant = [
        match
        for match in matches
        if bool(_mapping(match.get("comparison")).get("relevant"))
    ]
    pool = relevant or list(matches)

    def comparison(match: Mapping[str, Any]) -> Mapping[str, Any]:
        return _mapping(match.get("comparison"))

    def topic_key(match: Mapping[str, Any]) -> tuple[Any, ...]:
        detail = comparison(match)
        return (
            -len(_sequence(detail.get("shared_specific_topics"))),
            -len(_sequence(detail.get("shared_entities"))),
            -len(_sequence(detail.get("shared_topics"))),
            -(_number(detail.get("keyword_cosine")) or 0),
            _number_or_infinity(detail.get("duration_delta_seconds")),
            _text(match.get("media_id")),
        )

    def structure_key(match: Mapping[str, Any]) -> tuple[Any, ...]:
        detail = comparison(match)
        return (
            -len(_sequence(detail.get("shared_hook_patterns"))),
            -len(_sequence(detail.get("shared_entities"))),
            -len(_sequence(detail.get("shared_specific_topics"))),
            -(_number(detail.get("keyword_cosine")) or 0),
            _text(match.get("media_id")),
        )

    def duration_key(match: Mapping[str, Any]) -> tuple[Any, ...]:
        detail = comparison(match)
        return (
            -int(bool(detail.get("same_duration_bucket"))),
            _number_or_infinity(detail.get("duration_delta_seconds")),
            -len(_sequence(detail.get("shared_specific_topics"))),
            -len(_sequence(detail.get("shared_hook_patterns"))),
            -(_number(detail.get("keyword_cosine")) or 0),
            _text(match.get("media_id")),
        )

    roles = (
        ("TOPIC", topic_key),
        ("STRUCTURE", structure_key),
        ("DURATION", duration_key),
    )
    selected: list[dict[str, Any]] = []
    selected_sources: set[str] = set()
    for role, key in roles:
        ordered = sorted(pool, key=key)
        choice = next(
            (
                match
                for match in ordered
                if match not in selected
                and (
                    not _text(match.get("source_video_id"))
                    or _text(match.get("source_video_id"))
                    not in selected_sources
                )
            ),
            None,
        )
        if choice is None:
            continue
        choice["analogue_role"] = role
        selected.append(choice)
        source_id = _text(choice.get("source_video_id"))
        if source_id:
            selected_sources.add(source_id)
        if len(selected) >= limit:
            return selected
    for match in list(relevant) + list(matches):
        if match in selected:
            continue
        source_id = _text(match.get("source_video_id"))
        if source_id and source_id in selected_sources:
            continue
        match["analogue_role"] = "FALLBACK"
        selected.append(match)
        if source_id:
            selected_sources.add(source_id)
        if len(selected) >= limit:
            break
    return selected


def apply_diversity_holds(
    evaluations: list[dict[str, Any]],
    config: Mapping[str, Any],
) -> None:
    threshold = _number(
        _mapping(config.get("diversity")).get(
            "candidate_concept_jaccard_threshold"
        )
    )
    if threshold is None:
        return
    eligible = [
        evaluation
        for evaluation in evaluations
        if evaluation["decision"]["recommendation"]
        in {DECISION_REPLICATION, DECISION_NOVEL}
    ]
    eligible.sort(
        key=lambda evaluation: (
            0
            if evaluation["decision"]["recommendation"] == DECISION_REPLICATION
            else 1,
            -int(
                evaluation["evidence_status"]["status"]
                == "STRONG_ANALOGUE_SET"
            ),
            -(
                _number(
                    _mapping(evaluation["candidate"].get("selection_scores")).get(
                        "overall"
                    )
                )
                or 0
            ),
            evaluation["candidate"]["candidate_id"],
        )
    )
    kept: list[dict[str, Any]] = []
    for evaluation in eligible:
        redundant_with: tuple[dict[str, Any], float] | None = None
        for prior in kept:
            similarity = concept_jaccard(
                evaluation["candidate"], prior["candidate"]
            )
            if similarity >= threshold:
                redundant_with = (prior, similarity)
                break
        if redundant_with is None:
            kept.append(evaluation)
            continue
        prior, similarity = redundant_with
        evaluation["decision"] = {
            "recommendation": DECISION_HOLD,
            "confidence_or_evidence_status": "PORTFOLIO_DIVERSITY",
            "analogue_evidence_confidence": evaluation["decision"].get(
                "analogue_evidence_confidence", "NONE"
            ),
            "confidence_scope": "comparison quality only",
            "outcome_prediction": None,
            "reason": (
                f"Concept overlap is {similarity:.0%} with higher-priority "
                f"candidate {prior['candidate']['candidate_id']}; hold this one "
                "unless the batch needs another test in the same lane."
            ),
        }
        evaluation["portfolio_flags"].append(
            {
                "flag": "NEAR_DUPLICATE_CANDIDATE_CONCEPTS",
                "candidate_id": prior["candidate"]["candidate_id"],
                "concept_jaccard": _round(similarity),
            }
        )


def evaluate_candidate(
    candidate: Mapping[str, Any],
    winners: Sequence[Mapping[str, Any]],
    *,
    idf: Mapping[str, float],
    config: Mapping[str, Any],
    winner_uploader_counts: Mapping[str, int],
    winner_video_counts: Mapping[str, int],
    batch_source_candidate_count: int,
    batch_uploader_candidate_count: int,
    batch_candidate_count: int,
) -> dict[str, Any]:
    matches = [
        build_analogue(candidate, winner, idf=idf, config=config)
        for winner in winners
    ]
    matches.sort(key=lambda match: match["_sort"])
    limit = int(_number(config.get("analogue_limit")) or 3)
    relevant = [
        match
        for match in matches
        if bool(_mapping(match.get("comparison")).get("relevant"))
    ]
    selected = select_analogue_roles(matches, limit=limit)
    for match in selected:
        match.pop("_sort", None)
    status, status_reasons = evidence_status(selected, config)
    support = hook_support(candidate)
    decision, confidence, reason = initial_decision(
        candidate,
        status=status,
        analogues=selected,
        support=support,
        config=config,
    )
    analogue_confidence = {
        "STRONG_ANALOGUE_SET": "HIGH",
        "DEVELOPING_ANALOGUE_SET": "MEDIUM",
        "THIN_ANALOGUE_SET": "LOW",
        "NO_RELEVANT_ANALOGUES": "NONE",
    }.get(status, "NONE")
    source = _mapping(candidate.get("source"))
    uploader = _text(source.get("uploader"))
    video_id = _text(source.get("video_id"))
    flags: list[dict[str, Any]] = []
    uploader_winners = int(winner_uploader_counts.get(uploader, 0))
    video_winners = int(winner_video_counts.get(video_id, 0))
    if uploader_winners:
        flags.append(
            {
                "flag": "UPLOADER_ALREADY_COMMON_IN_WINNER_POOL",
                "winner_posts": uploader_winners,
                "uploader": uploader,
            }
        )
    if video_winners:
        flags.append(
            {
                "flag": "SOURCE_VIDEO_ALREADY_USED_BY_WINNERS",
                "winner_posts": video_winners,
                "source_video_id": video_id,
            }
        )
    if batch_source_candidate_count >= 5:
        flags.append(
            {
                "flag": "MANY_CANDIDATES_FROM_ONE_SOURCE",
                "candidate_count": batch_source_candidate_count,
                "source_video_id": video_id,
            }
        )
    if (
        batch_candidate_count >= 4
        and batch_uploader_candidate_count / batch_candidate_count > 0.25
    ):
        flags.append(
            {
                "flag": "BATCH_UPLOADER_CONCENTRATION",
                "candidate_count": batch_uploader_candidate_count,
                "batch_candidate_count": batch_candidate_count,
                "percentage": _round(
                    batch_uploader_candidate_count / batch_candidate_count * 100,
                    1,
                ),
                "uploader": uploader,
            }
        )
    return {
        "candidate": dict(candidate),
        "recommended_evidence_lane": analogue_lane(selected),
        "evidence_status": {
            "status": status,
            "reasons": status_reasons,
            "warning": (
                "Analogue evidence is associative and fixed at 24 hours. It "
                "does not isolate the hook, prove causality, or predict results."
            ),
        },
        "nearest_measured_analogues": selected,
        "hook_source_support": support,
        "portfolio_flags": flags,
        "decision": {
            "recommendation": decision,
            "confidence_or_evidence_status": confidence,
            "analogue_evidence_confidence": analogue_confidence,
            "confidence_scope": "comparison quality only",
            "outcome_prediction": None,
            "reason": reason,
        },
    }


def load_discriminator_rejections(
    source: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidate_file = Path(_text(source.get("candidate_file")))
    discriminator_path = (
        candidate_file.parent / "work" / "ai_candidate_discriminator.json"
    )
    if not discriminator_path.is_file():
        return (
            {
                "status": "DISCRIMINATOR_ARTIFACT_UNAVAILABLE",
                "artifact": str(discriminator_path),
                "artifact_sha256": None,
                "rejected_judgment_count": 0,
            },
            [],
        )
    payload = load_json_object(
        discriminator_path,
        label="AI candidate discriminator JSON",
    )
    metadata_path_text = _text(source.get("metadata_file"))
    metadata_path = Path(metadata_path_text) if metadata_path_text else None
    metadata = (
        load_json_object(metadata_path, label="Reel source metadata")
        if metadata_path is not None and metadata_path.is_file()
        else {}
    )
    video_id = _text(source.get("video_id"))
    source_url = _text(source.get("url"))
    rejected: list[dict[str, Any]] = []
    for position, raw in enumerate(
        _sequence(payload.get("judgments")), start=1
    ):
        judgment = _mapping(raw)
        if not judgment or bool(judgment.get("keep")):
            continue
        candidate = normalize_clip_candidate(
            judgment,
            position=position,
            video_id=video_id,
            source_url=source_url,
            metadata=metadata,
            config=config,
            origin="discriminator_rejection",
        )
        candidate["discriminator"] = {
            "keep": bool(judgment.get("keep")),
            "raw_keep": bool(judgment.get("raw_keep")),
            "reason": _text(judgment.get("reason")),
            "payoff": _text(judgment.get("payoff")),
        }
        rejected.append(candidate)
    return (
        {
            "status": "AUDITED",
            "artifact": str(discriminator_path.resolve()),
            "artifact_sha256": sha256_file(discriminator_path),
            "stage": _text(payload.get("stage")),
            "selection_profile": _text(payload.get("selection_profile")),
            "selection_profile_version": payload.get(
                "selection_profile_version"
            ),
            "prompt_lineage_sha256": _text(
                payload.get("prompt_lineage_sha256")
            ),
            "kept_count": int(_number(payload.get("kept_count")) or 0),
            "rejected_judgment_count": len(rejected),
        },
        rejected,
    )


def false_negative_verdict(
    evaluation: Mapping[str, Any],
) -> tuple[str, str]:
    candidate = _mapping(evaluation.get("candidate"))
    scores = _mapping(candidate.get("selection_scores"))
    decision = _text(
        _mapping(evaluation.get("decision")).get("recommendation")
    )
    evidence = _text(
        _mapping(evaluation.get("evidence_status")).get("status")
    )
    support = _mapping(evaluation.get("hook_source_support"))
    hard_mismatches = [
        _text(value)
        for value in _sequence(support.get("hard_mismatches"))
        if _text(value)
    ]
    hook_score = _number(scores.get("hook")) or 0
    value_score = _number(scores.get("value")) or 0
    overall_score = _number(scores.get("overall")) or 0
    opening_score = _number(scores.get("opening")) or 0
    if (
        not _text(candidate.get("hook"))
        or not _text(candidate.get("transcript"))
    ):
        return (
            "CANNOT_REVIEW",
            "The rejected judgment is missing a primary hook or transcript.",
        )
    if hard_mismatches:
        return (
            "REJECTION_SUPPORTED",
            "An exact entity, number, or artifact hook anchor is unsupported: "
            + ", ".join(hard_mismatches)
            + ".",
        )
    if decision == DECISION_REPLICATION:
        return (
            "HIGH_PRIORITY_FALSE_NEGATIVE_REVIEW",
            (
                "The rejected clip now clears the candidate gates and has a "
                "strong independent measured-analogue set. Human re-review is "
                "required before any promotion."
            ),
        )
    if (
        evidence in {"STRONG_ANALOGUE_SET", "DEVELOPING_ANALOGUE_SET"}
        and hook_score >= 7.0
    ):
        return (
            "POSSIBLE_FALSE_NEGATIVE",
            (
                f"The hook scored {hook_score:g} and retrieved {evidence.lower().replace('_', ' ')} "
                "despite the discriminator rejection."
            ),
        )
    if (
        hook_score >= 8.0
        and value_score >= 6.0
        and overall_score >= 6.0
        and opening_score >= 5.0
    ):
        return (
            "POSSIBLE_FALSE_NEGATIVE",
            (
                f"The rejected hook scored {hook_score:g}, value "
                f"{value_score:g}, opening {opening_score:g}. Its analogue "
                f"status is {evidence}; re-review it as a possible novel test "
                "instead of treating absent precedent as proof of failure."
            ),
        )
    return (
        "REJECTION_SUPPORTED",
        (
            "The winner-library comparison did not overcome the original "
            "quality/evidence weaknesses. This remains a review result, not "
            "proof that the clip could never work."
        ),
    )


def evaluate_false_negative_audit(
    source: Mapping[str, Any],
    *,
    winners: Sequence[Mapping[str, Any]],
    idf: Mapping[str, float],
    config: Mapping[str, Any],
    winner_uploader_counts: Mapping[str, int],
    winner_video_counts: Mapping[str, int],
) -> dict[str, Any]:
    audit_metadata, rejected = load_discriminator_rejections(source, config)
    if not rejected:
        return {
            **audit_metadata,
            "verdict_counts": {},
            "suspect_count": 0,
            "review_queue": [],
            "reviewed_rejections": [],
        }
    evaluations = [
        evaluate_candidate(
            candidate,
            winners,
            idf=idf,
            config=config,
            winner_uploader_counts=winner_uploader_counts,
            winner_video_counts=winner_video_counts,
            batch_source_candidate_count=len(rejected),
            batch_uploader_candidate_count=len(rejected),
            batch_candidate_count=len(rejected),
        )
        for candidate in rejected
    ]
    verdict_counts: Counter[str] = Counter()
    for evaluation in evaluations:
        verdict, reason = false_negative_verdict(evaluation)
        evaluation["false_negative_review"] = {
            "verdict": verdict,
            "reason": reason,
            "automatic_promotion": False,
            "warning": (
                "A review flag never changes candidates.json and never restores "
                "a rejected clip automatically."
            ),
        }
        verdict_counts[verdict] += 1
    verdict_priority = {
        "HIGH_PRIORITY_FALSE_NEGATIVE_REVIEW": 0,
        "POSSIBLE_FALSE_NEGATIVE": 1,
        "CANNOT_REVIEW": 2,
        "REJECTION_SUPPORTED": 3,
    }
    evidence_priority = {
        "STRONG_ANALOGUE_SET": 0,
        "DEVELOPING_ANALOGUE_SET": 1,
        "THIN_ANALOGUE_SET": 2,
        "NO_RELEVANT_ANALOGUES": 3,
    }
    evaluations.sort(
        key=lambda evaluation: (
            verdict_priority.get(
                _text(
                    _mapping(evaluation.get("false_negative_review")).get(
                        "verdict"
                    )
                ),
                99,
            ),
            evidence_priority.get(
                _text(
                    _mapping(evaluation.get("evidence_status")).get("status")
                ),
                99,
            ),
            -(
                _number(
                    _mapping(
                        _mapping(evaluation.get("candidate")).get(
                            "selection_scores"
                        )
                    ).get("hook")
                )
                or 0
            ),
            _text(
                _mapping(evaluation.get("candidate")).get("candidate_id")
            ),
        )
    )
    review_limit = int(
        _number(config.get("false_negative_review_limit")) or 10
    )
    suspects = [
        evaluation
        for evaluation in evaluations
        if _text(
            _mapping(evaluation.get("false_negative_review")).get("verdict")
        )
        in {
            "HIGH_PRIORITY_FALSE_NEGATIVE_REVIEW",
            "POSSIBLE_FALSE_NEGATIVE",
        }
    ]
    compact = []
    for evaluation in evaluations:
        candidate = _mapping(evaluation.get("candidate"))
        analogues = [
            _mapping(value)
            for value in _sequence(
                evaluation.get("nearest_measured_analogues")
            )
        ]
        closest = analogues[0] if analogues else {}
        compact.append(
            {
                "candidate_id": _text(candidate.get("candidate_id")),
                "index": candidate.get("index"),
                "hook": _text(candidate.get("hook")),
                "source_timestamp_url": _text(
                    candidate.get("source_timestamp_url")
                ),
                "selection_scores": dict(
                    _mapping(candidate.get("selection_scores"))
                ),
                "discriminator_reason": _text(
                    _mapping(candidate.get("discriminator")).get("reason")
                ),
                "evidence_status": _text(
                    _mapping(evaluation.get("evidence_status")).get("status")
                ),
                "verdict": _text(
                    _mapping(evaluation.get("false_negative_review")).get(
                        "verdict"
                    )
                ),
                "verdict_reason": _text(
                    _mapping(evaluation.get("false_negative_review")).get(
                        "reason"
                    )
                ),
                "closest_winner": {
                    "published_hook": _text(
                        closest.get("published_hook")
                    ),
                    "permalink": _text(closest.get("permalink")),
                    "relevant": bool(
                        _mapping(closest.get("comparison")).get("relevant")
                    ),
                    "close_for_replication": bool(
                        _mapping(closest.get("comparison")).get(
                            "close_for_replication"
                        )
                    ),
                },
            }
        )
    return {
        **audit_metadata,
        "verdict_counts": dict(verdict_counts),
        "suspect_count": len(suspects),
        "review_queue": compact[:review_limit],
        "reviewed_rejections": compact,
        "suspect_evaluations": suspects,
        "automatic_promotions": 0,
    }


def build_candidate_evaluation(
    candidate_paths: Sequence[Path],
    winner_library: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    winner_library_path: Path | None = None,
) -> dict[str, Any]:
    sources = [
        normalize_candidate_source(path.expanduser().resolve(), config)
        for path in candidate_paths
    ]
    return build_candidate_evaluation_from_sources(
        sources,
        winner_library,
        config,
        winner_library_path=winner_library_path,
    )


def build_candidate_evaluation_from_sources(
    sources: Sequence[dict[str, Any]],
    winner_library: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    winner_library_path: Path | None = None,
) -> dict[str, Any]:
    """Evaluate already-normalized sources with the standard candidate rules.

    This additive entry point lets other local pipelines, such as the scheduled
    Reel ledger, reuse the exact same retrieval and decision machinery without
    manufacturing temporary ``candidates.json`` files.
    """

    metadata = _mapping(winner_library.get("library_metadata"))
    if metadata.get("schema_version") != 1:
        raise ValueError(
            f"Unsupported winner library schema: {metadata.get('schema_version')!r}"
        )
    maturity_window = _text(metadata.get("maturity_window"))
    if maturity_window != "24h":
        raise ValueError(
            f"Candidate evaluation requires a 24h winner library, got {maturity_window!r}"
        )
    sources = list(sources)
    batch_candidate_count = sum(source["candidate_count"] for source in sources)
    batch_uploader_counts: Counter[str] = Counter()
    for source in sources:
        uploader = _text(source.get("uploader"))
        if uploader:
            batch_uploader_counts[uploader] += int(source["candidate_count"])
    winner_posts = [
        normalize_winner(_mapping(raw), config)
        for raw in _sequence(winner_library.get("winners"))
        if _mapping(raw)
    ]
    winners_by_script: dict[str, dict[str, Any]] = {}
    for winner in winner_posts:
        script_key = _text(winner.get("script_asset_id")) or (
            f"media:{_text(winner.get('media_id'))}"
        )
        if script_key not in winners_by_script:
            winners_by_script[script_key] = winner
    winners = list(winners_by_script.values())
    winner_documents = [_text(winner.get("_match_text")) for winner in winners]
    idf = inverse_document_frequencies(winner_documents)
    uploader_counts: Counter[str] = Counter(
        _text(winner.get("source_uploader"))
        for winner in winners
        if _text(winner.get("source_uploader"))
    )
    video_counts: Counter[str] = Counter(
        _text(winner.get("source_video_id"))
        for winner in winners
        if _text(winner.get("source_video_id"))
    )
    evaluated_sources: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    candidate_total = 0
    rejected_judgment_total = 0
    false_negative_suspect_total = 0
    for source in sources:
        evaluations = [
            evaluate_candidate(
                candidate,
                winners,
                idf=idf,
                config=config,
                winner_uploader_counts=uploader_counts,
                winner_video_counts=video_counts,
                batch_source_candidate_count=source["candidate_count"],
                batch_uploader_candidate_count=batch_uploader_counts.get(
                    _text(source.get("uploader")), 0
                ),
                batch_candidate_count=batch_candidate_count,
            )
            for candidate in source["candidates"]
        ]
        apply_diversity_holds(evaluations, config)
        source["evaluations"] = evaluations
        source.pop("candidates", None)
        source["decision_counts"] = dict(
            Counter(
                evaluation["decision"]["recommendation"]
                for evaluation in evaluations
            )
        )
        if source["status"] == "NO_RECONCILED_CANDIDATES":
            false_negative_audit = evaluate_false_negative_audit(
                source,
                winners=winners,
                idf=idf,
                config=config,
                winner_uploader_counts=uploader_counts,
                winner_video_counts=video_counts,
            )
            source["false_negative_audit"] = false_negative_audit
            rejected_judgment_total += int(
                _number(
                    false_negative_audit.get("rejected_judgment_count")
                )
                or 0
            )
            false_negative_suspect_total += int(
                _number(false_negative_audit.get("suspect_count")) or 0
            )
        decision_counts.update(source["decision_counts"])
        candidate_total += len(evaluations)
        evaluated_sources.append(source)
    decision_priority = {
        DECISION_REPLICATION: 0,
        DECISION_NOVEL: 1,
        DECISION_REVISE: 2,
        DECISION_HOLD: 3,
        DECISION_INSUFFICIENT: 4,
    }
    evidence_priority = {
        "STRONG_ANALOGUE_SET": 0,
        "DEVELOPING_ANALOGUE_SET": 1,
        "THIN_ANALOGUE_SET": 2,
        "NO_RELEVANT_ANALOGUES": 3,
    }
    review_entries: list[dict[str, Any]] = []
    for source in evaluated_sources:
        for evaluation in source["evaluations"]:
            candidate = _mapping(evaluation.get("candidate"))
            review_entries.append(
                {
                    "candidate_id": _text(candidate.get("candidate_id")),
                    "source_video_id": _text(source.get("video_id")),
                    "hook": _text(candidate.get("hook")),
                    "source_timestamp_url": _text(
                        candidate.get("source_timestamp_url")
                    ),
                    "decision": _text(
                        _mapping(evaluation.get("decision")).get(
                            "recommendation"
                        )
                    ),
                    "evidence_status": _text(
                        _mapping(evaluation.get("evidence_status")).get("status")
                    ),
                    "selection_overall": _number(
                        _mapping(candidate.get("selection_scores")).get(
                            "overall"
                        )
                    ),
                    "reason": _text(
                        _mapping(evaluation.get("decision")).get("reason")
                    ),
                }
            )
    review_entries.sort(
        key=lambda entry: (
            decision_priority.get(entry["decision"], 99),
            evidence_priority.get(entry["evidence_status"], 99),
            -(_number(entry.get("selection_overall")) or 0),
            entry["candidate_id"],
        )
    )
    for position, entry in enumerate(review_entries, start=1):
        entry["review_order"] = position
        entry["ordering_basis"] = (
            "decision gate, analogue-evidence status, existing selection-model "
            "overall score, stable candidate ID"
        )
    for winner in winners:
        winner.pop("_match_text", None)
    generated_at = _text(metadata.get("generated_at"))
    return {
        "report_metadata": {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "account": _text(metadata.get("account")),
            "platform": _text(metadata.get("platform")),
            "maturity_window": maturity_window,
            "winner_library": (
                str(winner_library_path.resolve())
                if winner_library_path is not None
                else None
            ),
            "winner_library_sha256": (
                sha256_file(winner_library_path)
                if winner_library_path is not None and winner_library_path.is_file()
                else None
            ),
            "winner_post_count": len(winner_posts),
            "winner_script_asset_count": len(winners),
            "candidate_source_count": len(evaluated_sources),
            "candidate_count": candidate_total,
            "evidence_boundary": (
                "This report retrieves measured analogues and applies transparent "
                "screening rules. It does not predict a winner, establish "
                "causality, localize hooks, render Reels, schedule, or publish."
            ),
        },
        "methodology": {
            "analogue_retrieval": [
                "Compare English source hooks, hook variants, chapters, and transcripts.",
                "Order by shared named entities, shared topic concepts, keyword cosine, hook patterns, duration, then evidence tier.",
                "Expose every retrieval component; do not create a combined performance score.",
                "Require same fixed 24-hour maturity evidence for winner metrics.",
            ],
            "decision_rules": {
                DECISION_REPLICATION: (
                    "Candidate clears source-quality checks and has a strong "
                    "analogue set containing balanced or cross-family evidence."
                ),
                DECISION_NOVEL: (
                    "Candidate clears source-quality checks but measured analogue "
                    "evidence is developing, thin, or absent."
                ),
                DECISION_REVISE: (
                    "Candidate misses an inspectable hook, value, opening, or "
                    "exact source-anchor threshold."
                ),
                DECISION_HOLD: (
                    "A higher-priority candidate in the same source batch covers "
                    "substantially the same entity/topic concepts."
                ),
                DECISION_INSUFFICIENT: (
                    "Required hook or transcript evidence is unavailable."
                ),
            },
            "empty_batch_false_negative_audit": {
                "trigger": (
                    "Run only when reconciled candidates.json contains no clips."
                ),
                "source": (
                    "work/ai_candidate_discriminator.json judgments with "
                    "keep=false"
                ),
                "review_flags": [
                    "HIGH_PRIORITY_FALSE_NEGATIVE_REVIEW",
                    "POSSIBLE_FALSE_NEGATIVE",
                    "CANNOT_REVIEW",
                    "REJECTION_SUPPORTED",
                ],
                "automatic_promotion": False,
            },
            "config": dict(config),
        },
        "summary": {
            "candidate_sources": len(evaluated_sources),
            "sources_with_candidates": sum(
                source["status"] == "AVAILABLE" for source in evaluated_sources
            ),
            "empty_sources": sum(
                source["status"] == "NO_RECONCILED_CANDIDATES"
                for source in evaluated_sources
            ),
            "candidates": candidate_total,
            "decision_counts": dict(decision_counts),
            "candidate_uploader_counts": dict(batch_uploader_counts),
            "empty_batch_rejected_judgments_audited": (
                rejected_judgment_total
            ),
            "possible_false_negatives": false_negative_suspect_total,
            "review_queue": review_entries,
            "review_queue_warning": (
                "This is review order, not predicted performance. Existing "
                "selection-model scores are used only after the decision and "
                "analogue-evidence gates."
            ),
        },
        "sources": evaluated_sources,
    }


def _escape_cell(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("\n", "<br>")


def _format_number(value: Any, digits: int = 2) -> str:
    number = _number(value)
    if number is None:
        return "Unavailable"
    return f"{number:.{digits}f}"


def _metric_display(key: str, value: Any) -> str:
    number = _number(value)
    if number is None:
        return "Unavailable"
    if key in {"total_interactions_per_reach", "watch_depth"}:
        return f"{number * 100:.1f}%"
    if key == "three_second_skip_rate":
        return f"{number:.1f}%"
    if key == "saves_per_1000_reach":
        return f"{number:.1f}/1k"
    if key == "views_per_reached_account":
        return f"{number:.2f}×"
    return f"{number:.2f}"


def _metric_evidence_text(key: str, metric_value: Any) -> str:
    metric = _mapping(metric_value)
    value = metric.get("value")
    support = _mapping(metric.get("supporting_metrics"))
    display = _metric_display(key, value)
    if key == "total_interactions_per_reach":
        raw = (
            f"{_format_number(support.get('interactions'), 0)}/"
            f"{_format_number(support.get('reach'), 0)} reach"
            if _number(support.get("interactions")) is not None
            and _number(support.get("reach")) is not None
            else ""
        )
    elif key == "watch_depth":
        raw = (
            f"{_format_number(support.get('average_watch_time_seconds'), 1)}s/"
            f"{_format_number(support.get('duration_seconds'), 1)}s"
            if _number(support.get("average_watch_time_seconds")) is not None
            and _number(support.get("duration_seconds")) is not None
            else ""
        )
    elif key == "three_second_skip_rate":
        raw = (
            f"source={_format_number(support.get('reels_skip_rate'), 1)}%"
            if _number(support.get("reels_skip_rate")) is not None
            else ""
        )
    elif key == "saves_per_1000_reach":
        raw = (
            f"{_format_number(support.get('saves'), 0)}/"
            f"{_format_number(support.get('reach'), 0)} reach"
            if _number(support.get("saves")) is not None
            and _number(support.get("reach")) is not None
            else ""
        )
    elif key == "views_per_reached_account":
        raw = (
            f"{_format_number(support.get('views'), 0)}/"
            f"{_format_number(support.get('reach'), 0)} reach"
            if _number(support.get("views")) is not None
            and _number(support.get("reach")) is not None
            else ""
        )
    else:
        raw = ""
    return f"{display} ({raw})" if raw else display


def _analogue_strengths(analogue: Mapping[str, Any]) -> str:
    memberships = [
        f"{_text(row.get('label'))} #{int(_number(row.get('rank')) or 0)}"
        for row in _sequence(analogue.get("ranking_memberships"))
        if _mapping(row)
    ]
    aggregate_rank = _number(analogue.get("aggregate_rank"))
    if aggregate_rank is not None:
        memberships.append(f"Aggregate #{int(aggregate_rank)}")
    return ", ".join(memberships) or "Measured Top 10"


def render_candidate_evaluation_markdown(report: Mapping[str, Any]) -> str:
    meta = _mapping(report.get("report_metadata"))
    summary = _mapping(report.get("summary"))
    lines = [
        "# AI Brief JP — candidate rejudgment against measured winners",
        "",
        f"Evidence snapshot: **{_text(meta.get('generated_at')) or 'Unavailable'}**  ",
        f"Winner maturity window: **{_text(meta.get('maturity_window'))}**  ",
        (
            f"Inputs: **{int(_number(meta.get('candidate_source_count')) or 0)}** "
            f"source folders, **{int(_number(meta.get('candidate_count')) or 0)}** "
            f"candidates, **{int(_number(meta.get('winner_post_count')) or 0)}** "
            "measured winner posts "
            f"(**{int(_number(meta.get('winner_script_asset_count')) or 0)}** "
            "unique script assets used for analogue retrieval)."
        ),
        "",
        "## Evidence boundary",
        "",
        f"- {_text(meta.get('evidence_boundary'))}",
        "- Candidate hooks and transcripts are still English source selections, not final Japanese rendered copy.",
        "- Similarity retrieves analogues; it is not a performance score.",
        "- Winner metrics are associative evidence at 24 hours, not proof that a hook caused the result.",
        "",
        "## Portfolio summary",
        "",
        "| Decision | Count |",
        "|---|---:|",
    ]
    decision_counts = _mapping(summary.get("decision_counts"))
    for decision in (
        DECISION_REPLICATION,
        DECISION_NOVEL,
        DECISION_REVISE,
        DECISION_HOLD,
        DECISION_INSUFFICIENT,
    ):
        lines.append(f"| {decision} | {int(_number(decision_counts.get(decision)) or 0)} |")
    rejected_audited = int(
        _number(summary.get("empty_batch_rejected_judgments_audited")) or 0
    )
    possible_false_negatives = int(
        _number(summary.get("possible_false_negatives")) or 0
    )
    if rejected_audited:
        lines.extend(
            [
                "",
                (
                    f"Empty-batch audit: **{rejected_audited}** rejected "
                    f"discriminator judgments checked; "
                    f"**{possible_false_negatives}** require false-negative "
                    "review. No rejected candidate was restored automatically."
                ),
            ]
        )
    review_queue = [
        _mapping(value) for value in _sequence(summary.get("review_queue"))
    ]
    if review_queue:
        lines.extend(
            [
                "",
                "## Suggested review order",
                "",
                f"> {_text(summary.get('review_queue_warning'))}",
                "",
                "| # | Candidate | Decision | Analogue evidence | Existing selector score |",
                "|---:|---|---|---|---:|",
            ]
        )
        for entry in review_queue:
            hook = _escape_cell(entry.get("hook"))
            url = _text(entry.get("source_timestamp_url"))
            hook_link = f"[{hook}]({url})" if url else hook
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(int(_number(entry.get("review_order")) or 0)),
                        hook_link,
                        _escape_cell(entry.get("decision")),
                        _escape_cell(entry.get("evidence_status")),
                        _format_number(entry.get("selection_overall"), 1),
                    ]
                )
                + " |"
            )
    for source in _sequence(report.get("sources")):
        source = _mapping(source)
        lines.extend(
            [
                "",
                f"## {_text(source.get('video_id'))} — {_text(source.get('title')) or 'Untitled source'}",
                "",
                f"- Source: [{_text(source.get('uploader')) or 'Open source'}]({_text(source.get('url'))})",
                f"- Candidate file: `{_text(source.get('candidate_file'))}`",
                f"- Status: **{_text(source.get('status'))}**; candidates: **{int(_number(source.get('candidate_count')) or 0)}**",
            ]
        )
        evaluations = [
            _mapping(value) for value in _sequence(source.get("evaluations"))
        ]
        if not evaluations:
            lines.extend(
                [
                    "",
                    "> No candidates were present in this folder's `clips` array, so no judgment was fabricated.",
                ]
            )
            audit = _mapping(source.get("false_negative_audit"))
            if _text(audit.get("status")) == "AUDITED":
                lines.extend(
                    [
                        "",
                        "### Empty-batch false-negative audit",
                        "",
                        f"- Discriminator artifact: `{_text(audit.get('artifact'))}`",
                        (
                            f"- Rejected judgments checked: "
                            f"**{int(_number(audit.get('rejected_judgment_count')) or 0)}**"
                        ),
                        (
                            f"- Possible false negatives requiring review: "
                            f"**{int(_number(audit.get('suspect_count')) or 0)}**"
                        ),
                        "- Automatic promotions: **0**",
                        "",
                        "| Verdict | Rejected hook | Original scores | Winner evidence | Closest measured Reel | Audit reason |",
                        "|---|---|---|---|---|---|",
                    ]
                )
                for row in _sequence(audit.get("review_queue")):
                    row = _mapping(row)
                    hook = _escape_cell(row.get("hook"))
                    source_url = _text(row.get("source_timestamp_url"))
                    hook_link = (
                        f"[{hook}]({source_url})" if source_url else hook
                    )
                    scores = _mapping(row.get("selection_scores"))
                    closest = _mapping(row.get("closest_winner"))
                    closest_hook = _escape_cell(
                        closest.get("published_hook")
                    )
                    closest_url = _text(closest.get("permalink"))
                    closest_prefix = (
                        ""
                        if bool(closest.get("relevant"))
                        else "Fallback: "
                    )
                    closest_link = (
                        closest_prefix
                        + f"[{closest_hook}]({closest_url})"
                        if closest_url
                        else "Unavailable"
                    )
                    score_text = (
                        f"overall {_format_number(scores.get('overall'), 1)}; "
                        f"hook {_format_number(scores.get('hook'), 1)}; "
                        f"value {_format_number(scores.get('value'), 1)}; "
                        f"opening {_format_number(scores.get('opening'), 1)}"
                    )
                    lines.append(
                        "| "
                        + " | ".join(
                            [
                                _escape_cell(row.get("verdict")),
                                hook_link,
                                _escape_cell(score_text),
                                _escape_cell(row.get("evidence_status")),
                                closest_link,
                                _escape_cell(row.get("verdict_reason")),
                            ]
                        )
                        + " |"
                    )
                lines.extend(
                    [
                        "",
                        (
                            "> These are review flags only. The source "
                            "`candidates.json` remains empty and rejected work "
                            "items remain rejected until a person explicitly "
                            "reviews them."
                        ),
                    ]
                )
            elif audit:
                lines.extend(
                    [
                        "",
                        "### Empty-batch false-negative audit",
                        "",
                        (
                            f"> {_text(audit.get('status'))}: "
                            f"`{_text(audit.get('artifact'))}`"
                        ),
                    ]
                )
            continue
        lines.extend(
            [
                "",
                "| Candidate | Decision | Lane | Evidence | Closest measured Reel |",
                "|---|---|---|---|---|",
            ]
        )
        for evaluation in evaluations:
            candidate = _mapping(evaluation.get("candidate"))
            decision = _mapping(evaluation.get("decision"))
            lane = _mapping(evaluation.get("recommended_evidence_lane"))
            evidence = _mapping(evaluation.get("evidence_status"))
            analogues = [
                _mapping(value)
                for value in _sequence(evaluation.get("nearest_measured_analogues"))
            ]
            closest = analogues[0] if analogues else {}
            closest_link = (
                (
                    ""
                    if bool(
                        _mapping(closest.get("comparison")).get("relevant")
                    )
                    else "Fallback: "
                )
                + f"[{_escape_cell(closest.get('published_hook'))}]"
                f"({_text(closest.get('permalink'))})"
                if closest
                else "Unavailable"
            )
            source_link = _text(candidate.get("source_timestamp_url"))
            hook = _escape_cell(candidate.get("hook"))
            hook_cell = f"[{hook}]({source_link})" if source_link else hook
            lines.append(
                "| "
                + " | ".join(
                    [
                        hook_cell,
                        _escape_cell(decision.get("recommendation")),
                        _escape_cell(lane.get("recommended_lane")),
                        _escape_cell(evidence.get("status")),
                        closest_link,
                    ]
                )
                + " |"
            )
        for evaluation in evaluations:
            candidate = _mapping(evaluation.get("candidate"))
            decision = _mapping(evaluation.get("decision"))
            lane = _mapping(evaluation.get("recommended_evidence_lane"))
            evidence = _mapping(evaluation.get("evidence_status"))
            support = _mapping(evaluation.get("hook_source_support"))
            scores = _mapping(candidate.get("selection_scores"))
            lines.extend(
                [
                    "",
                    "<details>",
                    (
                        f"<summary><strong>{_escape_cell(decision.get('recommendation'))}</strong> "
                        f"· {_escape_cell(candidate.get('hook'))}</summary>"
                    ),
                    "",
                    f"- Candidate ID: `{_text(candidate.get('candidate_id'))}`",
                    f"- Source moment: [{_format_number(candidate.get('start_seconds'), 0)}s]({_text(candidate.get('source_timestamp_url'))})",
                    f"- Duration: {_format_number(candidate.get('duration_seconds'), 1)}s ({_text(candidate.get('duration_bucket')) or 'Unavailable'})",
                    (
                        "- Selection-model scores: "
                        f"overall {_format_number(scores.get('overall'), 1)}; "
                        f"hook {_format_number(scores.get('hook'), 1)}; "
                        f"value {_format_number(scores.get('value'), 1)}; "
                        f"opening {_format_number(scores.get('opening'), 1)}. "
                        "These are generation-pipeline priors, not measured account results."
                    ),
                    "",
                    "### Judgment",
                    "",
                    f"- Recommended lane: **{_text(lane.get('recommended_lane'))}**",
                    f"- Evidence status: **{_text(evidence.get('status'))}**",
                    (
                        "- Analogue evidence confidence: "
                        f"**{_text(decision.get('analogue_evidence_confidence'))}** "
                        "(comparison quality only; outcome prediction is null)"
                    ),
                    f"- Decision: **{_text(decision.get('recommendation'))}**",
                    f"- Reason: {_text(decision.get('reason'))}",
                    (
                        f"- Exact hook-anchor screen: "
                        + (
                            f"{_number(support.get('coverage')) * 100:.1f}%"
                            if _number(support.get("coverage")) is not None
                            else "no exact entity/number/artifact anchors"
                        )
                        + "; unsupported anchors: "
                        + (
                            ", ".join(
                                _text(value)
                                for value in _sequence(
                                    support.get("unsupported_anchors")
                                )
                            )
                            or "none"
                        )
                        + ". Semantic claim review remains required."
                    ),
                    "",
                    "### Nearest measured 24-hour analogues",
                    "",
                    "| Reel | Evidence | Shared basis | Duration | 24h metrics | Flags |",
                    "|---|---|---|---:|---|---|",
                ]
            )
            for analogue in _sequence(
                evaluation.get("nearest_measured_analogues")
            ):
                analogue = _mapping(analogue)
                comparison = _mapping(analogue.get("comparison"))
                metrics = _mapping(analogue.get("metrics_24h"))
                basis_parts = []
                for label, key in (
                    ("entities", "shared_entities"),
                    ("topics", "shared_topics"),
                    ("patterns", "shared_hook_patterns"),
                    ("keywords", "shared_keywords"),
                ):
                    values = [_text(value) for value in _sequence(comparison.get(key))]
                    if values:
                        basis_parts.append(f"{label}: {', '.join(values)}")
                basis_parts.append(
                    f"lexical {_format_number(comparison.get('keyword_cosine'), 3)}"
                )
                basis_parts.append(
                    "relevant "
                    + (
                        "yes"
                        if bool(comparison.get("relevant"))
                        else "no (retrieval fallback)"
                    )
                )
                basis_parts.append(
                    "close for replication "
                    + (
                        "yes"
                        if bool(comparison.get("close_for_replication"))
                        else "no"
                    )
                )
                metric_text = "; ".join(
                    [
                        f"interactions {_metric_evidence_text('total_interactions_per_reach', metrics.get('total_interactions_per_reach'))}",
                        f"watch {_metric_evidence_text('watch_depth', metrics.get('watch_depth'))}",
                        f"skip {_metric_evidence_text('three_second_skip_rate', metrics.get('three_second_skip_rate'))}",
                        f"saves {_metric_evidence_text('saves_per_1000_reach', metrics.get('saves_per_1000_reach'))}",
                        f"views/reach {_metric_evidence_text('views_per_reached_account', metrics.get('views_per_reached_account'))}",
                    ]
                )
                link = (
                    f"[{_escape_cell(analogue.get('published_hook'))}]"
                    f"({_text(analogue.get('permalink'))})"
                )
                flags = ", ".join(
                    _text(value)
                    for value in _sequence(analogue.get("evidence_flags"))
                ) or "—"
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            link,
                            _escape_cell(
                                f"{analogue.get('analogue_role')}; "
                                f"{analogue.get('tier')}; "
                                f"{_analogue_strengths(analogue)}; "
                                f"actual age {_format_number(analogue.get('actual_age_hours'), 2)}h"
                            ),
                            _escape_cell("; ".join(basis_parts)),
                            _format_number(analogue.get("duration_seconds"), 1),
                            _escape_cell(metric_text),
                            _escape_cell(flags),
                        ]
                    )
                    + " |"
                )
            flags = [
                _mapping(value) for value in _sequence(evaluation.get("portfolio_flags"))
            ]
            lines.extend(
                [
                    "",
                    "### Candidate source material",
                    "",
                    f"**Selection rationale:** {_text(candidate.get('reason'))}",
                    "",
                    f"**Source transcript:** {_text(candidate.get('transcript'))}",
                    "",
                    (
                        "**Portfolio flags:** "
                        + (
                            "; ".join(
                                f"{_text(flag.get('flag'))}: "
                                + ", ".join(
                                    f"{key}={value}"
                                    for key, value in flag.items()
                                    if key != "flag"
                                )
                                for flag in flags
                            )
                            if flags
                            else "None"
                        )
                    ),
                    "",
                    "</details>",
                ]
            )
    lines.extend(
        [
            "",
            "## How to use these judgments",
            "",
            "1. Review `ADVANCE AS REPLICATION TEST` candidates first, but keep them as controlled tests.",
            "2. Use `ADVANCE AS NOVEL TEST` to reserve exploration slots rather than treating weak analogue coverage as failure.",
            "3. Rewrite `REVISE` hooks or openings, then rerun this evaluator.",
            "4. Use `HOLD FOR DIVERSITY` to prevent one source or concept cluster from consuming the portfolio.",
            "5. Confirm claims and Japanese localization before rendering; this report does neither.",
            "",
        ]
    )
    return "\n".join(lines)


def render_candidate_evaluation_json(report: Mapping[str, Any]) -> str:
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
