"""Human-readable, account-scoped views of the Moneyball record book.

These renderers only consume the record-book payload. Ranking, eligibility, and
record-event decisions stay in the builder so the HTML and Markdown agree.
"""

from __future__ import annotations

import html
import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit


_METRICS = (
    "three_second_skip_rate",
    "views_per_reached_account",
    "saves_per_reach",
    "shares_per_view",
    "reach",
)
_WINDOWS = ("24h", "72h", "7d")
_SUPPORT_LABELS = {
    "reach": "reach",
    "views": "views",
    "saves": "saves",
    "shares": "shares",
    "reels_skip_rate": "raw skip (%)",
    "denominator_type": "denominator",
    "eligibility_base": "eligibility base",
}
_LIMITATIONS = (
    "Records mean best among this account’s tracked, eligible observations, "
    "not Instagram-wide records or a complete account lifetime history.",
    "24h, 72h and 7d checkpoints are separate cohorts. Missing checkpoints "
    "cannot be reconstructed from lifetime totals; actual snapshot ages are shown.",
    "Eligible ranks exclude flagged small-base or small-count observations. "
    "Measured ranks include them and are provisional, not editorial benchmarks. "
    "Percentiles are directional: higher is stronger, including for lower skip rate.",
    "Looping is views divided by reached accounts. It is a proxy for repeat viewing, "
    "not a verified replay count. Save rate uses reach; share ratio uses views.",
    "These metrics do not establish follower conversion. Lifetime reach has unequal "
    "exposure ages and is descriptive context only; accompanying raw views are not ranked. Manually reported "
    "follows remain separate and are excluded from combined rankings.",
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _num(value: Any, places: int = 2) -> str:
    number = _number(value)
    if number is None:
        return "—"
    result = f"{number:,.{places}f}"
    return result.rstrip("0").rstrip(".") if places else result


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname:
            return None
    except ValueError:
        return None
    return value


def _plain(value: Any) -> str:
    return " ".join(str(value if value is not None else "—").split())


def _text(value: Any, *, as_html: bool) -> str:
    escaped = html.escape(_plain(value), quote=True)
    if not as_html:
        for char in ("\\", "|", "[", "]", "*", "_", "`"):
            escaped = escaped.replace(char, "\\" + char)
    return escaped


def _link(row: Mapping[str, Any], *, as_html: bool) -> str:
    title = _text(row.get("title") or row.get("media_id") or "Unknown Reel", as_html=as_html)
    url = _safe_url(row.get("permalink"))
    if not url:
        return title
    if as_html:
        return (
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{title}</a>'
        )
    safe = quote(url, safe=":/?&=#%+@,;~-._")
    return f"[{title}]({safe})"


def _value(value: Any, metric: Mapping[str, Any]) -> str:
    number = _number(value)
    if number is None:
        return "—"
    format_name = metric.get("format")
    if format_name == "percent_ratio":
        return f"{_num(number * 100, 4)}%"
    if format_name == "percent_direct":
        return f"{_num(number)}%"
    if format_name == "rate_per_1000":
        return f"{_num(number)} /1k"
    if format_name == "ratio":
        return f"{_num(number, 3)}×"
    return _num(number, 0 if number.is_integer() else 2)


def _ordered_metrics(boards: Mapping[str, Any], *, lifetime: bool = False) -> list[str]:
    selected = ("reach",) if lifetime else _METRICS
    return [key for key in selected if key in boards]


def _age(row: Mapping[str, Any]) -> str:
    value = _number(row.get("actual_age_hours"))
    return f"{_num(value)}h" if value is not None else "age unavailable"


def _flags(row: Mapping[str, Any]) -> str:
    flags = row.get("eligibility_flags") or []
    if isinstance(flags, str):
        flags = [flags]
    if not isinstance(flags, (list, tuple)):
        flags = []
    text = "; ".join(_plain(flag) for flag in flags)
    if not row.get("eligible", False):
        return "PROVISIONAL" + (f": {text}" if text else "; no eligible standing")
    return text or "Eligible"


def _support(row: Mapping[str, Any]) -> str:
    support = _mapping(row.get("supporting_metrics"))
    raw = _mapping(row.get("raw_metrics"))
    # Keep sample sizes visible even when a metric's support map is narrower.
    values = dict(support)
    for key in ("reach", "views", "saves", "shares"):
        if key not in values and raw.get(key) is not None:
            values[key] = raw[key]
    ordered = [key for key in _SUPPORT_LABELS if key in values]
    ordered += sorted(set(values) - set(ordered))
    return "; ".join(
        f"{_SUPPORT_LABELS.get(key, key)}: "
        f"{_num(values[key]) if _number(values[key]) is not None else _plain(values[key])}"
        for key in ordered
    ) or "raw support unavailable"


def _standing(row: Mapping[str, Any], *, measured: bool = False) -> str:
    prefix = "measured_" if measured else ""
    rank = _number(row.get(prefix + "rank"))
    cohort = row.get(prefix + "cohort_size")
    percentile = row.get(prefix + "directional_percentile")
    if rank is None:
        return "—"
    return f"#{_num(rank, 0)} / {_num(cohort, 0)} · P{_num(percentile, 1)}"


def _leaders(rows: Any, *, as_html: bool) -> str:
    leaders = _rows(rows)
    if not leaders:
        return "—"
    # One comparator is sufficient in dense tables; ties remain explicit.
    return _link(leaders[0], as_html=as_html) + (
        f" (+{len(leaders) - 1} tied)" if len(leaders) > 1 else ""
    )


def _table(headers: list[str], rows: list[list[str]], *, as_html: bool, scroll: bool = False) -> str:
    if not rows:
        return "<p>No observations available.</p>" if as_html else "No observations available."
    if as_html:
        headings = "".join(f"<th scope=\"col\">{_text(item, as_html=True)}</th>" for item in headers)
        body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
        wrapper_class = "table-wrap reel-table-wrap" if scroll else "table-wrap"
        table_class = ' class="aggregate-table"' if scroll else ""
        return f'<div class="{wrapper_class}"><table{table_class}><thead><tr>{headings}</tr></thead><tbody>{body}</tbody></table></div>'
    return "\n".join(
        ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
        + ["| " + " | ".join(row) + " |" for row in rows]
    )


def _paragraph(value: Any, *, as_html: bool) -> str:
    text = _text(value, as_html=as_html)
    return f"<p>{text}</p>" if as_html else text


def _heading(value: str, *, as_html: bool, level: int = 3) -> str:
    text = _text(value, as_html=as_html)
    return f"<h{level}>{text}</h{level}>" if as_html else "#" * level + " " + text


def _leader_table(window: Mapping[str, Any], *, as_html: bool, lifetime: bool = False) -> str:
    boards = _mapping(window.get("metric_rankings"))
    rows = []
    for key in _ordered_metrics(boards, lifetime=lifetime):
        board = _mapping(boards[key])
        leaders = _rows(board.get("record_leaders"))
        label = str(board.get("label") or key)
        direction = "lower is stronger" if board.get("direction") == "lower" else "higher is stronger"
        for leader in leaders or [{}]:
            cells = [
                _text(label + ("" if lifetime else f" ({direction})"), as_html=as_html),
                (_link(leader, as_html=as_html) + (" (tied leader)" if len(leaders) > 1 else "")) if leader else "—",
                _value(leader.get("value"), board),
                f"{_num(board.get('eligible_count'), 0)} eligible / {_num(board.get('measured_count'), 0)} measured",
                _text(_age(leader) if leader else "—", as_html=as_html),
                _text(_support(leader) if leader else "No eligible observation", as_html=as_html),
            ]
            rows.append(cells)
    return _table(["Metric", "Leader", "Value", "Cohort", "Actual age", "Raw support"], rows, as_html=as_html)


def _fresh_section(records: Mapping[str, Any], *, as_html: bool) -> str:
    fresh = _mapping(records.get("fresh"))
    boards = _mapping(_mapping(_mapping(records.get("windows")).get("24h")).get("metric_rankings"))
    posts = _rows(fresh.get("posts"))
    intro = (
        f"{len(posts)} fresh posts; {_num(fresh.get('missing_checkpoint_count'), 0)} "
        "without a comparable 24h checkpoint. Eligible standing is rank / cohort · "
        "directional percentile. Measured standing includes provisional observations."
    )
    if fresh.get("start_exclusive") or fresh.get("end_inclusive"):
        intro += f" Published after {fresh.get('start_exclusive') or '—'} through {fresh.get('end_inclusive') or '—'}."
    rows = []
    for post in posts:
        metrics = _mapping(post.get("metrics"))
        if not metrics:
            checkpoint_present = post.get("has_24h_checkpoint", False)
            rows.append([
                _link(post, as_html=as_html),
                "24h metrics unavailable" if checkpoint_present else "24h checkpoint unavailable",
                "—", "—", "—", "—", "—",
                "Missing metric fields" if checkpoint_present else "Missing comparable checkpoint", "—",
            ])
        for key in _ordered_metrics(metrics):
            row = _mapping(metrics[key])
            board = _mapping(boards.get(key))
            comparator = _leaders(row.get("record_leaders"), as_html=as_html)
            if _number(row.get("record_value")) is not None:
                comparator += " · " + _value(row.get("record_value"), board)
            rows.append([
                _link(post, as_html=as_html),
                _text(board.get("label") or key, as_html=as_html),
                _value(row.get("value"), board),
                _text(_standing(row), as_html=as_html),
                _text(_standing(row, measured=True), as_html=as_html),
                comparator,
                _text(_age(row), as_html=as_html),
                _text(_flags(row), as_html=as_html),
                _text(_support(row), as_html=as_html),
            ])
    return "\n\n".join([
        _heading("Fresh posts in all-time 24h context", as_html=as_html),
        _paragraph(intro, as_html=as_html),
        _table(["Fresh Reel", "Metric", "Value", "Eligible standing", "Measured standing", "Best eligible comparator", "Actual age", "Flags", "Raw support"], rows, as_html=as_html, scroll=True),
    ])


def _improvement(event: Mapping[str, Any]) -> str:
    current, previous = _number(event.get("value")), _number(event.get("previous_value"))
    if current is None or previous is None:
        return "—"
    delta = current - previous
    prefix = "+" if delta > 0 else "−" if delta < 0 else ""
    format_name = event.get("format")
    if format_name in {"percent_ratio", "percent_direct"}:
        difference = _num(abs(delta) * (100 if format_name == "percent_ratio" else 1)) + " pp"
    else:
        difference = _value(abs(delta), event)
    improvement = _number(event.get("improvement_percent"))
    relative_label = "higher" if event.get("window") == "lifetime" else "better"
    return prefix + difference + (
        f"; {_num(improvement)}% {relative_label}" if improvement is not None else "; relative change unavailable"
    )


def _event_leaders(rows: Any, value: Any, event: Mapping[str, Any], *, as_html: bool) -> str:
    """Keep each event holder's value, age, and raw evidence together."""
    leaders = _rows(rows)
    if not leaders:
        return _value(value, event) + " · holder and snapshot evidence unavailable"
    rendered = []
    for leader in leaders:
        title = _link(leader, as_html=as_html) + " · " + _value(value, event)
        evidence = _text(_age(leader) + " · " + _support(leader), as_html=as_html)
        rendered.append(f"{title}<br><small>{evidence}</small>" if as_html else f"{title} ({evidence})")
    return "<br>".join(rendered)


def _events_section(records: Mapping[str, Any], *, as_html: bool) -> str:
    methodology_baseline = records.get("status") == "METHODOLOGY_BASELINE_ESTABLISHED"
    baseline = records.get("status") == "BASELINE_ESTABLISHED" or methodology_baseline
    parts = [
        _heading("Record changes", as_html=as_html),
        _paragraph(
            "These events compare with the previous analytics refresh. The three-day "
            "pulse uses persisted record history since the previous pulse.", as_html=as_html,
        ),
    ]
    if baseline:
        note = (
            "Ranking metrics changed. A new methodology baseline is established; "
            "old records are retained as historical context. This comparison makes "
            "no new-record claims."
            if methodology_baseline else
            "Baseline established. Current leaders are historical reference points; "
            "this first run makes no new-record claims."
        )
        parts.append(_paragraph(note, as_html=as_html))
        return "\n\n".join(parts)
    events = _rows(records.get("events"))
    new_records = []
    lifetime_totals = []
    updates = []
    for event in events:
        lifetime = event.get("window") == "lifetime"
        if event.get("metric_key") not in (("reach",) if lifetime else _METRICS):
            continue
        window_label = "Lifetime · descriptive total" if lifetime else event.get("window")
        if event.get("status") == "NEW_RECORD":
            current, previous = _number(event.get("value")), _number(event.get("previous_value"))
            improves = current is not None and previous is not None and (
                current < previous if event.get("direction") == "lower" else current > previous
            )
            # Do not turn a malformed or tied event into a user-facing record claim.
            if not improves:
                continue
            cells = [
                _text(window_label, as_html=as_html),
                _text(event.get("label") or event.get("metric_key"), as_html=as_html),
                _event_leaders(event.get("previous_leaders"), previous, event, as_html=as_html),
                _event_leaders(event.get("leaders"), current, event, as_html=as_html),
                _text(_improvement(event), as_html=as_html),
            ]
            (lifetime_totals if lifetime else new_records).append(cells)
        elif event.get("status") in {"RECORD_TIED", "RECORD_REVISED", "HISTORICAL_RECORD_DISCOVERED", "BASELINE_ESTABLISHED"}:
            updates.append([
                _text(window_label, as_html=as_html),
                _text(event.get("label") or event.get("metric_key"), as_html=as_html),
                _text(str(event.get("status")).replace("_", " ").capitalize(), as_html=as_html),
                _event_leaders(event.get("leaders"), event.get("value"), event, as_html=as_html),
            ])
    if new_records:
        parts += [
            _heading("New records · strict improvements", as_html=as_html, level=4),
            _table(["Window", "Metric", "Previous record", "New record", "Improvement"], new_records, as_html=as_html),
        ]
    else:
        parts.append(_paragraph("No new strict-improvement records in this comparison.", as_html=as_html))
    if lifetime_totals:
        parts += [
            _heading("New descriptive lifetime reach totals · unequal ages", as_html=as_html, level=4),
            _paragraph("Higher cumulative reach can reflect longer exposure; this is not a fixed-window performance record. Raw views accompany the reach-selected holder as supporting context only.", as_html=as_html),
            _table(["Window", "Metric", "Previous total", "New total", "Change"], lifetime_totals, as_html=as_html),
        ]
    if updates:
        parts += [
            _paragraph("Other record-history updates are listed separately from new records.", as_html=as_html),
            _table(["Window", "Metric", "History update", "Current leader"], updates, as_html=as_html),
        ]
    return "\n\n".join(parts)


def _render(records: Mapping[str, Any], *, as_html: bool) -> str:
    records = _mapping(records)
    scope = (
        f"Account: {records.get('account') or 'unavailable'} · "
        f"Platform: {records.get('platform') or 'unavailable'} · "
        f"As of {records.get('as_of') or 'unavailable'}. "
        f"{_num(records.get('tracked_post_count'), 0)} tracked posts; "
        f"earliest tracked publication: {records.get('history_start') or 'unavailable'}."
    )
    parts = [
        _heading("All-time tracked account records", as_html=as_html, level=2),
        _paragraph(scope, as_html=as_html),
        _paragraph(_LIMITATIONS[0], as_html=as_html),
        _events_section(records, as_html=as_html),
        _fresh_section(records, as_html=as_html),
    ]
    windows = _mapping(records.get("windows"))
    for name in _WINDOWS:
        window = _mapping(windows.get(name))
        title = f"{name} all-time eligible leaders"
        content = "\n\n".join([
            _paragraph(
                f"{_num(window.get('cohort_size'), 0)} comparable checkpoints; "
                f"{_num(window.get('missing_checkpoint_count'), 0)} tracked posts without "
                "this checkpoint. Metric-specific eligible and measured counts appear below.",
                as_html=as_html,
            ),
            _leader_table(window, as_html=as_html),
        ])
        if as_html and name != "24h":
            parts.append(f"<details><summary>{_text(title, as_html=True)}</summary>{content}</details>")
        else:
            parts += [_heading(title, as_html=as_html), content]
    lifetime = _mapping(records.get("lifetime"))
    parts += [
        _heading("Lifetime reach · descriptive context", as_html=as_html),
        _paragraph("Latest observed cumulative reach has unequal exposure ages and is not a fixed-window performance ranking. Raw views accompany the reach-selected leader as context; they never select or rank a leader.", as_html=as_html),
        _leader_table(lifetime, as_html=as_html, lifetime=True),
        _heading("How to read these records", as_html=as_html),
    ]
    methodology = _mapping(records.get("methodology"))
    if all(_number(methodology.get(key)) is not None for key in (
        "minimum_rate_reach", "minimum_rate_views", "minimum_saves", "minimum_shares",
    )):
        parts.append(_paragraph(
            f"Skip and looping eligibility require at least {_num(methodology['minimum_rate_reach'], 0)} reach. "
            f"Save-rate records require {_num(methodology['minimum_rate_reach'], 0)} reach and "
            f"{_num(methodology['minimum_saves'], 0)} saves. Share-ratio records require "
            f"{_num(methodology['minimum_rate_views'], 0)} views and {_num(methodology['minimum_shares'], 0)} shares. "
            "Raw reach has no rate denominator. "
            "These are screening floors, not proof that a result is statistically reliable.",
            as_html=as_html,
        ))
    if as_html:
        parts.append("<ul>" + "".join(f"<li>{_text(item, as_html=True)}</li>" for item in _LIMITATIONS[1:]) + "</ul>")
        return '<section id="instagram-account-records" data-testid="instagram-account-records" class="panel full ranking-shell">' + "\n".join(parts) + "</section>"
    parts.append("\n".join("- " + _text(item, as_html=False) for item in _LIMITATIONS[1:]))
    return "\n\n".join(parts) + "\n"


def render_account_records_markdown(records: Mapping[str, Any]) -> str:
    """Render a linked report without exposing the full historical row inventory."""
    return _render(records, as_html=False)


def render_account_records_html(records: Mapping[str, Any]) -> str:
    """Render a safe section fragment using the canonical dashboard's CSS classes."""
    return _render(records, as_html=True)
