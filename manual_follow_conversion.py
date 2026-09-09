"""Read and display a separate, user-supplied follow-conversion record.

Add a new object to the JSON ``observations`` list for each supplied snapshot;
keep the same creative_id and do not replace an older object. ``recorded_at`` is
when the entry was recorded, not when Instagram measured it. Leave observed_at
null unless its measurement date is known. Latest means latest recorded_at,
with the last appended entry breaking ties. Unknown dates remain unknown.

Counts may be integers or source strings such as "~20000" and "<9". Approximate,
bounded, missing and zero-denominator measurements are preserved but unranked.
This module has no API, ledger or Moneyball dependency and never writes files.
"""

from __future__ import annotations

import copy
import html
import json
import re
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlsplit


_ATTRIBUTIONS = {"post_level", "trial", "unknown"}
_BOOST_STATUSES = {"UNKNOWN", "UNVERIFIED", "UNBOOSTED", "BOOSTED"}
_DISTRIBUTIONS = {"regular", "trial", "unknown"}
_LIMITATIONS = (
    "Manual follow conversion is separate from API metrics and fixed-window Moneyball rankings. "
    "Viewers and post-attributed follows are the supplied measurements; they are not substituted "
    "with API reach, views or account-level follower change.",
    "Observation dates and snapshot ages are unknown unless explicitly recorded. "
    "Rates and group ranks describe these supplied snapshots; they do not establish "
    "a matched-age, organic, causal or statistically significant winner.",
    "Trial attribution and post-level attribution are separate groups. Unknown, unverified, "
    "unboosted and boosted delivery are separate groups. Fewer than five follows is LOW COUNT.",
)


def _date_key(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Manual follow conversion {field} must be an ISO date or timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Manual follow conversion invalid {field}: {value!r}") from exc
    if result.tzinfo is None:
        if len(value) != 10:
            raise ValueError(f"Manual follow conversion {field} timestamp requires a timezone")
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _exact_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)", value):
        return int(value.replace(",", ""))
    return None


def _base(account: str) -> dict[str, Any]:
    return {
        "schema_version": 1, "account": account, "platform": "instagram",
        "status": "AVAILABLE", "source": "USER_SUPPLIED_MANUAL_OBSERVATIONS",
        "observations": [], "rows": [], "groups": [],
        "observation_count": 0, "creative_count": 0, "exact_rate_count": 0,
        "resolved_identity_count": 0,
        "latest_recorded_at": None, "unknown_observed_at_count": 0,
        "methodology": {
            "denominator": "user-supplied viewers", "numerator": "user-supplied follows",
            "formula": "100 * follows / viewers",
            "latest_selection": "Latest recorded_at per creative; last appended entry breaks ties.",
            "rank_method": "Exact-count competition ties (1, 1, 3) within attribution and boost groups only.",
            "overall_rank": None,
            "overall_order": "Exact calculated rates descending; unranked measurements follow separately.",
            "minimum_follow_count_flag": 5,
            "limitations": list(_LIMITATIONS),
        },
    }


