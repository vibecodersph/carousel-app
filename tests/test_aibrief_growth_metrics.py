from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import aibrief_growth_metrics


class AibriefGrowthMetricsTests(unittest.TestCase):
    def test_schedule_report_flags_manual_live_conflict_and_metrics_only_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_path = root / "growth_publish_queue.json"
            metrics_path = root / "metrics_log.csv"
            db_path = root / "reels.db"

            queue_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "growth-hidden-agents-20260704",
                                "recommendedPublishAt": "2026-07-04T20:30:00+09:00",
                                "hook": "Hidden agents",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with metrics_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "post_id",
                        "posted_at_jst",
                        "followers_before",
                        "followers_24h",
                        "reach_24h",
                        "saves_24h",
                        "shares_24h",
                        "follows_24h",
                    ],
                )
                writer.writeheader()
                writer.writerow({"post_id": "growth-hidden-agents-20260704"})
                writer.writerow({"post_id": "growth-product-safety-stakes-20260708"})

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table reels (
                        content_hash text,
                        channel_id text,
                        scheduled_at text,
                        source_video text,
                        title text,
                        status text,
                        caption text
                    )
                    """
                )
                conn.executemany(
                    "insert into reels values (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("hash-a", "aibrief_jp", "2026-07-04T09:00:00+09:00", "src-a", "Morning", "scheduled", "new follow CTA"),
                        ("hash-b", "aibrief_jp", "2026-07-04T13:00:00+09:00", "src-b", "Noon", "scheduled", "new follow CTA"),
                        ("hash-c", "aibrief_jp", "2026-07-04T19:00:00+09:00", "src-a", "Evening", "scheduled", "old caption 気になったら保存して"),
                        ("hash-d", "aibrief_jp", "2026-07-08T09:00:00+09:00", "src-d", "Draft day", "scheduled", "old caption"),
                        ("hash-x", "other", "2026-07-04T09:00:00+09:00", "src-x", "Wrong channel", "scheduled", "old caption"),
                    ],
                )

            report = aibrief_growth_metrics.render_schedule_report(
                queue_path,
                metrics_path,
                db_path,
                "aibrief_jp",
            )

        self.assertIn("Manual days with 3+ live scheduled Reels: 1", report)
        self.assertIn("approval needed: replace one live Reel", report)
        self.assertIn("| 2026-07-04 | growth-hidden-agents-20260704 | unknown / unknown | 20:30 | 3 (09:00, 13:00, 19:00) | 90 |", report)
        self.assertIn("hash-a", report)
        self.assertIn("growth-product-safety-stakes-20260708", report)
        self.assertIn("prepared asset is tracked in metrics but not in the manual queue", report)
        self.assertNotIn("Wrong channel", report)

    def test_replacement_report_picks_explainable_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_path = root / "growth_publish_queue.json"
            metrics_path = root / "metrics_log.csv"
            db_path = root / "reels.db"

            queue_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "growth-hidden-agents-20260704",
                                "recommendedPublishAt": "2026-07-04T20:30:00+09:00",
                                "status": "ready_for_approval",
                                "format": "instagram_reel",
                                "hook": "Hidden agents",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with metrics_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["post_id"])
                writer.writeheader()
                writer.writerow({"post_id": "growth-hidden-agents-20260704"})

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table reels (
                        content_hash text,
                        channel_id text,
                        scheduled_at text,
                        source_video text,
                        title text,
                        status text,
                        caption text
                    )
                    """
                )
                conn.executemany(
                    "insert into reels values (?, ?, ?, ?, ?, ?, ?)",
                    [
                        ("hash-a", "aibrief_jp", "2026-07-04T09:00:00+09:00", "src-a", "Morning", "scheduled", aibrief_growth_metrics.FOLLOW_CTA),
                        ("hash-b", "aibrief_jp", "2026-07-04T13:00:00+09:00", "src-b", "Noon", "scheduled", aibrief_growth_metrics.FOLLOW_CTA),
                        ("hash-c", "aibrief_jp", "2026-07-04T19:00:00+09:00", "src-a", "Evening", "scheduled", f"{aibrief_growth_metrics.OLD_SAVE_CTA} old"),
                    ],
                )

            report = aibrief_growth_metrics.render_replacement_report(
                queue_path,
                metrics_path,
                db_path,
                "aibrief_jp",
            )

        self.assertIn("hash-c", report)
        self.assertIn("same-day source repeat", report)
        self.assertIn("closest live row to manual post", report)
        self.assertIn("approve replace/postpone before posting manual asset", report)

    def test_tail_risk_report_recommends_fresh_diverse_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reels.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table reels (
                        content_hash text,
                        channel_id text,
                        scheduled_at text,
                        source_video text,
                        title text,
                        status text
                    )
                    """
                )
                conn.executemany(
                    "insert into reels values (?, ?, ?, ?, ?, ?)",
                    [
                        ("hash-a", "aibrief_jp", "2026-07-04T09:00:00+09:00", "src-a", "A1", "scheduled"),
                        ("hash-b", "aibrief_jp", "2026-07-04T13:00:00+09:00", "src-a", "A2", "scheduled"),
                        ("hash-c", "aibrief_jp", "2026-07-04T19:00:00+09:00", "src-a", "A3", "scheduled"),
                        ("hash-d", "aibrief_jp", "2026-07-04T22:00:00+09:00", "src-b", "B1", "scheduled"),
                        ("hash-e", "aibrief_jp", "2026-07-05T09:00:00+09:00", "src-a", "A4", "scheduled"),
                        ("hash-x", "other", "2026-07-04T09:00:00+09:00", "src-x", "Wrong", "scheduled"),
                    ],
                )

            report = aibrief_growth_metrics.render_tail_risk_report(
                db_path,
                "aibrief_jp",
                target_daily_slots=4,
                refill_days=2,
                source_share_limit=0.5,
            )

        self.assertIn("# Post-Reflow Tail Risk Report", report)
        self.assertIn("Queued rows: 5", report)
        self.assertIn("Top source: `src-a` (4/5, 80.0%)", report)
        self.assertIn("Prepare at least **8** fresh approved Reels", report)
        self.assertIn("add at least **3** non-`src-a` rows", report)
        self.assertNotIn("Wrong", report)

    def test_tail_replacement_plan_lists_exact_overrepresented_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "reels.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table reels (
                        content_hash text,
                        channel_id text,
                        scheduled_at text,
                        source_video text,
                        title text,
                        status text
                    )
                    """
                )
                conn.executemany(
                    "insert into reels values (?, ?, ?, ?, ?, ?)",
                    [
                        ("hash-a", "aibrief_jp", "2026-07-14T09:00:00+09:00", "src-a", "Before scope", "scheduled"),
                        ("hash-b", "aibrief_jp", "2026-07-15T09:00:00+09:00", "src-a", "Claude work story", "scheduled"),
                        ("hash-c", "aibrief_jp", "2026-07-15T13:00:00+09:00", "src-a", "Anthropic launch", "scheduled"),
                        ("hash-d", "aibrief_jp", "2026-07-15T19:00:00+09:00", "src-b", "Fresh research", "scheduled"),
                        ("hash-e", "aibrief_jp", "2026-07-16T09:00:00+09:00", "src-a", "AI productivity", "scheduled"),
                    ],
                )

            report = aibrief_growth_metrics.render_tail_replacement_plan(
                db_path,
                "aibrief_jp",
                start_date="2026-07-15",
                source_share_limit=0.4,
            )

        self.assertIn("# Tail Diversity Replacement Plan", report)
        self.assertIn("Replacements needed to hit cap without adding new rows: 2", report)
        self.assertIn("hash-b", report)
        self.assertIn("hash-c", report)
        self.assertIn("Claude/Anthropic-heavy topic", report)
        self.assertNotIn("Before scope", report)

    def test_source_intake_report_compares_backlog_to_reel_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backlog_path = root / "source_backlog.json"
            outputs_root = root / "outputs"
            ready_source = outputs_root / "vid-ready"
            dry_run_source = outputs_root / "vid-dry-run"
            second_batch_dry_run_source = outputs_root / "vid-second-dry-run"
            missing_source_id = "vid-missing"
            second_batch_missing_source_id = "vid-second-missing"
            old_source = outputs_root / "old-source"
            for clip in ["001", "002"]:
                clip_dir = ready_source / "clips" / clip
                clip_dir.mkdir(parents=True, exist_ok=True)
                (clip_dir / "reel.ja.aibrief_jp.mp4").write_text("mp4", encoding="utf-8")
            (ready_source / "metadata.json").write_text(
                json.dumps({"title": "Ready source title", "channel": "Ready channel"}),
                encoding="utf-8",
            )
            dry_run_source.mkdir(parents=True, exist_ok=True)
            (dry_run_source / "metadata.json").write_text(
                json.dumps({"title": "Dry run source title", "channel": "Dry run channel"}),
                encoding="utf-8",
            )
            (dry_run_source / "candidates.json").write_text(
                json.dumps({"clips": [{"index": 1}, {"index": 2}]}),
                encoding="utf-8",
            )
            second_batch_dry_run_source.mkdir(parents=True, exist_ok=True)
            (second_batch_dry_run_source / "metadata.json").write_text(
                json.dumps({"title": "Second dry run source title", "channel": "Second dry run channel"}),
                encoding="utf-8",
            )
            (second_batch_dry_run_source / "candidates.json").write_text(
                json.dumps({"clips": [{"index": 1}]}),
                encoding="utf-8",
            )
            old_clip = old_source / "clips" / "001"
            old_clip.mkdir(parents=True, exist_ok=True)
            (old_clip / "reel.ja.aibrief_jp.mp4").write_text("mp4", encoding="utf-8")
            (old_source / "metadata.json").write_text(
                json.dumps({"title": "Old source title", "uploader": "Old channel", "upload_date": "20260217"}),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "targets": {"freshApprovedRows": 3},
                        "sources": [
                            {
                                "priority": 1,
                                "batch": "process_first",
                                "youtubeId": "vid-ready",
                                "title": "Ready backlog title",
                                "channel": "Backlog channel",
                                "suggestedCap": 2,
                            },
                            {
                                "priority": 2,
                                "batch": "process_first",
                                "youtubeId": "vid-dry-run",
                                "title": "Dry run backlog title",
                                "channel": "Dry run backlog channel",
                                "suggestedCap": 2,
                            },
                            {
                                "priority": 3,
                                "batch": "second_batch",
                                "youtubeId": "vid-second-dry-run",
                                "title": "Second dry run backlog title",
                                "channel": "Second dry run backlog channel",
                                "suggestedCap": 1,
                            },
                            {
                                "priority": 4,
                                "batch": "process_first",
                                "youtubeId": missing_source_id,
                                "title": "Missing backlog title",
                                "channel": "Missing channel",
                                "suggestedCap": 2,
                            },
                            {
                                "priority": 5,
                                "batch": "second_batch",
                                "youtubeId": second_batch_missing_source_id,
                                "title": "Second missing backlog title",
                                "channel": "Second missing channel",
                                "suggestedCap": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = aibrief_growth_metrics.render_source_intake_report(
                backlog_path,
                outputs_root,
                "aibrief_jp",
            )

        self.assertIn("Backlog sources with ready `aibrief_jp` clips: 1", report)
        self.assertIn("Backlog sources with dry-run candidates only: 2", report)
        self.assertIn("First-batch sources still missing ready clips: 2", report)
        self.assertIn("First-batch sources not started in outputs: 1", report)
        self.assertIn("Ready clips from backlog sources: 2/3 target fresh rows", report)
        self.assertIn("| 1 | process_first | `vid-ready` | Ready backlog title - Backlog channel | 2 | 2 | 0 | ready_for_review |", report)
        self.assertIn("| 2 | process_first | `vid-dry-run` | Dry run backlog title - Dry run backlog channel | 2 | 0 | 2 | dry_run_only |", report)
        self.assertIn("| 3 | second_batch | `vid-second-dry-run` | Second dry run backlog title - Second dry run backlog channel | 1 | 0 | 1 | dry_run_only |", report)
        self.assertIn("| 4 | process_first | `vid-missing` | Missing backlog title - Missing channel | 2 | 0 | 0 | missing_from_outputs |", report)
        self.assertIn("| 5 | second_batch | `vid-second-missing` | Second missing backlog title - Second missing channel | 1 | 0 | 0 | missing_from_outputs |", report)
        self.assertIn("Suppressed stale output folders: 1", report)
        self.assertNotIn("| `old-source` | 1 | Old source title - Old channel |", report)
        self.assertIn("Dry-run candidates exist but still need rights review", report)
        self.assertIn("`vid-second-dry-run`", report)
        self.assertIn("Dry-run/process the first-batch source videos not started", report)
        self.assertIn("Backlog sources still not started", report)
        self.assertIn("`vid-second-missing`", report)
        self.assertIn("Need 1 more approved clips", report)

    def test_approval_queue_accepts_fresh_fail_closed_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "approval_queue.json"
            queue_path.write_text(json.dumps(self._approval_queue()), encoding="utf-8")

            issues = aibrief_growth_metrics.approval_queue_issues(queue_path)

        self.assertEqual([], issues)

    def test_approval_queue_rejects_stale_source_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue = self._approval_queue()
            queue["candidates"][0]["upload_date"] = "2026-02-17"
            queue["candidates"][0]["source"]["upload_date"] = "2026-02-17"
            queue_path = Path(tmp) / "approval_queue.json"
            queue_path.write_text(json.dumps(queue), encoding="utf-8")

            issues = aibrief_growth_metrics.approval_queue_issues(queue_path)

        self.assertIn(
            "youtube:fresh0:10-20.upload_date 2026-02-17 is before freshness cutoff 2026-03-03",
            issues,
        )

    def test_published_metric_coverage_requires_latest_scheduler_publish_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "reels.db"
            metrics_path = root / "metrics_log.csv"

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    create table reels (
                        content_hash text,
                        channel_id text,
                        scheduled_at text,
                        source_video text,
                        title text,
                        status text,
                        published_at text,
                        media_id text,
                        permalink text
                    )
                    """
                )
                conn.executemany(
                    "insert into reels values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "oldhash000000",
                            "aibrief_jp",
                            "2026-07-03T09:00:00+09:00",
                            "old-source",
                            "Old publish",
                            "published",
                            "2026-07-03T00:10:00+00:00",
                            "old-media",
                            "https://www.instagram.com/reel/old/",
                        ),
                        (
                            "latesthash1234567890",
                            "aibrief_jp",
                            "2026-07-03T19:00:00+09:00",
                            "fresh-source",
                            "Latest publish",
                            "published",
                            "2026-07-03T10:17:20+00:00",
                            "latest-media",
                            "https://www.instagram.com/reel/latest/",
                        ),
                    ],
                )

            with metrics_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["post_id", "instagram_media_id", "permalink"])
                writer.writeheader()
                writer.writerow({"post_id": "older", "instagram_media_id": "old-media", "permalink": "https://www.instagram.com/reel/old/"})

            issues = aibrief_growth_metrics.published_metric_coverage_issues(
                db_path,
                metrics_path,
                "aibrief_jp",
            )
            self.assertEqual(1, len(issues))
            self.assertIn("latest-media missing metrics row", issues[0])

            with metrics_path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["post_id", "instagram_media_id", "permalink"])
                writer.writerow({"post_id": "auto-latesthash12-20260703", "instagram_media_id": "latest-media", "permalink": ""})

            self.assertEqual(
                [],
                aibrief_growth_metrics.published_metric_coverage_issues(
                    db_path,
                    metrics_path,
                    "aibrief_jp",
                ),
            )

    def _approval_queue(self) -> dict[str, object]:
        gates = {
            "render": False,
            "replace_existing": False,
            "enqueue": False,
            "publish": False,
            "boost": False,
            "profile_edit": False,
            "follow_or_unfollow": False,
            "comment_or_dm": False,
        }

        def candidate(index: int) -> dict[str, object]:
            start = 10 + index
            end = 20 + index
            video_id = f"fresh{index}"
            return {
                "candidate_id": f"youtube:{video_id}:{start}-{end}",
                "priority_group": "top_five",
                "rank": index + 1,
                "review_status": "pending_manual_watch",
                "approved": False,
                "approved_actions": [],
                "approved_by": None,
                "approved_at": None,
                "upload_date": "2026-06-28",
                "published_after_cutoff": True,
                "source": {
                    "platform": "youtube",
                    "video_id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}&t={start}s",
                    "upload_date": "2026-06-28",
                    "start_seconds": start,
                    "end_seconds": end,
                },
            }

        return {
            "schema_version": "1.0",
            "queue_type": "instagram_dry_run_source_candidate_approval",
            "dry_run_only": True,
            "requires_manual_approval": True,
            "freshness_cutoff": "2026-03-03",
            "side_effect_gates": gates,
            "candidates": [candidate(index) for index in range(5)],
        }


if __name__ == "__main__":
    unittest.main()
