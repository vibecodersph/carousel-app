#!/usr/bin/env python3
"""SQLite ledger that makes the clip-on-a-channel the source of truth for reels.

The legacy flow stores reel state inside per-run ``schedule.json`` files, so
answering "which clips have been scheduled or published" requires scanning and
joining every schedule. This ledger keeps one durable row per
``(content_hash, channel_id)``: the hash is computed from the channel-specific
media file, so the two language variants of one clip are independent rows, and
re-running discovery is idempotent. That primary key doubles as the
duplicate-publish guard.

The module is intentionally dependency-free (stdlib ``sqlite3`` only) and holds
no scheduling policy — callers decide *when* a clip posts. See
``REEL_SCHEDULING.md`` for the full design.
"""
from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "state" / "reels.db"
SCHEMA_VERSION = 1

# Status lifecycle. ``new`` is discovered-but-unscheduled; the rest mirror the
# legacy schedule.json vocabulary so imports map cleanly.
STATUS_NEW = "new"
STATUS_SCHEDULED = "scheduled"
STATUS_PUBLISHING = "publishing"
STATUS_PREVIEWED = "publish_previewed"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Precedence used when two records collide (e.g. a clip scheduled in two
# different schedules). Higher wins, so an import never downgrades a published
# reel back to scheduled, and a duplicate "failed" never overwrites a success.
STATUS_RANK = {
    STATUS_NEW: 0,
    STATUS_SKIPPED: 1,
    STATUS_SCHEDULED: 2,
    STATUS_PREVIEWED: 3,
    STATUS_PUBLISHING: 4,
    STATUS_FAILED: 5,
    STATUS_PUBLISHED: 6,
}

REEL_COLUMNS = {
    "lang",
    "clip_dir",
    "media_path",
    "source_video",
    "title",
    "caption",
    "status",
    "scheduled_at",
    "published_at",
    "media_id",
    "permalink",
    "last_error",
    "manifest_path",
    "updated_at",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS reels (
  content_hash  TEXT NOT NULL,
  channel_id    TEXT NOT NULL,
  lang          TEXT,
  clip_dir      TEXT NOT NULL,
  media_path    TEXT NOT NULL,
  source_video  TEXT,
  title         TEXT,
  caption       TEXT,
  status        TEXT NOT NULL DEFAULT 'new',
  scheduled_at  TEXT,
  published_at  TEXT,
  media_id      TEXT,
  permalink     TEXT,
  last_error    TEXT,
  manifest_path TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (content_hash, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_reels_channel_status ON reels(channel_id, status);
CREATE INDEX IF NOT EXISTS idx_reels_scheduled ON reels(scheduled_at);

CREATE TABLE IF NOT EXISTS insights (
  id           INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL,
  channel_id   TEXT NOT NULL,
  media_id     TEXT NOT NULL,
  captured_at  TEXT NOT NULL,
  views INTEGER, reach INTEGER, likes INTEGER, comments INTEGER,
  saved INTEGER, shares INTEGER, total_interactions INTEGER,
  raw          TEXT,
  FOREIGN KEY (content_hash, channel_id) REFERENCES reels(content_hash, channel_id)
);
CREATE INDEX IF NOT EXISTS idx_insights_reel ON insights(content_hash, channel_id, captured_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def hash_file(path: Path | str, *, chunk_size: int = 1 << 20) -> str:
    """sha256 of a file's bytes, streamed so large videos don't load into RAM."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_text(value: str) -> str:
    """Stable id fallback for records whose media file is missing on disk."""
    return "missing:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def connect(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Open the ledger with WAL + foreign keys, creating the schema if needed.

    Commits on clean exit, rolls back on exception. WAL mode lets a periodic
    runner write while a dashboard reads, and serializes overlapping writers.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        init_db(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


def get_reel(conn: sqlite3.Connection, content_hash: str, channel_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reels WHERE content_hash=? AND channel_id=?",
        (content_hash, channel_id),
    ).fetchone()


def upsert_discovered(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    channel_id: str,
    lang: str | None,
    clip_dir: Path | str,
    media_path: Path | str,
    source_video: str | None = None,
    title: str | None = None,
) -> str:
    """Record a freshly discovered clip variant without disturbing its lifecycle.

    A new clip lands as ``new``. An existing clip only has its mutable metadata
    refreshed (path moved, title changed) — status, scheduled_at, and publish
    fields are preserved so re-scanning never un-schedules or re-publishes.
    Returns ``"inserted"`` or ``"updated"``.
    """
    now = utc_now()
    if get_reel(conn, content_hash, channel_id) is None:
        conn.execute(
            "INSERT INTO reels (content_hash, channel_id, lang, clip_dir, media_path, "
            "source_video, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content_hash, channel_id, lang, str(clip_dir), str(media_path),
             source_video, title, STATUS_NEW, now, now),
        )
        return "inserted"
    conn.execute(
        "UPDATE reels SET lang=?, clip_dir=?, media_path=?, "
        "source_video=COALESCE(?, source_video), title=COALESCE(?, title), updated_at=? "
        "WHERE content_hash=? AND channel_id=?",
        (lang, str(clip_dir), str(media_path), source_video, title, now,
         content_hash, channel_id),
    )
    return "updated"


def upsert_imported(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    channel_id: str,
    lang: str | None,
    clip_dir: Path | str,
    media_path: Path | str,
    status: str,
    source_video: str | None = None,
    title: str | None = None,
    scheduled_at: str | None = None,
    published_at: str | None = None,
    media_id: str | None = None,
    permalink: str | None = None,
    manifest_path: str | None = None,
) -> str:
    """Seed a row from existing schedule history, upgrading status by precedence.

    Used by ``import-schedules``. On collision (the same clip present in two
    schedules) the more-advanced status wins, so a duplicate ``_attributed``
    schedule collapses into the original. Returns ``inserted``/``updated``/``kept``.
    """
    now = utc_now()
    existing = get_reel(conn, content_hash, channel_id)
    if existing is None:
        conn.execute(
            "INSERT INTO reels (content_hash, channel_id, lang, clip_dir, media_path, "
            "source_video, title, status, scheduled_at, published_at, media_id, permalink, "
            "manifest_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (content_hash, channel_id, lang, str(clip_dir), str(media_path), source_video,
             title, status, scheduled_at, published_at, media_id, permalink, manifest_path,
             now, now),
        )
        return "inserted"
    if STATUS_RANK.get(status, 0) < STATUS_RANK.get(str(existing["status"]), 0):
        return "kept"
    conn.execute(
        "UPDATE reels SET status=?, lang=?, clip_dir=?, media_path=?, "
        "source_video=COALESCE(?, source_video), title=COALESCE(?, title), "
        "scheduled_at=COALESCE(?, scheduled_at), published_at=COALESCE(?, published_at), "
        "media_id=COALESCE(?, media_id), permalink=COALESCE(?, permalink), "
        "manifest_path=COALESCE(?, manifest_path), updated_at=? "
        "WHERE content_hash=? AND channel_id=?",
        (status, lang, str(clip_dir), str(media_path), source_video, title, scheduled_at,
         published_at, media_id, permalink, manifest_path, now, content_hash, channel_id),
    )
    return "updated"


def set_status(
    conn: sqlite3.Connection,
    content_hash: str,
    channel_id: str,
    status: str,
    **fields: Any,
) -> None:
    """Transition a single reel's status plus any extra columns (used at publish)."""
    columns = ["status=?", "updated_at=?"]
    values: list[Any] = [status, utc_now()]
    for key, value in fields.items():
        if key not in REEL_COLUMNS:
            raise ValueError(f"Unknown reel column: {key}")
        columns.append(f"{key}=?")
        values.append(value)
    values.extend([content_hash, channel_id])
    conn.execute(
        f"UPDATE reels SET {', '.join(columns)} WHERE content_hash=? AND channel_id=?",
        values,
    )


def claim_for_publish(
    conn: sqlite3.Connection,
    content_hash: str,
    channel_id: str,
    *,
    retry_failed: bool = False,
) -> bool:
    """Atomically move a due row into publishing if it is still publishable."""
    allowed = [STATUS_SCHEDULED, STATUS_PREVIEWED]
    if retry_failed:
        allowed.append(STATUS_FAILED)
    placeholders = ",".join("?" for _ in allowed)
    cursor = conn.execute(
        "UPDATE reels SET status=?, updated_at=?, last_error=NULL "
        "WHERE content_hash=? AND channel_id=? AND status IN (" + placeholders + ")",
        [STATUS_PUBLISHING, utc_now(), content_hash, channel_id, *allowed],
    )
    return cursor.rowcount == 1


def record_insight(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    channel_id: str,
    media_id: str,
    metrics: dict[str, Any],
    raw: str | None = None,
    captured_at: str | None = None,
) -> None:
    """Append a timestamped insights snapshot for a published reel."""
    conn.execute(
        "INSERT INTO insights (content_hash, channel_id, media_id, captured_at, "
        "views, reach, likes, comments, saved, shares, total_interactions, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            content_hash, channel_id, media_id, captured_at or utc_now(),
            metrics.get("views"), metrics.get("reach"), metrics.get("likes"),
            metrics.get("comments"), metrics.get("saved"), metrics.get("shares"),
            metrics.get("total_interactions"), raw,
        ),
    )


def status_counts(conn: sqlite3.Connection, channel_id: str | None = None) -> dict[str, dict[str, int]]:
    """Per-channel ``{channel_id: {status: count}}`` summary."""
    if channel_id:
        rows = conn.execute(
            "SELECT channel_id, status, COUNT(*) AS n FROM reels WHERE channel_id=? "
            "GROUP BY channel_id, status",
            (channel_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT channel_id, status, COUNT(*) AS n FROM reels GROUP BY channel_id, status"
        ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out.setdefault(row["channel_id"], {})[row["status"]] = row["n"]
    return out


def _scheduled_sort_key(value: Any) -> tuple[int, float]:
    """Sort key that normalizes mixed-timezone ISO timestamps to a UTC instant.

    Channels post in different zones (Tokyo +09:00 vs Manila +08:00), so a raw
    string sort would mis-order reels at the same absolute time. Unparseable
    values sort last.
    """
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (0, parsed.astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError):
        return (1, 0.0)


def upcoming(
    conn: sqlite3.Connection,
    channel_id: str | None = None,
    limit: int | None = 20,
) -> list[sqlite3.Row]:
    """Queued-but-unpublished reels in true chronological (UTC) order."""
    statuses = [STATUS_SCHEDULED, STATUS_PREVIEWED]
    placeholders = ",".join("?" for _ in statuses)
    query = (
        "SELECT * FROM reels WHERE status IN (" + placeholders + ") AND scheduled_at IS NOT NULL"
        + (" AND channel_id=?" if channel_id else "")
    )
    params: list[Any] = list(statuses)
    if channel_id:
        params.append(channel_id)
    rows = conn.execute(query, params).fetchall()
    rows.sort(key=lambda row: _scheduled_sort_key(row["scheduled_at"]))
    return rows[:limit] if limit is not None else rows


def recent_published(
    conn: sqlite3.Connection,
    channel_id: str | None = None,
    limit: int | None = 10,
) -> list[sqlite3.Row]:
    query = (
        "SELECT * FROM reels WHERE status=?"
        + (" AND channel_id=?" if channel_id else "")
        + " ORDER BY published_at DESC"
    )
    params: list[Any] = [STATUS_PUBLISHED]
    if channel_id:
        params.append(channel_id)
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def new_reels(
    conn: sqlite3.Connection,
    channel_id: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Discovered reels that have not been assigned to a posting slot yet."""
    query = (
        "SELECT * FROM reels WHERE status=? AND scheduled_at IS NULL"
        + (" AND channel_id=?" if channel_id else "")
        + " ORDER BY channel_id, source_video, clip_dir, lang, content_hash"
    )
    params: list[Any] = [STATUS_NEW]
    if channel_id:
        params.append(channel_id)
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def rows_with_schedule(
    conn: sqlite3.Connection,
    channel_id: str | None = None,
) -> list[sqlite3.Row]:
    """Rows that already have a scheduled_at value, regardless of lifecycle."""
    query = (
        "SELECT * FROM reels WHERE scheduled_at IS NOT NULL "
        "AND status IN (?, ?, ?, ?)"
        + (" AND channel_id=?" if channel_id else "")
    )
    params: list[Any] = [
        STATUS_SCHEDULED,
        STATUS_PREVIEWED,
        STATUS_PUBLISHING,
        STATUS_PUBLISHED,
    ]
    if channel_id:
        params.append(channel_id)
    rows = conn.execute(query, params).fetchall()
    rows.sort(key=lambda row: _scheduled_sort_key(row["scheduled_at"]))
    return rows


def due_reels(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    channel_id: str | None = None,
    include_future: bool = False,
    retry_failed: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Scheduled ledger rows ready for publishing."""
    statuses = [STATUS_SCHEDULED, STATUS_PREVIEWED]
    if retry_failed:
        statuses.append(STATUS_FAILED)
    placeholders = ",".join("?" for _ in statuses)
    query = (
        "SELECT * FROM reels WHERE status IN (" + placeholders + ") "
        "AND scheduled_at IS NOT NULL AND manifest_path IS NOT NULL AND manifest_path != ''"
        + (" AND channel_id=?" if channel_id else "")
    )
    params: list[Any] = list(statuses)
    if channel_id:
        params.append(channel_id)
    rows = conn.execute(query, params).fetchall()
    now_utc = now.astimezone(timezone.utc)

    def ready(row: sqlite3.Row) -> bool:
        if include_future:
            return True
        try:
            parsed = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) <= now_utc

    filtered = [row for row in rows if ready(row)]
    filtered.sort(key=lambda row: _scheduled_sort_key(row["scheduled_at"]))
    return filtered[:limit] if limit is not None else filtered


def published_reels_for_insights(
    conn: sqlite3.Connection,
    channel_id: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Published reels with an Instagram media id, newest first."""
    query = (
        "SELECT * FROM reels WHERE status=? AND media_id IS NOT NULL AND media_id != ''"
        + (" AND channel_id=?" if channel_id else "")
        + " ORDER BY published_at DESC"
    )
    params: list[Any] = [STATUS_PUBLISHED]
    if channel_id:
        params.append(channel_id)
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def latest_insight_rows(
    conn: sqlite3.Connection,
    channel_id: str | None = None,
    limit: int | None = 50,
) -> list[sqlite3.Row]:
    """Published rows joined to their most recent insights snapshot, if any."""
    query = """
        SELECT
          r.content_hash, r.channel_id, r.title, r.published_at, r.permalink, r.media_id,
          r.lang, r.clip_dir, r.media_path, r.source_video, r.caption, r.scheduled_at,
          r.manifest_path,
          i.captured_at, i.views, i.reach, i.likes, i.comments, i.saved, i.shares,
          i.total_interactions, i.raw
        FROM reels r
        LEFT JOIN insights i
          ON i.id = (
            SELECT i2.id
            FROM insights i2
            WHERE i2.content_hash = r.content_hash AND i2.channel_id = r.channel_id
            ORDER BY i2.captured_at DESC, i2.id DESC
            LIMIT 1
          )
        WHERE r.status = ?
    """
    params: list[Any] = [STATUS_PUBLISHED]
    if channel_id:
        query += " AND r.channel_id = ?"
        params.append(channel_id)
    query += " ORDER BY r.published_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()
