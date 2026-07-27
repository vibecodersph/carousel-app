from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import reel_ledger
import reel_scheduler
from scripts import aibrief_jp_reel_checkpoints_v2 as checkpoints_v2
from scripts import run_aibrief_jp_reel_checkpoints as checkpoints


def instagram_metrics(*, views: int = 100, reach: int = 75) -> dict[str, int | float]:
    return {
        "views": views,
        "total_views": views + 20,
        "reach": reach,
        "likes": 4,
        "comments": 1,
        "saved": 2,
        "shares": 1,
        "total_interactions": 8,
        "ig_reels_avg_watch_time": 7100,
        "reels_skip_rate": 48.5,
        "facebook_views": 20,
        "crossposted_views": views + 20,
    }


def insight_payload(metrics: dict[str, object]) -> dict[str, object]:
    return {
        "data": [
            {"name": name, "period": "lifetime", "values": [{"value": value}]}
            for name, value in metrics.items()
        ]
    }


class ReelCheckpointV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir()
        self.instagram_db = self.root / "state" / "reels.db"
        self.facebook_db = self.root / "state" / "facebook.db"
        with reel_ledger.connect(self.instagram_db), reel_ledger.connect(self.facebook_db):
            pass
        channel_dir = self.root / "channels" / checkpoints.CHANNEL
        channel_dir.mkdir(parents=True, exist_ok=True)
        channel_dir.joinpath("channel.json").write_text(
            json.dumps(
                {
                    "publishing": {
                        "facebook_reels": {
                            "mirror_start_at": "2026-07-15T00:00:00+00:00"
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.content_hash = "abcdef1234567890"
        self.instagram_published = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
        self.facebook_published = self.instagram_published + timedelta(minutes=1)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def insert_reel(
        self,
        platform: str,
        *,
        content_hash: str | None = None,
        published_at: datetime | None = None,
        media_id: str | None = None,
        status: str = reel_ledger.STATUS_PUBLISHED,
    ) -> None:
        is_instagram = platform == "instagram"
        db = self.instagram_db if is_instagram else self.facebook_db
        actual = published_at or (
            self.instagram_published if is_instagram else self.facebook_published
        )
        resolved_media_id = media_id or ("ig-media-1" if is_instagram else "fb-video-1")
        permalink = (
            f"https://instagram.com/reel/{resolved_media_id}/"
            if is_instagram
            else f"/reel/{resolved_media_id}/"
        )
        with reel_ledger.connect(db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=content_hash or self.content_hash,
                channel_id=checkpoints.CHANNEL,
                lang="ja",
                clip_dir="/missing/clip",
                media_path="/missing/clip/reel.mp4",
                title="日本語のテストフック",
                status=status,
                scheduled_at=actual.isoformat(),
                published_at=(
                    actual.isoformat()
                    if status == reel_ledger.STATUS_PUBLISHED
                    else None
                ),
                media_id=(
                    resolved_media_id
                    if status == reel_ledger.STATUS_PUBLISHED
                    else None
                ),
                permalink=(
                    permalink if status == reel_ledger.STATUS_PUBLISHED else None
                ),
            )

    def insert_instagram_snapshot(
        self,
        *,
        age: float,
        metrics: dict[str, int | float] | None = None,
    ) -> None:
        values = metrics or instagram_metrics()
        with reel_ledger.connect(self.instagram_db) as connection:
            reel_ledger.record_insight(
                connection,
                content_hash=self.content_hash,
                channel_id=checkpoints.CHANNEL,
                media_id="ig-media-1",
                captured_at=(
                    self.instagram_published + timedelta(hours=age)
                ).isoformat(),
                metrics=values,
                raw=json.dumps(insight_payload(values), ensure_ascii=False),
            )

    def insert_facebook_snapshot(
        self,
        *,
        age: float,
        plays: int = 40,
        captured_from_instagram_clock: bool = False,
    ) -> None:
        native = {
            "fb_reels_total_plays": plays,
            "post_total_media_view_unique": 30,
            "post_video_avg_time_watched": 5200,
            "post_video_likes_by_reaction_type": {"LIKE": 2},
            "post_video_social_actions": {"COMMENT": 1, "SHARE": 1},
        }
        payload = insight_payload(native)
        metrics = reel_scheduler.facebook_reel_insight_metrics(payload)
        clock = (
            self.instagram_published
            if captured_from_instagram_clock
            else self.facebook_published
        )
        with reel_ledger.connect(self.facebook_db) as connection:
            reel_ledger.record_insight(
                connection,
                content_hash=self.content_hash,
                channel_id=checkpoints.CHANNEL,
                media_id="fb-video-1",
                captured_at=(clock + timedelta(hours=age)).isoformat(),
                metrics=metrics,
                raw=json.dumps(payload, ensure_ascii=False),
            )

    def run_main(self, *extra: str) -> int:
        argv = [
            "run_aibrief_jp_reel_checkpoints.py",
            "--root",
            str(self.root),
            "--db",
            str(self.instagram_db),
            "--facebook-db",
            str(self.facebook_db),
            *extra,
        ]
        with patch.object(sys, "argv", argv):
            return checkpoints.main()

    def checkpoint_path(self, key: str = "01h") -> Path:
        return (
            self.root
            / "out"
            / "aibrief_jp_reel_learning"
            / "2026-07-14"
            / "0900_abcdef123456"
            / f"{key}.v2.md"
        )

    def test_paired_report_uses_separate_ids_clocks_and_nonunique_play_sum(self) -> None:
        self.insert_reel("instagram")
        self.insert_reel("facebook")
        self.insert_instagram_snapshot(age=1.20)
        self.insert_facebook_snapshot(age=1.25)
        legacy_path = self.checkpoint_path().with_name("01h.md")
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text("immutable v1\n", encoding="utf-8")

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.facebook_published + timedelta(hours=1.5)).isoformat(),
        )

        self.assertEqual(rc, 0)
        self.assertEqual(legacy_path.read_text(encoding="utf-8"), "immutable v1\n")
        text = self.checkpoint_path().read_text(encoding="utf-8")
        self.assertIn("Distribution mode: `independent_dual_upload`", text)
        self.assertIn("`ig-media-1`", text)
        self.assertIn("`fb-video-1`", text)
        self.assertIn("https://www.facebook.com/reel/fb-video-1/", text)
        self.assertIn("Actual observed age: **1.20h**", text)
        self.assertIn("Actual observed age: **1.25h**", text)
        self.assertIn("| Facebook total plays | 40 | N/A |", text)
        self.assertIn("Combined non-unique plays: **140**", text)
        self.assertIn("Reach is not summed", text)
        self.assertNotIn("Legacy Facebook views from IG object", text)

    def test_v2_preserves_trial_experiment_context(self) -> None:
        self.insert_reel("instagram")
        self.insert_reel("facebook")
        with reel_ledger.connect(self.instagram_db) as connection:
            connection.execute(
                "UPDATE reels SET trial_reel=1, "
                "trial_graduation_strategy='MANUAL' "
                "WHERE content_hash=? AND channel_id=?",
                (self.content_hash, checkpoints.CHANNEL),
            )
            now = self.instagram_published.isoformat()
            connection.execute(
                """
                INSERT INTO trial_experiments (
                  experiment_id, content_hash, channel_id, case_type,
                  parent_media_id, asset_family_id, baseline_hook, variant_hook,
                  changed_variables_json, state, created_at, updated_at
                ) VALUES (
                  'TRIAL-V2', ?, ?, 'successful_post_variant',
                  'parent-media', 'aibrief_jp:source:clip-1', '旧フック', '新フック',
                  '["hook"]', 'active', ?, ?
                )
                """,
                (self.content_hash, checkpoints.CHANNEL, now, now),
            )
        self.insert_instagram_snapshot(age=1.20)
        self.insert_facebook_snapshot(age=1.25)

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.facebook_published + timedelta(hours=1.5)).isoformat(),
        )

        self.assertEqual(rc, 0)
        text = self.checkpoint_path().read_text(encoding="utf-8")
        self.assertIn("Launch type: `TRIAL_REEL`", text)
        self.assertIn("Trial phase at Instagram capture: `PRE_GRADUATION`", text)
        self.assertIn("Trial graduation strategy: `MANUAL`", text)
        self.assertIn("Experiment ID: `TRIAL-V2`", text)
        self.assertIn("Experiment case: `successful_post_variant`", text)
        self.assertIn("Asset family: `aibrief_jp:source:clip-1`", text)
        self.assertIn("Parent media: `parent-media`", text)
        self.assertIn("Baseline hook: 旧フック", text)
        self.assertIn("Trial hook: 新フック", text)

    def test_partial_platform_waits_then_freezes_the_other_side_as_missed(self) -> None:
        self.insert_reel("instagram")
        self.insert_reel("facebook")
        self.insert_instagram_snapshot(age=1.20)

        waiting_rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.facebook_published + timedelta(hours=1.5)).isoformat(),
        )

        self.assertEqual(waiting_rc, 1)
        self.assertFalse(self.checkpoint_path().exists())

        closed_rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.facebook_published + timedelta(hours=2.1)).isoformat(),
        )

        self.assertEqual(closed_rc, 0)
        text = self.checkpoint_path().read_text(encoding="utf-8")
        self.assertIn("- Status: `PARTIAL_CHECKPOINT`", text)
        self.assertIn("| Instagram | published | `RECORDED` |", text)
        self.assertIn("| Facebook | published | `MISSED_CHECKPOINT` |", text)
        self.assertIn("Combined non-unique plays: unavailable", text)
        frozen = self.checkpoint_path().read_bytes()

        self.insert_facebook_snapshot(age=1.25)
        rerun_rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.facebook_published + timedelta(hours=2.2)).isoformat(),
        )

        self.assertEqual(rerun_rc, 0)
        self.assertEqual(self.checkpoint_path().read_bytes(), frozen)

    def test_facebook_snapshot_window_uses_facebook_publication_clock(self) -> None:
        self.insert_reel("instagram")
        self.insert_reel(
            "facebook",
            published_at=self.instagram_published + timedelta(hours=1),
        )
        self.facebook_published = self.instagram_published + timedelta(hours=1)
        self.insert_instagram_snapshot(age=1.20)
        self.insert_facebook_snapshot(
            age=1.20,
            captured_from_instagram_clock=True,
        )

        wrong_clock_rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.facebook_published + timedelta(hours=1.5)).isoformat(),
        )

        self.assertEqual(wrong_clock_rc, 1)
        self.assertFalse(self.checkpoint_path().exists())

        self.insert_facebook_snapshot(age=1.20, plays=45)
        correct_clock_rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.facebook_published + timedelta(hours=1.5)).isoformat(),
        )

        self.assertEqual(correct_clock_rc, 0)
        text = self.checkpoint_path().read_text(encoding="utf-8")
        self.assertIn("| Facebook total plays | 45 | N/A |", text)
        self.assertIn("Actual observed age: **1.20h**", text)

    def test_due_sync_is_exact_and_uses_platform_specific_metric_sets(self) -> None:
        self.insert_reel("instagram")
        self.insert_reel("facebook")

        with patch.object(
            checkpoints_v2.legacy,
            "run_exact_sync",
            return_value=0,
        ) as sync:
            rc = self.run_main(
                "--checkpoint",
                "01h",
                "--as-of",
                (self.facebook_published + timedelta(hours=1.5)).isoformat(),
            )

        self.assertEqual(rc, 1)
        self.assertEqual(sync.call_count, 2)
        calls = {
            call.kwargs["platform"]: call.kwargs for call in sync.call_args_list
        }
        self.assertEqual(calls["instagram"]["media_ids"], ["ig-media-1"])
        self.assertNotIn("facebook_views", calls["instagram"]["metrics"])
        self.assertNotIn("crossposted_views", calls["instagram"]["metrics"])
        self.assertEqual(calls["facebook"]["media_ids"], ["fb-video-1"])
        self.assertEqual(
            calls["facebook"]["metrics"],
            reel_scheduler.FACEBOOK_INSIGHT_REQUEST_METRIC_KEYS,
        )
        self.assertFalse(self.checkpoint_path().exists())

    def test_legacy_crosspost_report_uses_existing_aggregate_without_adding(self) -> None:
        self.insert_reel("instagram")
        self.insert_instagram_snapshot(age=1.20)

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.instagram_published + timedelta(hours=1.5)).isoformat(),
        )

        self.assertEqual(rc, 0)
        text = self.checkpoint_path().read_text(encoding="utf-8")
        self.assertIn("Distribution mode: `legacy_crosspost`", text)
        self.assertIn("Legacy crossposted views: **120**", text)
        self.assertIn("is never added to Instagram views", text)
        self.assertNotIn("Combined non-unique plays:", text)

    def test_skipped_facebook_tombstone_does_not_reclassify_old_crosspost(self) -> None:
        cutoff = datetime(2026, 7, 24, 11, 58, tzinfo=timezone.utc)
        instagram = {
            "status": reel_ledger.STATUS_PUBLISHED,
            "scheduled_at": cutoff.isoformat(),
            "published_at": (cutoff - timedelta(days=1)).isoformat(),
            "media_id": "ig-old",
        }
        facebook = {
            "status": reel_ledger.STATUS_SKIPPED,
            "scheduled_at": cutoff.isoformat(),
            "published_at": None,
            "media_id": None,
        }

        mode = checkpoints_v2.classify_mode(instagram, facebook, cutoff)

        self.assertEqual(mode, "legacy_crosspost")

    def test_actual_publication_time_takes_priority_over_old_schedule(self) -> None:
        cutoff = datetime(2026, 7, 24, 11, 58, tzinfo=timezone.utc)
        instagram = {
            "status": reel_ledger.STATUS_PUBLISHED,
            "scheduled_at": (cutoff - timedelta(hours=1)).isoformat(),
            "published_at": (cutoff + timedelta(minutes=5)).isoformat(),
            "media_id": "ig-after-cutover",
        }

        mode = checkpoints_v2.classify_mode(instagram, None, cutoff)

        self.assertEqual(mode, "independent_dual_upload")

    def test_independent_cutover_requires_an_explicit_timezone(self) -> None:
        with self.assertRaisesRegex(SystemExit, "timezone-aware"):
            checkpoints_v2.configured_independent_start(
                self.root,
                "2026-07-24T20:58:00",
            )

    def test_stale_timestamp_on_skipped_row_does_not_move_output_anchor(self) -> None:
        unit = {
            "content_hash": self.content_hash,
            "instagram": {
                "status": reel_ledger.STATUS_PUBLISHED,
                "published_at": self.instagram_published.isoformat(),
            },
            "facebook": {
                "status": reel_ledger.STATUS_SKIPPED,
                "published_at": (
                    self.instagram_published - timedelta(days=2)
                ).isoformat(),
            },
        }

        anchor = checkpoints_v2.anchor_at(unit)

        self.assertEqual(anchor, self.instagram_published)

    def test_published_row_without_media_id_reaches_terminal_report(self) -> None:
        with reel_ledger.connect(self.instagram_db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=self.content_hash,
                channel_id=checkpoints.CHANNEL,
                lang="ja",
                clip_dir="/missing/clip",
                media_path="/missing/clip/reel.mp4",
                title="missing id",
                status=reel_ledger.STATUS_PUBLISHED,
                scheduled_at=self.instagram_published.isoformat(),
                published_at=self.instagram_published.isoformat(),
                media_id=None,
            )

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.instagram_published + timedelta(hours=2.1)).isoformat(),
        )

        self.assertEqual(rc, 0)
        text = self.checkpoint_path().read_text(encoding="utf-8")
        self.assertIn("- Status: `MISSED_CHECKPOINT`", text)
        self.assertIn("| Instagram | published | `MEDIA_ID_MISSING` |", text)

    def test_post_cutover_missing_facebook_row_is_explicitly_terminal(self) -> None:
        channel_dir = self.root / "channels" / checkpoints.CHANNEL
        channel_dir.mkdir(parents=True, exist_ok=True)
        channel_dir.joinpath("channel.json").write_text(
            json.dumps(
                {
                    "publishing": {
                        "facebook_reels": {
                            "mirror_start_at": self.instagram_published.isoformat()
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.insert_reel("instagram")
        self.insert_instagram_snapshot(age=1.20)

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--independent-start-at",
            self.instagram_published.isoformat(),
            "--as-of",
            (self.instagram_published + timedelta(hours=2.1)).isoformat(),
        )

        self.assertEqual(rc, 0)
        text = self.checkpoint_path().read_text(encoding="utf-8")
        self.assertIn("Distribution mode: `independent_dual_upload`", text)
        self.assertIn("| Facebook | missing | `NOT_PUBLISHED` |", text)
        self.assertIn("| Instagram | published | `RECORDED` |", text)


if __name__ == "__main__":
    unittest.main()