def build_manual_follow_conversion(
    path: str | Path, account: str = "aibrief_jp",
) -> dict[str, Any]:
    """Load appendable manual observations without joining or changing API data.

    A missing file yields UNAVAILABLE. A malformed or mismatched file raises
    ValueError so callers cannot silently mix accounts or reset source history.
    The returned observations preserve every source entry, including older ones.
    """
    path = Path(path).expanduser()
    result = _base(account)
    if not path.is_file():
        result["status"] = "UNAVAILABLE"
        result["reason"] = "Manual follow-conversion source file is unavailable."
        return result
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Cannot read manual follow conversion {path}: {exc}") from exc
    if not isinstance(source, dict) or source.get("schema_version") != 1:
        raise ValueError("Manual follow conversion requires schema_version 1")
    if source.get("account") != account or source.get("platform") != "instagram":
        raise ValueError("Manual follow conversion account/platform mismatch")
    observations = source.get("observations")
    if not isinstance(observations, list):
        raise ValueError("Manual follow conversion observations must be a list")
    latest: dict[str, tuple[datetime, int, dict[str, Any]]] = {}
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise ValueError("Each manual follow conversion observation must be an object")
        creative_id = observation.get("creative_id")
        if not isinstance(creative_id, str) or not creative_id:
            raise ValueError("Each manual follow conversion observation needs a creative_id")
        if not isinstance(observation.get("creative"), str) or not observation["creative"]:
            raise ValueError("Each manual follow conversion observation needs its supplied creative name")
        recorded = _date_key(observation.get("recorded_at"), "recorded_at")
        if observation.get("observed_at") is not None:
            _date_key(observation["observed_at"], "observed_at")
        if observation.get("attribution_scope", "unknown") not in _ATTRIBUTIONS:
            raise ValueError("Unsupported manual follow conversion attribution_scope")
        if observation.get("boost_status", "UNKNOWN") not in _BOOST_STATUSES:
            raise ValueError("Unsupported manual follow conversion boost_status")
        if observation.get("distribution", "unknown") not in _DISTRIBUTIONS:
            raise ValueError("Unsupported manual follow conversion distribution")
        if observation.get("identity_status") == "MATCHED":
            media_id = observation.get("media_id")
            if not isinstance(media_id, str) or not re.fullmatch(r"\d+", media_id):
                raise ValueError("MATCHED manual follow conversion identity requires a numeric media_id string")
            if _instagram_reel_url(observation.get("permalink")) is None:
                raise ValueError("MATCHED manual follow conversion identity requires an HTTPS Instagram Reel permalink")
        reported = observation.get("user_reported_rate_percent")
        if reported is not None and not isinstance(reported, str):
            raise ValueError("user_reported_rate_percent must preserve the supplied rate as a string")
        if creative_id not in latest or (recorded, index) > latest[creative_id][:2]:
            latest[creative_id] = (recorded, index, copy.deepcopy(observation))
    result["observations"] = copy.deepcopy(observations)
    result["observation_count"] = len(observations)
    if latest:
        result["latest_recorded_at"] = max(latest.values(), key=lambda item: item[:2])[2]["recorded_at"]
    rates: dict[str, Fraction] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    rows = []
    for _, _, observation in latest.values():
        row = observation
        row.setdefault("observed_at", None)
        row.setdefault("watch_seconds", None)
        row.setdefault("attribution_scope", "unknown")
        row.setdefault("boost_status", "UNKNOWN")
        row.setdefault("identity_status", "UNRESOLVED")
        row.setdefault("media_id", None)
        row.setdefault("permalink", None)
        row.setdefault("published_title", None)
        row.setdefault("published_at", None)
        row.setdefault("distribution", "unknown")
        viewers, follows = _exact_count(row.get("viewers")), _exact_count(row.get("follows"))
        flags = []
        if row.get("observed_at") is None:
            flags.append("OBSERVATION_DATE_UNKNOWN")
        if row.get("viewers") is None:
            flags.append("MISSING_DENOMINATOR")
        elif viewers == 0:
            flags.append("ZERO_DENOMINATOR")
        elif viewers is None:
            flags.append("NONEXACT_DENOMINATOR")
        if row.get("follows") is None:
            flags.append("MISSING_FOLLOWS")
        elif follows is None:
            flags.append("NONEXACT_FOLLOWS")
        if follows is not None and follows < 5:
            flags.append("LOW_COUNT")
        row.update(
            viewers_exact=viewers, follows_exact=follows,
            calculated_rate_percent=None, exact_rate_available=False,
            group_rank=None, group_exact_count=0,
            group_key=f"{row['attribution_scope']}:{row['boost_status']}",
            flags=flags,
        )
        if viewers is not None and viewers > 0 and follows is not None:
            rate = Fraction(follows * 100, viewers)
            rates[row["creative_id"]] = rate
            row.update(calculated_rate_percent=float(rate), exact_rate_available=True)
        grouped.setdefault(row["group_key"], []).append(row)
        rows.append(row)
    groups = []
    for key, members in sorted(grouped.items()):
        ranked = sorted(
            (row for row in members if row["exact_rate_available"]),
            key=lambda row: (-rates[row["creative_id"]], row["creative_id"]),
        )
        previous_rate, rank = None, 0
        for position, row in enumerate(ranked, 1):
            rate = rates[row["creative_id"]]
            if previous_rate is None or rate != previous_rate:
                rank = position
            row["group_rank"] = rank
            previous_rate = rate
        for row in members:
            row["group_exact_count"] = len(ranked)
        unranked = sorted((row for row in members if not row["exact_rate_available"]), key=lambda row: row["creative_id"])
        groups.append({
            "key": key, "attribution_scope": members[0]["attribution_scope"],
            "boost_status": members[0]["boost_status"], "exact_count": len(ranked),
            "unranked_count": len(unranked), "rows": ranked + unranked,
        })
    rows.sort(key=lambda row: (
        not row["exact_rate_available"], -rates.get(row["creative_id"], Fraction(0)), row["creative_id"],
    ))
    result.update(
        rows=rows, groups=groups, creative_count=len(rows), exact_rate_count=len(rates),
        unknown_observed_at_count=sum(row["observed_at"] is None for row in rows),
        resolved_identity_count=sum(_identity_url(row) is not None for row in rows),
    )
    return result


