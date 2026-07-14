from __future__ import annotations

import json
import argparse
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import reel_ledger
import reel_scheduler
from scripts import run_aibrief_jp_reel_checkpoints as checkpoints


def raw_payload(metrics: dict[str, int | float]) -> str:
    return json.dumps(
        {
            "data": [
                {"name": name, "period": "lifetime", "values": [{"value": value}]}
                for name, value in metrics.items()
            ]
        }
    )


def complete_metrics(*, views: int, reach: int) -> dict[str, int | float]:
    return {
        "views": views,
        "total_views": views + 25,
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


class ReelCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "state").mkdir()
        self.db = self.root / "state" / "reels.db"
        self.published = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def insert_reel(
        self,
        *,
        content_hash: str = "abcdef1234567890",
        media_id: str = "media-1",
    ) -> None:
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=content_hash,
                channel_id=checkpoints.CHANNEL,
                lang="ja",
                clip_dir="/missing/clip",
                media_path="/missing/clip/reel.mp4",
                title="日本語のテストフック",
                status=reel_ledger.STATUS_PUBLISHED,
                published_at=self.published.isoformat(),
                media_id=media_id,
                permalink=f"https://instagram.com/reel/{media_id}/",
            )

    def insert_snapshot(
        self,
        *,
        age: float,
        metrics: dict[str, int | float],
        content_hash: str = "abcdef1234567890",
        media_id: str = "media-1",
    ) -> None:
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.record_insight(
                connection,
                content_hash=content_hash,
                channel_id=checkpoints.CHANNEL,
                media_id=media_id,
                captured_at=(self.published + timedelta(hours=age)).isoformat(),
                metrics=metrics,
                raw=raw_payload(metrics),
            )

    def run_main(self, *extra: str) -> int:
        argv = [
            "run_aibrief_jp_reel_checkpoints.py",
            "--root",
            str(self.root),
            "--db",
            str(self.db),
            *extra,
        ]
        with patch.object(sys, "argv", argv):
            return checkpoints.main()

    def test_exact_media_filter_returns_only_requested_published_reel(self) -> None:
        self.insert_reel()
        self.insert_reel(content_hash="second-hash", media_id="media-2")

        with reel_ledger.connect(self.db) as connection:
            rows = reel_ledger.published_reels_for_insights(
                connection,
                checkpoints.CHANNEL,
                media_ids=["media-2"],
            )

        self.assertEqual([row["media_id"] for row in rows], ["media-2"])

    def test_sync_command_fetches_only_exact_requested_media_id(self) -> None:
        self.insert_reel()
        self.insert_reel(content_hash="second-hash", media_id="media-2")
        args = argparse.Namespace(
            platform="instagram",
            channel=checkpoints.CHANNEL,
            db=self.db,
            limit=None,
            media_id=["media-2"],
            dry_run=False,
            metrics=",".join(reel_scheduler.INSTAGRAM_INSIGHT_REQUEST_METRIC_KEYS),
            access_token="",
            graph_api_version="v25.0",
            graph_api_root="https://graph.instagram.com",
        )
        payload = json.loads(raw_payload(complete_metrics(views=100, reach=75)))

        with patch("instagram_publish.load_env_file"), patch(
            "instagram_publish.resolve_instagram_access_token",
            return_value=("token", "test"),
        ), patch.object(
            reel_scheduler,
            "fetch_instagram_insights_resilient",
            return_value=(payload, []),
        ) as fetch:
            rc = reel_scheduler.sync_insights_command(args)

        self.assertEqual(rc, 0)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["media_id"], "media-2")
        with reel_ledger.connect(self.db) as connection:
            media_ids = [
                row["media_id"]
                for row in connection.execute("SELECT media_id FROM insights")
            ]
        self.assertEqual(media_ids, ["media-2"])

    def test_one_hour_file_uses_first_core_valid_snapshot_in_window(self) -> None:
        self.insert_reel()
        invalid = complete_metrics(views=80, reach=60)
        invalid.pop("shares")
        self.insert_snapshot(age=1.05, metrics=invalid)
        self.insert_snapshot(age=1.20, metrics=complete_metrics(views=100, reach=75))
        self.insert_snapshot(age=1.60, metrics=complete_metrics(views=999, reach=900))

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.published + timedelta(hours=1.75)).isoformat(),
        )

        self.assertEqual(rc, 0)
        path = (
            self.root
            / "out"
            / "aibrief_jp_reel_learning"
            / "2026-07-14"
            / "0900_abcdef123456"
            / "01h.md"
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("Actual observed age: **1.20h**", text)
        self.assertIn("| Instagram views | 100 | N/A |", text)
        self.assertNotIn("999", text)
        self.assertIn("Meta all-surface views", text)
        self.assertIn("never added together", text)

    def test_three_hour_file_has_delta_and_is_immutable_on_rerun(self) -> None:
        self.insert_reel()
        self.insert_snapshot(age=1.20, metrics=complete_metrics(views=100, reach=75))
        self.insert_snapshot(age=3.25, metrics=complete_metrics(views=180, reach=130))
        as_of = (self.published + timedelta(hours=3.5)).isoformat()

        first_rc = self.run_main(
            "--no-sync", "--checkpoint", "03h", "--as-of", as_of
        )
        path = (
            self.root
            / "out"
            / "aibrief_jp_reel_learning"
            / "2026-07-14"
            / "0900_abcdef123456"
            / "03h.md"
        )
        first = path.read_bytes()
        self.insert_snapshot(age=3.75, metrics=complete_metrics(views=900, reach=800))
        second_rc = self.run_main(
            "--no-sync", "--checkpoint", "03h", "--as-of", as_of
        )

        self.assertEqual((first_rc, second_rc), (0, 0))
        self.assertEqual(path.read_bytes(), first)
        text = first.decode("utf-8")
        self.assertIn("| Instagram views | 180 | +80 |", text)
        self.assertIn("Compared with: +1h at 1.20h", text)

    def test_past_window_writes_missed_without_substituting_late_snapshot(self) -> None:
        self.insert_reel()
        self.insert_snapshot(age=5.0, metrics=complete_metrics(views=500, reach=400))

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "03h",
            "--as-of",
            (self.published + timedelta(hours=6)).isoformat(),
        )

        self.assertEqual(rc, 0)
        path = (
            self.root
            / "out"
            / "aibrief_jp_reel_learning"
            / "2026-07-14"
            / "0900_abcdef123456"
            / "03h.md"
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("`MISSED_CHECKPOINT`", text)
        self.assertIn("later lifetime value was not substituted", text)
        self.assertNotIn("500", text)

    def test_due_no_sync_waits_without_writing_false_checkpoint(self) -> None:
        self.insert_reel()

        rc = self.run_main(
            "--no-sync",
            "--checkpoint",
            "01h",
            "--as-of",
            (self.published + timedelta(hours=1.5)).isoformat(),
        )

        self.assertEqual(rc, 1)
        self.assertFalse((self.root / "out").exists())

    def test_dry_run_never_writes_or_syncs(self) -> None:
        self.insert_reel()
        with patch.object(checkpoints, "run_exact_sync") as sync:
            rc = self.run_main(
                "--dry-run",
                "--as-of",
                (self.published + timedelta(hours=3.5)).isoformat(),
            )

        self.assertEqual(rc, 0)
        sync.assert_not_called()
        self.assertFalse((self.root / "out").exists())


if __name__ == "__main__":
    unittest.main()
