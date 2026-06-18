#!/usr/bin/env python3
"""Verification gates for weekly carousel copy."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

Verdict = str

KNOWN_ENTITIES = [
    "Anthropic",
    "OpenAI",
    "Codex",
    "Claude",
    "Claude Corps",
    "Fable 5",
    "Mythos 5",
    "Google DeepMind",
    "DeepMind",
    "Schmidt Sciences",
    "Cooperative AI Foundation",
    "ARIA",
    "Google.org",
    "Dario Amodei",
    "ダリオ・アモデイ",
    "UBI",
]

CLAIM_ALIASES = {
    "6月12日": ["June 12", "Jun 12", "2026-06-12", "5:21pm"],
    "1.5億ドル": ["$150m", "$150 million", "150m", "150 million"],
    "8.5万ドル": ["$85,000", "85,000", "85000"],
    "1,000人": ["1,000 people", "1,000 fellows", "1000 people", "1000 fellows"],
    "400以上": ["at least 400", "400 nonprofits", "400+"],
    "1,000万ドル": ["$10M", "$10 million", "10M", "10 million", "up to $10M"],
    "8月8日": ["August 8", "August 8, 2026"],
    "30日": ["30 days", "30-day", "30 day"],
    "2億ドル": ["$200 million", "$200m", "200 million"],
    "5%": ["5%"],
    "10%": ["10%"],
    "ダリオ・アモデイ": ["Dario Amodei"],
    "UBI": ["basic income"],
}

NUMERIC_CLAIM_RE = re.compile(
    r"(?:\d{1,2}月\d{1,2}日)|"
    r"(?:\d[\d,]*(?:\.\d+)?(?:億|万)?(?:ドル|人|日|月|年|%|％|件|社|つ|時間|分|秒)?)"
)
LATIN_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9&.]+(?:\s+[A-Z][A-Za-z0-9&.]+){0,4}\b")


@dataclass
class SlideRecord:
    slide: int
    label: str
    headline: str
    body: str
    category: str
    source_url: str
    source_text: str = ""
    source_name: str = ""
    claims: list[str] = field(default_factory=list)
    verdict: Verdict = "needs_review"
    notes: list[str] = field(default_factory=list)


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.lower()
    return re.sub(r"[\s、。,.。:：;；'\"“”‘’()（）\[\]{}<>・/\\_-]+", "", normalized)


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = normalize_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def extract_claims(text: str) -> list[str]:
    claims: list[str] = []
    claims.extend(match.group(0) for match in NUMERIC_CLAIM_RE.finditer(text or ""))
    for entity in KNOWN_ENTITIES:
        if entity in (text or ""):
            claims.append(entity)
    for match in LATIN_ENTITY_RE.finditer(text or ""):
        token = match.group(0)
        if token.upper() in {"AI", "CEO", "US", "UK", "SEC"}:
            continue
        claims.append(token)
    return _dedupe_preserve_order(claims)


def _claim_variants(claim: str) -> list[str]:
    variants = [claim]
    variants.extend(CLAIM_ALIASES.get(claim, []))
    if "," in claim:
        variants.append(claim.replace(",", ""))
    return variants


def claim_has_evidence(claim: str, source_text: str) -> bool:
    source_norm = normalize_text(source_text)
    return any(normalize_text(variant) in source_norm for variant in _claim_variants(claim))


def entity_type_issues(record: SlideRecord) -> list[str]:
    text = f"{record.headline} {record.body}"
    source_norm = normalize_text(record.source_text)
    issues: list[str] = []
    for model in ("Fable", "Fable 5", "Mythos", "Mythos 5"):
        if f"{model}社" in text:
            issues.append(f"{model} is treated as a company")
    if "閉鎖" in text and any(token in source_norm for token in ("suspend", "disable", "停止", "access")):
        issues.append("copy says closed when source says access was suspended or disabled")
    return issues


def specificity_issues(record: SlideRecord) -> list[str]:
    body_norm = normalize_text(record.body)
    headline_norm = normalize_text(record.headline)
    body_claims = extract_claims(record.body)
    if not body_claims:
        return ["body has no source-verifiable specific"]
    if body_norm and headline_norm and (body_norm == headline_norm or body_norm in headline_norm):
        return ["body only restates the headline"]
    return []


def verify_record(record: SlideRecord) -> SlideRecord:
    record.claims = record.claims or extract_claims(f"{record.headline} {record.body}")
    notes: list[str] = []
    blocked = False

    if not record.source_url:
        notes.append("missing source_url")
        blocked = True
    if not record.source_text:
        notes.append("missing source_text")
        blocked = True

    issues = entity_type_issues(record)
    if issues:
        notes.extend(issues)
        blocked = True

    number_claims = [claim for claim in record.claims if NUMERIC_CLAIM_RE.fullmatch(claim)]
    missing_numbers = [
        claim for claim in number_claims if record.source_text and not claim_has_evidence(claim, record.source_text)
    ]
    if missing_numbers:
        notes.append("untraced numeric claim: " + ", ".join(missing_numbers))
        blocked = True

    missing_entities = [
        claim
        for claim in record.claims
        if claim not in number_claims and record.source_text and not claim_has_evidence(claim, record.source_text)
    ]
    if missing_entities:
        notes.append("untraced named entity: " + ", ".join(missing_entities))
        blocked = True

    specific_issues = specificity_issues(record)
    if specific_issues:
        notes.extend(specific_issues)
        blocked = True

    record.notes = notes
    record.verdict = "blocked" if blocked else "verified"
    return record


def verify_records(records: list[SlideRecord]) -> list[SlideRecord]:
    return [verify_record(record) for record in records]


def blocked_records(records: list[SlideRecord]) -> list[SlideRecord]:
    return [record for record in records if record.verdict == "blocked"]


def assert_no_blocked(records: list[SlideRecord]) -> None:
    blocked = blocked_records(records)
    if not blocked:
        return
    lines = [f"slide {record.slide} {record.label}: {'; '.join(record.notes)}" for record in blocked]
    raise SystemExit("weekly verification blocked render:\n" + "\n".join(lines))


def manifest_record(record: SlideRecord) -> dict[str, Any]:
    return {
        "slide": record.slide,
        "label": record.label,
        "headline": record.headline,
        "body": record.body,
        "category": record.category,
        "source_url": record.source_url,
        "source_name": record.source_name,
        "claims": record.claims,
        "verdict": record.verdict,
        "verified": record.verdict == "verified",
        "notes": record.notes,
        "source_excerpt": (record.source_text or "")[:320],
    }


def write_run_manifest(
    path: Path,
    records: list[SlideRecord],
    *,
    meta: dict[str, Any] | None = None,
    extra_slides: list[dict[str, Any]] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    slide_rows = [manifest_record(record) for record in records]
    if extra_slides:
        slide_rows = extra_slides[:1] + slide_rows + extra_slides[1:]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta or {},
        "summary": {
            "slide_records": len(slide_rows),
            "news_records": len(records),
            "blocked": len(blocked_records(records)),
        },
        "slides": slide_rows,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
