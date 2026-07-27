from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import reel_ledger
from scripts import select_aibrief_jp_trial_candidates as selector


CHANNEL = "aibrief_jp"


def raw_payload(metrics: dict[str, int]) -> str:
    return json.dumps(
        {
            "data": [
                {
                    "name": name,
                    "period": "lifetime",
                    "values": [{"value": value}],
                }
                for name, value in metrics.items()
            ]
        }
    )


class TrialCandidateSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "reels.db"
        self.report = self.root / "report.json"
        self.facebook_db = self.root / "missing-facebook.db"
        self.as_of = datetime(2026, 7, 27, 13, 30, tzinfo=selector.JST)
        self.report.write_text(
            json.dumps({"generated_at": self.as_of.isoformat(), "items": []}),
            encoding="utf-8",
        )
        with reel_ledger.connect(self.db):
            pass

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_assets(self, name: str, *, manifest: bool = False) -> tuple[Path, Path, Path]:
        clip_dir = self.root / "clips" / name
        clip_dir.mkdir(parents=True)
        media = clip_dir / "reel.ja.aibrief_jp.mp4"
        media.write_bytes(b"video-" + name.encode())
        (clip_dir / "subtitles.ja.ass").write_text("[Events]\n", encoding="utf-8")
        (clip_dir / "notes.json").write_text("{}", encoding="utf-8")
        (clip_dir / "one_liners.json").write_text("{}", encoding="utf-8")
        source_media = self.root / "work" / "source.mp4"
        source_media.parent.mkdir(exist_ok=True)
        source_media.write_bytes(b"source")
        manifest_path = clip_dir / "manifest.json"
        if manifest:
            manifest_path.write_text("{}", encoding="utf-8")
        return clip_dir, media, manifest_path

    def insert_published(
        self,
        *,
        content_hash: str,
        title: str,
        published_at: datetime,
        snapshot_age: float,
        views: int,
        reach: int,
        interactions: int,
        saved: int,
        shares: int,
    ) -> None:
        clip_dir, media, _ = self.make_assets(content_hash)
        media_id = f"media-{content_hash}"
        metrics = {
            "views": views,
            "total_views": views + 5,
            "reach": reach,
            "likes": max(0, interactions - saved - shares),
            "comments": 0,
            "saved": saved,
            "shares": shares,
            "total_interactions": interactions,
        }
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=content_hash,
                channel_id=CHANNEL,
                lang="ja",
                clip_dir=clip_dir,
                media_path=media,
                source_video=f"source-{content_hash}",
                title=title,
                status=reel_ledger.STATUS_PUBLISHED,
                published_at=published_at.isoformat(),
                media_id=media_id,
            )
            reel_ledger.record_insight(
                connection,
                content_hash=content_hash,
                channel_id=CHANNEL,
                media_id=media_id,
                captured_at=(published_at + timedelta(hours=snapshot_age)).isoformat(),
                metrics=metrics,
                raw=raw_payload(metrics),
            )

    def insert_scheduled(
        self,
        *,
        content_hash: str,
        scheduled_at: datetime,
        title: str | None = None,
    ) -> None:
        clip_dir, media, manifest = self.make_assets(content_hash, manifest=True)
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=content_hash,
                channel_id=CHANNEL,
                lang="ja",
                clip_dir=clip_dir,
                media_path=media,
                source_video=f"source-{content_hash}",
                title=title or content_hash,
                status=reel_ledger.STATUS_SCHEDULED,
                scheduled_at=scheduled_at.isoformat(),
                manifest_path=str(manifest),
            )

    def test_cycle_alternates_and_balances_every_lane_across_every_slot(self) -> None:
        cycle = [selector.cycle_for_ordinal(index) for index in range(8)]
        self.assertEqual(
            [item["lane"] for item in cycle],
            [
                "successful_post_variant",
                "scheduled_conversion",
                "successful_post_variant",
                "scheduled_conversion",
                "successful_post_variant",
                "scheduled_conversion",
                "successful_post_variant",
                "scheduled_conversion",
            ],
        )
        for lane in ("successful_post_variant", "scheduled_conversion"):
            self.assertEqual(
                {
                    item["target_slot"]
                    for item in cycle
                    if item["lane"] == lane
                },
                {"09", "13", "18", "21"},
            )

    def test_pilot_starts_formal_cycle_at_scheduled_conversion_18(self) -> None:
        experiments = [{"experiment_id": "PILOT-000"}]
        ordinal = selector.next_cycle_ordinal(experiments)
        self.assertEqual(ordinal, 1)
        self.assertEqual(
            selector.cycle_for_ordinal(ordinal),
            {
                "ordinal": 1,
                "position": 1,
                "lane": "scheduled_conversion",
                "target_slot": "18",
            },
        )

    def test_parent_ranking_uses_only_strict_72_to_96_hour_snapshots(self) -> None:
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        self.insert_published(
            content_hash="audience-fit",
            title="Audience fit",
            published_at=published,
            snapshot_age=72,
            views=202,
            reach=154,
            interactions=9,
            saved=4,
            shares=2,
        )
        self.insert_published(
            content_hash="complete",
            title="Complete",
            published_at=published,
            snapshot_age=96,
            views=300,
            reach=220,
            interactions=10,
            saved=4,
            shares=2,
        )
        self.insert_published(
            content_hash="late",
            title="Late snapshot",
            published_at=published,
            snapshot_age=96.01,
            views=400,
            reach=300,
            interactions=20,
            saved=10,
            shares=5,
        )
        rows, experiments = selector.load_ledger_state(self.db, channel_id=CHANNEL)
        candidates, exclusions = selector.published_parent_candidates(
            report_path=self.report,
            db_path=self.db,
            ledger_rows=rows,
            channel_id=CHANNEL,
            as_of=self.as_of,
            tested_parents=set(),
            tested_families=set(),
        )
        self.assertEqual(
            [item["content_hash"] for item in candidates],
            ["audience-fit", "complete"],
        )
        self.assertEqual(candidates[0]["snapshot_age_hours"], 72)
        self.assertEqual(candidates[1]["snapshot_age_hours"], 96)
        self.assertEqual(exclusions["NO_STRICT_72_96_SNAPSHOT"], 1)
        self.assertEqual(experiments, [])

    def test_next_three_use_lowest_stable_hash_not_notes_score_or_sql_order(self) -> None:
        ordinal = 1
        target_slot = "18"
        rows: list[dict[str, object]] = []
        for index, content_hash in enumerate(("first", "second", "third", "fourth")):
            scheduled = self.as_of + timedelta(days=index + 2)
            scheduled = scheduled.replace(hour=18, minute=index, second=0)
            rows.append(
                {
                    "content_hash": content_hash,
                    "status": "scheduled",
                    "trial_reel": 0,
                    "scheduled_at": scheduled.isoformat(),
                    "clip_dir": str(self.root),
                    "media_path": str(self.root / f"{content_hash}.mp4"),
                    "manifest_path": str(self.root / f"{content_hash}.json"),
                    "source_video": content_hash,
                }
            )
            Path(rows[-1]["media_path"]).write_bytes(b"video")
            Path(rows[-1]["manifest_path"]).write_text(
                json.dumps({"notes_score": 10 - index}),
                encoding="utf-8",
            )
        shortlist, _ = selector.scheduled_shortlist(
            list(reversed(rows)),
            as_of=self.as_of,
            ordinal=ordinal,
            lead_hours=36,
            target_slot=target_slot,
            weekly_counts={},
            trial_times=[],
            tested_reels=set(),
            tested_families=set(),
            facebook_statuses={},
        )
        expected_pool = rows[:3]
        expected = min(
            expected_pool,
            key=lambda row: selector.stable_selection_key(
                ordinal,
                str(row["content_hash"]),
            ),
        )
        self.assertEqual(shortlist[0]["content_hash"], expected["content_hash"])
        self.assertNotIn("fourth", [item["content_hash"] for item in shortlist])
        self.assertTrue(
            all(not item["notes_score_used_for_selection"] for item in shortlist)
        )

    def test_weekly_cap_and_48_hour_gap_fail_closed(self) -> None:
        scheduled = self.as_of + timedelta(days=4)
        scheduled = scheduled.replace(hour=18, minute=0, second=0)
        content_hash = "capacity-row"
        clip_dir, media, manifest = self.make_assets(content_hash, manifest=True)
        row = {
            "content_hash": content_hash,
            "status": "scheduled",
            "trial_reel": 0,
            "scheduled_at": scheduled.isoformat(),
            "clip_dir": str(clip_dir),
            "media_path": str(media),
            "manifest_path": str(manifest),
            "source_video": "source-capacity",
        }
        reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            lead_hours=36,
            target_slot="18",
            weekly_counts={selector.week_key(scheduled): 2},
            trial_times=[],
            tested_reels=set(),
            tested_families=set(),
            facebook_statuses={},
        )
        self.assertIn("WEEKLY_CAP_REACHED", reasons)

        reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            lead_hours=36,
            target_slot="18",
            weekly_counts={},
            trial_times=[scheduled - timedelta(hours=47, minutes=59)],
            tested_reels=set(),
            tested_families=set(),
            facebook_statuses={},
        )
        self.assertIn("TRIAL_SPACING", reasons)

        reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            lead_hours=36,
            target_slot="18",
            weekly_counts={},
            trial_times=[scheduled - timedelta(hours=48)],
            tested_reels=set(),
            tested_families=set(),
            facebook_statuses={},
        )
        self.assertNotIn("TRIAL_SPACING", reasons)

    def test_future_candidate_allowed_after_active_observation_windows_end(self) -> None:
        scheduled = (self.as_of + timedelta(days=4)).replace(
            hour=18,
            minute=0,
            second=0,
        )
        content_hash = "future-capacity-row"
        clip_dir, media, manifest = self.make_assets(content_hash, manifest=True)
        row = {
            "content_hash": content_hash,
            "status": "scheduled",
            "trial_reel": 0,
            "scheduled_at": scheduled.isoformat(),
            "clip_dir": str(clip_dir),
            "media_path": str(media),
            "manifest_path": str(manifest),
            "source_video": "source-future-capacity",
        }
        experiments = [
            {
                "state": "active",
                "published_at": (self.as_of - timedelta(hours=2)).isoformat(),
            },
            {
                "state": "scheduled",
                "scheduled_at": (self.as_of - timedelta(hours=1)).isoformat(),
            },
        ]
        window_starts = selector.nonterminal_observation_window_starts(experiments)

        self.assertEqual(
            selector.concurrent_observation_windows_at(self.as_of, window_starts),
            2,
        )
        self.assertEqual(
            selector.concurrent_observation_windows_at(scheduled, window_starts),
            0,
        )
        reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            lead_hours=36,
            target_slot="18",
            weekly_counts={},
            trial_times=[],
            tested_reels=set(),
            tested_families=set(),
            facebook_statuses={},
            observation_window_starts=window_starts,
        )
        self.assertNotIn("OBSERVATION_WINDOW_CAP_REACHED", reasons)

    def test_third_overlapping_observation_window_is_rejected(self) -> None:
        scheduled = (self.as_of + timedelta(days=4)).replace(
            hour=18,
            minute=0,
            second=0,
        )
        content_hash = "overlapping-capacity-row"
        clip_dir, media, manifest = self.make_assets(content_hash, manifest=True)
        row = {
            "content_hash": content_hash,
            "status": "scheduled",
            "trial_reel": 0,
            "scheduled_at": scheduled.isoformat(),
            "clip_dir": str(clip_dir),
            "media_path": str(media),
            "manifest_path": str(manifest),
            "source_video": "source-overlapping-capacity",
        }
        experiments = [
            {
                "state": "active",
                "published_at": (scheduled - timedelta(hours=24)).isoformat(),
            },
            {
                "state": "publishing",
                "scheduled_at": (scheduled - timedelta(hours=36)).isoformat(),
            },
        ]
        window_starts = selector.nonterminal_observation_window_starts(experiments)

        self.assertEqual(
            selector.concurrent_observation_windows_at(scheduled, window_starts),
            2,
        )
        reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            lead_hours=36,
            target_slot="18",
            weekly_counts={},
            trial_times=[],
            tested_reels=set(),
            tested_families=set(),
            facebook_statuses={},
            observation_window_starts=window_starts,
        )
        self.assertIn("OBSERVATION_WINDOW_CAP_REACHED", reasons)

    def test_integration_writes_review_packet_without_mutating_database(self) -> None:
        # No formal experiment means position A09.
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        self.insert_published(
            content_hash="parent-a",
            title="Strong utility parent",
            published_at=published,
            snapshot_age=80,
            views=202,
            reach=154,
            interactions=9,
            saved=4,
            shares=2,
        )
        for index, content_hash in enumerate(("slot-a", "slot-b", "slot-c")):
            scheduled = self.as_of + timedelta(days=index + 4)
            scheduled = scheduled.replace(hour=9, minute=index, second=0)
            self.insert_scheduled(
                content_hash=content_hash,
                scheduled_at=scheduled,
            )
        json_out = self.root / "selection.json"
        markdown_out = self.root / "selection.md"
        before = self.db.read_bytes()
        before_digest = hashlib.sha256(before).hexdigest()

        result = selector.main(
            [
                "--channel",
                CHANNEL,
                "--db",
                str(self.db),
                "--facebook-db",
                str(self.facebook_db),
                "--report",
                str(self.report),
                "--as-of",
                self.as_of.isoformat(),
                "--json-out",
                str(json_out),
                "--markdown-out",
                str(markdown_out),
            ]
        )

        self.assertEqual(result, 0)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before_digest)
        payload = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["recommendation"]["status"], "READY")
        self.assertEqual(
            payload["recommendation"]["lane"],
            "successful_post_variant",
        )
        self.assertEqual(
            payload["capacity"]["maximum_concurrent_observation_windows"],
            2,
        )
        self.assertEqual(
            payload["capacity"]["projected_observation_windows_at_launch"],
            1,
        )
        argv = payload["recommendation"]["dry_run_argv"]
        self.assertNotIn("--apply", argv)
        self.assertIn("--expected-scheduled-at", argv)
        self.assertIn("<rerendered-variant.mp4>", argv)
        self.assertIn("Strong utility parent", markdown_out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