def _escape(value: Any, as_html: bool) -> str:
    text = "Unknown" if value is None else str(value)
    if as_html:
        return html.escape(text, quote=True)
    text = text.replace("\\", "\\\\")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for symbol in ("|", "[", "]", "*", "_", "`"):
        text = text.replace(symbol, "\\" + symbol)
    return text.replace("\r", " ").replace("\n", " ")


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return None
    return value


def _instagram_reel_url(value: Any) -> str | None:
    url = _safe_url(value)
    if url is None:
        return None
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"instagram.com", "www.instagram.com"}
        or not re.fullmatch(r"/reel/[A-Za-z0-9_-]+/?", parsed.path)
    ):
        return None
    return url


def _identity_url(row: Mapping[str, Any]) -> str | None:
    if row.get("identity_status") not in {"MATCHED", "VERIFIED"}:
        return None
    media_id = row.get("media_id")
    if not isinstance(media_id, str) or not re.fullmatch(r"\d+", media_id):
        return None
    return _instagram_reel_url(row.get("permalink"))


def _creative(row: Mapping[str, Any], as_html: bool) -> str:
    label = _escape(row.get("creative"), as_html)
    url = _identity_url(row)
    if url:
        if as_html:
            label = f'<a href="{html.escape(url, quote=True)}">{label}</a>'
        else:
            label = f'[{label}]({quote(url, safe=":/?=&%#@+;,")})'
    published_title = row.get("published_title")
    if published_title and published_title != row.get("creative"):
        title = _escape(published_title, as_html)
        if as_html:
            label += f'<br><small class="muted">Published title: {title}</small>'
        else:
            label += f"<br>Published title: {title}"
    return f'<span style="display:block;min-width:240px">{label}</span>' if as_html else label


def _display_count(value: Any) -> Any:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else value


def _display_flags(flags: list[str]) -> str:
    labels = {
        "LOW_COUNT": "Low count",
        "NONEXACT_DENOMINATOR": "Approximate viewers",
        "NONEXACT_FOLLOWS": "Bounded follows",
        "MISSING_DENOMINATOR": "Viewers missing",
        "ZERO_DENOMINATOR": "Zero viewers",
        "MISSING_FOLLOWS": "Follows missing",
    }
    return "; ".join(labels.get(flag, flag.replace("_", " ").capitalize())
                     for flag in flags if flag != "OBSERVATION_DATE_UNKNOWN") or "—"


def _table(headers: list[str], rows: list[list[str]], as_html: bool) -> str:
    if as_html:
        head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
        body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
        return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    return "\n".join([
        "| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ])


def _render(data: Mapping[str, Any], as_html: bool) -> str:
    def heading(value: str, level: int = 3) -> str:
        return f"<h{level}>{html.escape(value)}</h{level}>" if as_html else "#" * level + " " + value

    def paragraph(value: str) -> str:
        return f"<p>{_escape(value, True)}</p>" if as_html else _escape(value, False)

    parts = [heading("Manual follow-conversion leaderboard", 2)]
    if data.get("status") != "AVAILABLE":
        parts.append(paragraph(str(data.get("reason") or "Manual follow-conversion data is unavailable.")))
    else:
        parts.append(paragraph(
            f"{data.get('account')} · {data.get('creative_count', 0)} creatives from "
            f"{data.get('observation_count', 0)} retained manual observations. Latest recorded entry: "
            f"{data.get('latest_recorded_at') or 'Unknown'}. Recorded date is not the measurement date."
        ))
        parts.append(paragraph(
            f"{data.get('resolved_identity_count', 0)} of {data.get('creative_count', 0)} creatives "
            "are linked to published Reels. Attribution preserves the supplied measurement context; "
            "Distribution identifies whether the matched Reel was regular or trial."
        ))
        parts.extend(paragraph(note) for note in _LIMITATIONS)
        parts.append(heading("Supplied snapshots · descriptive order, no overall rank"))
        rows = []
        for row in data.get("rows", []):
            rate = row.get("calculated_rate_percent")
            source_rate = row.get("user_reported_rate_percent")
            if source_rate is not None:
                source_rate = str(source_rate)
                if not source_rate.endswith("%"):
                    source_rate += "%"
            rows.append([
                _creative(row, as_html), _escape(_display_count(row.get("viewers")), as_html),
                _escape(_display_count(row.get("follows")), as_html), _escape(source_rate, as_html),
                f"{rate:.4f}%" if rate is not None else "Unranked",
                _escape(row.get("watch_seconds"), as_html),
                _escape(row.get("attribution_scope"), as_html),
                _escape(row.get("distribution"), as_html), _escape(row.get("boost_status"), as_html),
                _escape(row.get("observed_at"), as_html),
                _escape(_display_flags(row.get("flags", [])), as_html),
            ])
        parts.append(_table([
            "Creative", "Viewers (supplied)", "Follows (supplied)", "Rate (reported)",
            "Rate (recomputed)", "Watch (seconds)", "Attribution", "Distribution", "Boost", "Observed at", "Flags",
        ], rows, as_html))
        parts.append(heading("Exact-count ranks within attribution and boost groups"))
        parts.append(paragraph("Group ranks remain descriptive because measurement ages are not matched. LOW COUNT rows keep their measured rank with the flag visible. Approximate, bounded and missing counts have no rank."))
        for group in data.get("groups", []):
            parts.append(heading(f"{group['attribution_scope']} · {group['boost_status']}", 4))
            parts.append(_table(
                ["Group rank", "Creative", "Follows / viewers", "Calculated rate", "Flags"],
                [[
                    str(row["group_rank"]) if row.get("group_rank") is not None else "Unranked",
                    _creative(row, as_html),
                    f"{_escape(_display_count(row.get('follows')), as_html)} / {_escape(_display_count(row.get('viewers')), as_html)}",
                    f"{row['calculated_rate_percent']:.4f}%" if row.get("calculated_rate_percent") is not None else "Unknown",
                    _escape(_display_flags(row.get("flags", [])), as_html),
                ] for row in group.get("rows", [])], as_html,
            ))
    content = "\n\n".join(parts) + "\n"
    return f'<section id="manual-follow-conversion" class="panel full">\n{content}</section>\n' if as_html else content


def render_manual_follow_conversion_markdown(data: Mapping[str, Any]) -> str:
    return _render(data, as_html=False)


def render_manual_follow_conversion_html(data: Mapping[str, Any]) -> str:
    return _render(data, as_html=True)
