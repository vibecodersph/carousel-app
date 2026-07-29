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


def raw_payload(metrics: dict[str, int | float]) -> str:
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

    def make_assets(
        self,
        name: str,
        *,
        manifest: bool = False,
    ) -> tuple[Path, Path, Path]:
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
        skip_rate: float | None = None,
        average_watch_ms: int | None = None,
        source_family: str | None = None,
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
        if skip_rate is not None:
            metrics["reels_skip_rate"] = skip_rate
        if average_watch_ms is not None:
            metrics["ig_reels_avg_watch_time"] = average_watch_ms
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=content_hash,
                channel_id=CHANNEL,
                lang="ja",
                clip_dir=clip_dir,
                media_path=media,
                source_video=source_family or f"source-{content_hash}",
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

    def insert_existing_trial(
        self,
        *,
        content_hash: str,
        scheduled_at: datetime,
        experiment_id: str,
        case_type: str,
        source_family: str | None = None,
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
                source_video=source_family or f"trial-source-{content_hash}",
                title=content_hash,
                status=reel_ledger.STATUS_SCHEDULED,
                scheduled_at=scheduled_at.isoformat(),
                manifest_path=str(manifest),
            )
            reel_ledger.set_status(
                connection,
                content_hash,
                CHANNEL,
                reel_ledger.STATUS_SCHEDULED,
                scheduled_at=scheduled_at.isoformat(),
                trial_reel=1,
            )
            reel_ledger.upsert_trial_experiment(
                connection,
                experiment_id=experiment_id,
                content_hash=content_hash,
                channel_id=CHANNEL,
                case_type=case_type,
                parent_content_hash=(
                    "parent-existing"
                    if case_type == selector.LANE_SUCCESSFUL_POST_VARIANT
                    else None
                ),
                parent_media_id=None,
                asset_family_id=(
                    f"{source_family}/clip"
                    if source_family
                    else f"trial-source-{content_hash}/clip"
                ),
                baseline_hook="baseline",
                variant_hook="variant",
                changed_variables_json="[]",
                state="scheduled",
                scheduled_at=scheduled_at.isoformat(),
            )

    def insert_scheduled(
        self,
        *,
        content_hash: str,
        scheduled_at: datetime,
        title: str | None = None,
        source_family: str | None = None,
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
                source_video=source_family or f"source-{content_hash}",
                title=title or content_hash,
                status=reel_ledger.STATUS_SCHEDULED,
                scheduled_at=scheduled_at.isoformat(),
                manifest_path=str(manifest),
            )

    def scheduled_row(
        self,
        content_hash: str,
        scheduled_at: datetime,
        *,
        source_family: str | None = None,
    ) -> dict[str, object]:
        clip_dir, media, manifest = self.make_assets(content_hash, manifest=True)
        return {
            "content_hash": content_hash,
            "status": "scheduled",
            "trial_reel": 0,
            "scheduled_at": scheduled_at.isoformat(),
            "clip_dir": str(clip_dir),
            "media_path": str(media),
            "manifest_path": str(manifest),
            "source_video": source_family or f"source-{content_hash}",
        }

    def test_formal_ordinals_reserve_two_ids_and_support_the_1900_lane(self) -> None:
        experiments = [
            {"experiment_id": "PILOT-000"},
            {"experiment_id": "TRIAL-V1-0003-B21-deadbeef"},
        ]
        ordinal = selector.next_cycle_ordinal(experiments)
        self.assertEqual(ordinal, 4)
        parent_id = selector.formal_experiment_id(
            ordinal=ordinal + 1,
            lane=selector.LANE_SUCCESSFUL_POST_VARIANT,
            slot="19",
            content_hash="parent",
        )
        self.assertRegex(parent_id, selector.FORMAL_ID)
        self.assertIn("-A19-", parent_id)

    def test_conversion_only_selector_uses_stable_daily_lottery_with_parent_present(
        self,
    ) -> None:
        target_date = self.as_of.date() + timedelta(days=1)
        self.insert_existing_trial(
            content_hash="parent-trial",
            scheduled_at=datetime.combine(
                target_date,
                datetime.min.time().replace(hour=19),
                tzinfo=selector.JST,
            ),
            experiment_id="TRIAL-V1-0001-A19-aaaabbbb",
            case_type=selector.LANE_SUCCESSFUL_POST_VARIANT,
            source_family="parent-family",
        )
        for hour in (9, 13, 18, 21):
            self.insert_scheduled(
                content_hash=f"regular-{hour}",
                scheduled_at=datetime.combine(
                    target_date,
                    datetime.min.time().replace(hour=hour),
                    tzinfo=selector.JST,
                ),
                source_family=f"family-{hour}",
            )

        first = selector.build_scheduled_conversion_selection(
            db_path=self.db,
            facebook_db=self.facebook_db,
            channel_id=CHANNEL,
            as_of=self.as_of,
        )
        second = selector.build_scheduled_conversion_selection(
            db_path=self.db,
            facebook_db=self.facebook_db,
            channel_id=CHANNEL,
            as_of=self.as_of,
        )

        self.assertEqual(first["status"], "READY")
        self.assertEqual(first["target_date"], target_date.isoformat())
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["experiment_id"], second["experiment_id"])
        self.assertIn(first["selected"]["canonical_slot"], selector.REGULAR_SLOTS)
        self.assertIn("-B", first["experiment_id"])

    def test_conversion_only_selector_fails_closed_on_unregistered_trial(self) -> None:
        target = self.as_of + timedelta(days=1)
        self.insert_scheduled(
            content_hash="regular",
            scheduled_at=target.replace(hour=13, minute=0),
            source_family="regular-family",
        )
        clip_dir, media, manifest = self.make_assets("orphan", manifest=True)
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash="orphan",
                channel_id=CHANNEL,
                lang="ja",
                clip_dir=clip_dir,
                media_path=media,
                source_video="orphan-family",
                title="orphan",
                status=reel_ledger.STATUS_SCHEDULED,
                scheduled_at=target.replace(hour=19, minute=0).isoformat(),
                manifest_path=str(manifest),
            )
            reel_ledger.set_status(
                connection,
                "orphan",
                CHANNEL,
                reel_ledger.STATUS_SCHEDULED,
                trial_reel=1,
            )

        selection = selector.build_scheduled_conversion_selection(
            db_path=self.db,
            facebook_db=self.facebook_db,
            channel_id=CHANNEL,
            as_of=self.as_of,
        )

        self.assertEqual(selection["status"], "HOLD")
        self.assertEqual(selection["excluded"]["UNREGISTERED_TRIAL_ON_DATE"], 1)

    def test_parent_evidence_tiers_never_rank_fallbacks_above_strict_winners(
        self,
    ) -> None:
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        self.insert_published(
            content_hash="tier-a",
            title="Tier A",
            published_at=published,
            snapshot_age=80,
            views=202,
            reach=154,
            interactions=9,
            saved=4,
            shares=2,
        )
        self.insert_published(
            content_hash="tier-b",
            title="Tier B",
            published_at=published,
            snapshot_age=24.5,
            views=300,
            reach=200,
            interactions=1,
            saved=0,
            shares=0,
            skip_rate=49.0,
            average_watch_ms=9500,
        )
        self.insert_published(
            content_hash="tier-c",
            title="Tier C",
            published_at=published,
            snapshot_age=100,
            views=400,
            reach=300,
            interactions=20,
            saved=10,
            shares=5,
        )
        self.insert_published(
            content_hash="tier-d",
            title="Tier D",
            published_at=published,
            snapshot_age=24.5,
            views=130,
            reach=110,
            interactions=2,
            saved=1,
            shares=1,
        )
        self.insert_published(
            content_hash="below-threshold",
            title="Below threshold",
            published_at=published,
            snapshot_age=24.5,
            views=130,
            reach=110,
            interactions=1,
            saved=1,
            shares=0,
            skip_rate=70.0,
            average_watch_ms=4000,
        )
        rows, experiments = selector.load_ledger_state(self.db, channel_id=CHANNEL)
        candidates, exclusions = selector.published_parent_candidates(
            report_path=self.report,
            db_path=self.db,
            ledger_rows=rows,
            channel_id=CHANNEL,
            as_of=self.as_of,
            tested_parents=set(),
            tested_reels=set(),
        )
        self.assertEqual(
            [item["content_hash"] for item in candidates],
            ["tier-a", "tier-b", "tier-c", "tier-d"],
        )
        self.assertEqual(
            [item["evidence_tier"] for item in candidates],
            ["A", "B", "C", "D"],
        )
        self.assertEqual(exclusions["NO_ELIGIBLE_EVIDENCE_TIER"], 1)
        self.assertEqual(experiments, [])

    def test_mature_no_winner_can_use_core_valid_tier_d_diagnostic(
        self,
    ) -> None:
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        metrics = {
            "views": 120,
            "total_views": 125,
            "reach": 100,
            "likes": 1,
            "comments": 0,
            "saved": 1,
            "shares": 1,
            "total_interactions": 3,
            "reels_skip_rate": 44.0,
            "ig_reels_avg_watch_time": 9100,
        }
        captured = published + timedelta(hours=80)
        evidence = selector.parent_evidence(
            result={
                "classification": "NO_WINNER",
                "snapshot_age_hours": 80,
                "snapshot_captured_at": captured.isoformat(),
                "metrics": {},
            },
            reel={
                "snapshots": [
                    {
                        "id": 1,
                        "captured_at": captured.isoformat(),
                        "raw_api_payload": raw_payload(metrics),
                    }
                ]
            },
            published_at=published,
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence["tier"], "D")
        self.assertEqual(
            evidence["label"],
            "CORE_VALID_72H_DIAGNOSTIC",
        )
        self.assertEqual(evidence["classification"], "DIAGNOSTIC_ONLY")
        self.assertEqual(evidence["snapshot_age_hours"], 80.0)
        self.assertIn("saves+shares>=2", evidence["reason"])

    def test_mature_no_winner_below_diagnostic_threshold_stays_ineligible(
        self,
    ) -> None:
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        metrics = {
            "views": 120,
            "total_views": 125,
            "reach": 100,
            "likes": 1,
            "comments": 0,
            "saved": 1,
            "shares": 0,
            "total_interactions": 2,
            "reels_skip_rate": 70.0,
            "ig_reels_avg_watch_time": 4000,
        }
        captured = published + timedelta(hours=80)
        evidence = selector.parent_evidence(
            result={
                "classification": "NO_WINNER",
                "snapshot_age_hours": 80,
                "snapshot_captured_at": captured.isoformat(),
                "metrics": {},
            },
            reel={
                "snapshots": [
                    {
                        "id": 1,
                        "captured_at": captured.isoformat(),
                        "raw_api_payload": raw_payload(metrics),
                    }
                ]
            },
            published_at=published,
        )
        self.assertIsNone(evidence)

    def test_parent_fallback_selects_tier_b_when_tier_a_is_already_tested(
        self,
    ) -> None:
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        self.insert_published(
            content_hash="used-tier-a",
            title="Used Tier A",
            published_at=published,
            snapshot_age=80,
            views=202,
            reach=154,
            interactions=9,
            saved=4,
            shares=2,
        )
        self.insert_published(
            content_hash="fallback-tier-b",
            title="Fallback Tier B",
            published_at=published,
            snapshot_age=24.5,
            views=300,
            reach=200,
            interactions=1,
            saved=0,
            shares=0,
            skip_rate=49.0,
            average_watch_ms=9500,
        )
        rows, _ = selector.load_ledger_state(self.db, channel_id=CHANNEL)
        candidates, exclusions = selector.published_parent_candidates(
            report_path=self.report,
            db_path=self.db,
            ledger_rows=rows,
            channel_id=CHANNEL,
            as_of=self.as_of,
            tested_parents={"used-tier-a"},
            tested_reels=set(),
        )
        self.assertEqual(candidates[0]["content_hash"], "fallback-tier-b")
        self.assertEqual(candidates[0]["evidence_tier"], "B")
        self.assertEqual(exclusions["PARENT_ALREADY_TESTED"], 1)

    def test_parent_candidate_respects_permanent_exact_reel_hash(self) -> None:
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        self.insert_published(
            content_hash="used-as-exact-reel",
            title="Already used as an exact Trial Reel",
            published_at=published,
            snapshot_age=80,
            views=202,
            reach=154,
            interactions=9,
            saved=4,
            shares=2,
        )
        rows, _ = selector.load_ledger_state(self.db, channel_id=CHANNEL)
        candidates, exclusions = selector.published_parent_candidates(
            report_path=self.report,
            db_path=self.db,
            ledger_rows=rows,
            channel_id=CHANNEL,
            as_of=self.as_of,
            tested_parents=set(),
            tested_reels={"used-as-exact-reel"},
        )
        self.assertEqual(candidates, [])
        self.assertEqual(exclusions["REEL_ALREADY_TESTED"], 1)

    def test_earliest_daily_pool_uses_stable_hash_not_input_or_notes_order(self) -> None:
        target_date = (self.as_of + timedelta(days=4)).date()
        rows: list[dict[str, object]] = []
        for index, (content_hash, hour) in enumerate(
            zip(("first", "second", "third", "fourth"), (9, 13, 18, 21))
        ):
            scheduled = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                hour,
                index,
                tzinfo=selector.JST,
            )
            row = self.scheduled_row(content_hash, scheduled)
            Path(str(row["manifest_path"])).write_text(
                json.dumps({"notes_score": 10 - index}),
                encoding="utf-8",
            )
            rows.append(row)
        later = self.scheduled_row(
            "later",
            datetime.combine(
                target_date + timedelta(days=1),
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=9),
        )
        shortlist, _ = selector.scheduled_shortlist(
            [later, *reversed(rows)],
            as_of=self.as_of,
            ordinal=7,
            tested_reels=set(),
            family_observation_starts={},
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
        )
        seed = f"{target_date.isoformat()}|7"
        expected = min(
            rows,
            key=lambda row: selector.stable_selection_key(
                seed,
                str(row["content_hash"]),
            ),
        )
        self.assertEqual(shortlist[0]["content_hash"], expected["content_hash"])
        self.assertEqual(len(shortlist), 4)
        self.assertNotIn("later", [item["content_hash"] for item in shortlist])
        self.assertTrue(
            all(not item["notes_score_used_for_selection"] for item in shortlist)
        )

    def test_daily_lane_uniqueness_skips_a_partially_filled_date(self) -> None:
        first_date = (self.as_of + timedelta(days=4)).date()
        second_date = first_date + timedelta(days=1)
        rows = [
            self.scheduled_row(
                "first-date",
                datetime.combine(
                    first_date,
                    datetime.min.time(),
                    tzinfo=selector.JST,
                ).replace(hour=9),
            ),
            self.scheduled_row(
                "second-date",
                datetime.combine(
                    second_date,
                    datetime.min.time(),
                    tzinfo=selector.JST,
                ).replace(hour=13),
            ),
        ]
        shortlist, exclusions = selector.scheduled_shortlist(
            rows,
            as_of=self.as_of,
            ordinal=1,
            tested_reels=set(),
            family_observation_starts={},
            facebook_statuses={},
            lane_dates={
                selector.LANE_SCHEDULED_CONVERSION: {first_date.isoformat()},
                selector.LANE_SUCCESSFUL_POST_VARIANT: set(),
            },
            queue_launch_times=set(),
        )
        self.assertEqual(shortlist[0]["content_hash"], "second-date")
        self.assertEqual(exclusions["SCHEDULED_CONVERSION_ALREADY_ON_DATE"], 1)

    def test_parent_only_gap_does_not_block_later_scheduled_conversion(
        self,
    ) -> None:
        parent_gap_date = (self.as_of + timedelta(days=4)).date()
        later_date = parent_gap_date + timedelta(days=1)
        self.insert_existing_trial(
            content_hash="a" * 64,
            scheduled_at=datetime.combine(
                parent_gap_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=9),
            experiment_id="TRIAL-V1-0003-B09-aaaaaaaa",
            case_type=selector.LANE_SCHEDULED_CONVERSION,
        )
        self.insert_scheduled(
            content_hash="b" * 64,
            scheduled_at=datetime.combine(
                later_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=13),
        )

        selection = selector.build_selection(
            db_path=self.db,
            report_path=self.report,
            facebook_db=self.facebook_db,
            channel_id=CHANNEL,
            as_of=self.as_of,
        )

        self.assertEqual(selection["recommendation"]["status"], "READY")
        self.assertEqual(
            selection["daily_batch"]["target_date"],
            later_date.isoformat(),
        )
        self.assertEqual(
            selection["daily_batch"]["recommended_lanes"],
            [selector.LANE_SCHEDULED_CONVERSION],
        )
        self.assertEqual(
            selection["daily_batch"]["blocked_lanes"],
            [selector.LANE_SUCCESSFUL_POST_VARIANT],
        )
        lanes = selection["recommendation"]["lanes"]
        self.assertEqual(
            lanes[selector.LANE_SCHEDULED_CONVERSION]["status"],
            "READY",
        )
        self.assertEqual(
            lanes[selector.LANE_SUCCESSFUL_POST_VARIANT]["status"],
            "HOLD",
        )
        self.assertIn(
            "NO_ELIGIBLE_PUBLISHED_PARENT",
            lanes[selector.LANE_SUCCESSFUL_POST_VARIANT]["hold_reasons"],
        )
        first_considered = selection["dates_considered"][0]
        self.assertEqual(first_considered["date"], parent_gap_date.isoformat())
        self.assertEqual(
            first_considered["lanes"][
                selector.LANE_SUCCESSFUL_POST_VARIANT
            ]["status"],
            "HOLD",
        )

    def test_global_hold_reports_the_exact_earliest_blocked_lane_and_date(
        self,
    ) -> None:
        blocked_date = (self.as_of + timedelta(days=4)).date()
        self.insert_existing_trial(
            content_hash="c" * 64,
            scheduled_at=datetime.combine(
                blocked_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=9),
            experiment_id="TRIAL-V1-0003-B09-cccccccc",
            case_type=selector.LANE_SCHEDULED_CONVERSION,
        )

        selection = selector.build_selection(
            db_path=self.db,
            report_path=self.report,
            facebook_db=self.facebook_db,
            channel_id=CHANNEL,
            as_of=self.as_of,
        )

        recommendation = selection["recommendation"]
        self.assertEqual(recommendation["status"], "HOLD")
        self.assertEqual(
            recommendation["earliest_blocked"],
            {
                "date": blocked_date.isoformat(),
                "lanes": [
                    {
                        "lane": selector.LANE_SUCCESSFUL_POST_VARIANT,
                        "hold_reasons": [
                            "NO_ELIGIBLE_PUBLISHED_PARENT"
                        ],
                    }
                ],
            },
        )
        parent_lane = recommendation["lanes"][
            selector.LANE_SUCCESSFUL_POST_VARIANT
        ]
        self.assertEqual(parent_lane["blocked_date"], blocked_date.isoformat())
        self.assertEqual(
            recommendation["lanes"][
                selector.LANE_SCHEDULED_CONVERSION
            ]["blocked_date"],
            None,
        )

    def test_source_family_cooldown_is_half_open_but_hash_is_permanent(
        self,
    ) -> None:
        launch = (self.as_of + timedelta(days=7)).replace(
            hour=9,
            minute=0,
            second=0,
        )
        row = self.scheduled_row(
            "same-content",
            launch,
            source_family="recurring-source",
        )
        boundary_reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            tested_reels=set(),
            family_observation_starts={
                "recurring-source": [
                    launch - timedelta(hours=selector.OBSERVATION_WINDOW_HOURS)
                ]
            },
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            published_lane_missing=False,
        )
        self.assertNotIn(
            "ASSET_FAMILY_OBSERVATION_COOLDOWN",
            boundary_reasons,
        )

        overlapping_reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            tested_reels=set(),
            family_observation_starts={
                "recurring-source": [
                    launch
                    - timedelta(hours=selector.OBSERVATION_WINDOW_HOURS)
                    + timedelta(seconds=1)
                ]
            },
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            published_lane_missing=False,
        )
        self.assertIn(
            "ASSET_FAMILY_OBSERVATION_COOLDOWN",
            overlapping_reasons,
        )

        future_overlap_reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            tested_reels=set(),
            family_observation_starts={
                "recurring-source": [
                    launch + timedelta(hours=71, minutes=59)
                ]
            },
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            published_lane_missing=False,
        )
        self.assertIn(
            "ASSET_FAMILY_OBSERVATION_COOLDOWN",
            future_overlap_reasons,
        )
        future_boundary_reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            tested_reels=set(),
            family_observation_starts={
                "recurring-source": [launch + timedelta(hours=72)]
            },
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            published_lane_missing=False,
        )
        self.assertNotIn(
            "ASSET_FAMILY_OBSERVATION_COOLDOWN",
            future_boundary_reasons,
        )

        permanent_hash_reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            tested_reels={"same-content"},
            family_observation_starts={
                "recurring-source": [
                    launch
                    - timedelta(hours=selector.OBSERVATION_WINDOW_HOURS)
                    - timedelta(days=1)
                ]
            },
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            published_lane_missing=False,
        )
        self.assertIn("EXPERIMENT_ALREADY_LINKED", permanent_hash_reasons)
        self.assertNotIn(
            "ASSET_FAMILY_OBSERVATION_COOLDOWN",
            permanent_hash_reasons,
        )

    def test_parent_lane_uses_same_cross_lane_family_cooldown(self) -> None:
        start = self.as_of + timedelta(days=1)
        candidates = [
            {
                "content_hash": "parent",
                "asset_family_id": "recurring-source",
            }
        ]
        blocked, blocked_count = selector.parent_candidates_at_launch(
            candidates,
            launch=start + timedelta(hours=71, minutes=59),
            family_observation_starts={"recurring-source": [start]},
        )
        self.assertEqual(blocked, [])
        self.assertEqual(blocked_count, 1)

        available, blocked_count = selector.parent_candidates_at_launch(
            candidates,
            launch=start + timedelta(hours=72),
            family_observation_starts={"recurring-source": [start]},
        )
        self.assertEqual(
            [item["content_hash"] for item in available],
            ["parent"],
        )
        self.assertEqual(blocked_count, 0)

    def test_scheduled_selection_reserves_future_parent_family_if_possible(
        self,
    ) -> None:
        target_date = (self.as_of + timedelta(days=4)).date()
        reserved = self.scheduled_row(
            "reserved",
            datetime.combine(
                target_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=9),
            source_family="gYfCm3zYajg",
        )
        alternative = self.scheduled_row(
            "alternative",
            datetime.combine(
                target_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=13),
            source_family="ordinary-source",
        )
        shortlist, exclusions = selector.scheduled_shortlist(
            [reserved, alternative],
            as_of=self.as_of,
            ordinal=1,
            tested_reels=set(),
            family_observation_starts={},
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            reserved_parent_families={"gYfCm3zYajg"},
            target_date=target_date,
            published_lane_missing=False,
        )
        self.assertEqual(
            [item["content_hash"] for item in shortlist],
            ["alternative"],
        )
        self.assertEqual(exclusions["FUTURE_PARENT_FAMILY_RESERVED"], 1)

        fallback, _ = selector.scheduled_shortlist(
            [reserved],
            as_of=self.as_of,
            ordinal=1,
            tested_reels=set(),
            family_observation_starts={},
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            reserved_parent_families={"gYfCm3zYajg"},
            target_date=target_date,
            published_lane_missing=False,
        )
        self.assertEqual(fallback[0]["content_hash"], "reserved")
        self.assertTrue(fallback[0]["future_parent_family_reserved"])

    def test_scheduled_selection_preserves_only_next_date_family_option(
        self,
    ) -> None:
        target_date = (self.as_of + timedelta(days=4)).date()
        next_date = target_date + timedelta(days=1)
        sole_next_family = "sole-next-family"
        risky = self.scheduled_row(
            "risky-today",
            datetime.combine(
                target_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=9),
            source_family=sole_next_family,
        )
        safe = self.scheduled_row(
            "safe-today",
            datetime.combine(
                target_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=13),
            source_family="different-family",
        )
        tomorrow = self.scheduled_row(
            "only-tomorrow",
            datetime.combine(
                next_date,
                datetime.min.time(),
                tzinfo=selector.JST,
            ).replace(hour=9),
            source_family=sole_next_family,
        )

        shortlist, exclusions = selector.scheduled_shortlist(
            [risky, safe, tomorrow],
            as_of=self.as_of,
            ordinal=1,
            tested_reels=set(),
            family_observation_starts={},
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            target_date=target_date,
            published_lane_missing=False,
        )

        self.assertEqual(
            [item["content_hash"] for item in shortlist],
            ["safe-today"],
        )
        self.assertEqual(
            exclusions["NEXT_DATE_CONVERSION_HORIZON_RESERVED"],
            1,
        )
        self.assertEqual(
            shortlist[0]["next_conversion_date"],
            next_date.isoformat(),
        )
        self.assertEqual(
            shortlist[0]["next_conversion_options_preserved"],
            1,
        )

        fallback, fallback_exclusions = selector.scheduled_shortlist(
            [risky, tomorrow],
            as_of=self.as_of,
            ordinal=1,
            tested_reels=set(),
            family_observation_starts={},
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            target_date=target_date,
            published_lane_missing=False,
        )
        self.assertEqual(fallback[0]["content_hash"], "risky-today")
        self.assertEqual(
            fallback[0]["next_conversion_options_preserved"],
            0,
        )
        self.assertEqual(
            fallback_exclusions["NEXT_DATE_CONVERSION_HORIZON_RESERVED"],
            0,
        )

    def test_immature_rerender_ready_parent_family_is_reserved(self) -> None:
        published = self.as_of.astimezone(timezone.utc) - timedelta(hours=48)
        self.insert_published(
            content_hash="immature-parent",
            title="Immature but rerender-ready",
            published_at=published,
            snapshot_age=24.5,
            views=300,
            reach=200,
            interactions=5,
            saved=2,
            shares=1,
            source_family="future-parent-source",
        )
        rows, _ = selector.load_ledger_state(self.db, channel_id=CHANNEL)
        reserved = selector.future_parent_reserve_families(
            report_path=self.report,
            db_path=self.db,
            ledger_rows=rows,
            as_of=self.as_of,
            tested_parents=set(),
            tested_reels=set(),
            eligible_parent_candidates=[],
        )
        self.assertIn("future-parent-source", reserved)
        self.assertTrue(
            selector.PRIORITY_PARENT_RESERVE_FAMILIES.issubset(reserved)
        )

    def test_existing_published_lane_fills_only_conversion_with_next_ordinal(
        self,
    ) -> None:
        existing_at = self.as_of.replace(hour=11, minute=45, second=0)
        self.insert_existing_trial(
            content_hash="a" * 64,
            scheduled_at=existing_at,
            experiment_id="TRIAL-V1-0003-A19-aaaaaaaa",
            case_type=selector.LANE_SUCCESSFUL_POST_VARIANT,
        )
        conversion_at = self.as_of.replace(hour=20, minute=54, second=0)
        self.insert_scheduled(
            content_hash="b" * 64,
            scheduled_at=conversion_at,
            title="Tonight's regular Reel",
        )

        selection = selector.build_selection(
            db_path=self.db,
            report_path=self.report,
            facebook_db=self.facebook_db,
            channel_id=CHANNEL,
            as_of=self.as_of,
        )

        self.assertEqual(selection["recommendation"]["status"], "READY")
        self.assertEqual(
            selection["daily_batch"]["target_date"],
            self.as_of.date().isoformat(),
        )
        self.assertEqual(
            selection["daily_batch"]["existing_lanes"],
            [selector.LANE_SUCCESSFUL_POST_VARIANT],
        )
        self.assertEqual(
            selection["daily_batch"]["missing_lanes"],
            [selector.LANE_SCHEDULED_CONVERSION],
        )
        lanes = selection["recommendation"]["lanes"]
        self.assertEqual(
            lanes[selector.LANE_SUCCESSFUL_POST_VARIANT]["status"],
            "ALREADY_FILLED",
        )
        self.assertEqual(
            lanes[selector.LANE_SCHEDULED_CONVERSION]["experiment_id"],
            "TRIAL-V1-0004-B21-bbbbbbbb",
        )
        self.assertEqual(selection["recommendation"]["recommended_lane_count"], 1)
        self.assertFalse(selection["recommendation"]["manual_approval_required"])

    def test_existing_conversion_fills_only_1900_parent_with_next_ordinal(
        self,
    ) -> None:
        target_date = (self.as_of + timedelta(days=4)).date()
        conversion_at = datetime.combine(
            target_date,
            datetime.min.time(),
            tzinfo=selector.JST,
        ).replace(hour=9)
        self.insert_existing_trial(
            content_hash="b" * 64,
            scheduled_at=conversion_at,
            experiment_id="TRIAL-V1-0003-B09-bbbbbbbb",
            case_type=selector.LANE_SCHEDULED_CONVERSION,
        )
        published = self.as_of.astimezone(timezone.utc) - timedelta(days=10)
        self.insert_published(
            content_hash="parent-ready",
            title="Ready published parent",
            published_at=published,
            snapshot_age=80,
            views=202,
            reach=154,
            interactions=9,
            saved=4,
            shares=2,
        )

        selection = selector.build_selection(
            db_path=self.db,
            report_path=self.report,
            facebook_db=self.facebook_db,
            channel_id=CHANNEL,
            as_of=self.as_of,
        )

        self.assertEqual(selection["recommendation"]["status"], "READY")
        self.assertEqual(
            selection["daily_batch"]["existing_lanes"],
            [selector.LANE_SCHEDULED_CONVERSION],
        )
        self.assertEqual(
            selection["daily_batch"]["missing_lanes"],
            [selector.LANE_SUCCESSFUL_POST_VARIANT],
        )
        lanes = selection["recommendation"]["lanes"]
        self.assertEqual(
            lanes[selector.LANE_SCHEDULED_CONVERSION]["status"],
            "ALREADY_FILLED",
        )
        parent_lane = lanes[selector.LANE_SUCCESSFUL_POST_VARIANT]
        self.assertEqual(parent_lane["experiment_id"], "TRIAL-V1-0004-A19-40c3c209")
        self.assertEqual(
            datetime.fromisoformat(parent_lane["scheduled_at"]).hour,
            19,
        )
        self.assertEqual(selection["recommendation"]["recommended_lane_count"], 1)
        self.assertTrue(selection["recommendation"]["manual_approval_required"])

    def test_family_facebook_and_1900_collision_guards_are_preserved(self) -> None:
        scheduled = (self.as_of + timedelta(days=4)).replace(
            hour=18,
            minute=0,
            second=0,
        )
        row = self.scheduled_row("guarded", scheduled)
        family = selector.asset_family_id(row)
        reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            tested_reels=set(),
            family_observation_starts={
                family: [scheduled - timedelta(hours=1)]
            },
            facebook_statuses={"guarded": "published"},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times={
                selector.published_variant_launch(
                    scheduled.astimezone(selector.JST).date()
                )
            },
        )
        self.assertIn("ASSET_FAMILY_OBSERVATION_COOLDOWN", reasons)
        self.assertIn("FACEBOOK_ROW_IMMUTABLE", reasons)
        self.assertIn("PUBLISHED_1900_SLOT_OCCUPIED", reasons)

    def test_observation_cap_is_seven_for_two_variable_time_daily_lanes(self) -> None:
        target_date = (self.as_of + timedelta(days=8)).date()
        scheduled = datetime.combine(
            target_date,
            datetime.min.time(),
            tzinfo=selector.JST,
        ).replace(hour=9)
        published = selector.published_variant_launch(target_date)
        starts = [
            published - timedelta(days=3),
            published - timedelta(days=3) + timedelta(hours=2),
            scheduled - timedelta(days=2),
            published - timedelta(days=2),
            scheduled - timedelta(days=1),
            published - timedelta(days=1),
        ]
        projected = selector.projected_daily_batch_windows(
            scheduled_launch=scheduled,
            published_launch=published,
            observation_window_starts=starts,
        )
        self.assertEqual(selector.MAX_CONCURRENT_OBSERVATION_WINDOWS, 7)
        self.assertEqual(projected[selector.LANE_SCHEDULED_CONVERSION], 7)
        self.assertLessEqual(max(projected.values()), 7)

        row = self.scheduled_row("over-cap", scheduled)
        reasons, _ = selector.scheduled_exclusions(
            row,
            as_of=self.as_of,
            tested_reels=set(),
            family_observation_starts={},
            facebook_statuses={},
            lane_dates={lane: set() for lane in selector.DAILY_LANES},
            queue_launch_times=set(),
            observation_window_starts=[
                *starts,
                scheduled - timedelta(hours=36),
            ],
        )
        self.assertIn("OBSERVATION_WINDOW_CAP_REACHED", reasons)

    def test_integration_emits_both_read_only_lanes_without_mutating_database(
        self,
    ) -> None:
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
        target_date = (self.as_of + timedelta(days=4)).date()
        for content_hash, hour in zip(
            ("slot-a", "slot-b", "slot-c"),
            (9, 13, 18),
        ):
            self.insert_scheduled(
                content_hash=content_hash,
                scheduled_at=datetime.combine(
                    target_date,
                    datetime.min.time(),
                    tzinfo=selector.JST,
                ).replace(hour=hour),
            )
        json_out = self.root / "selection.json"
        markdown_out = self.root / "selection.md"
        before_digest = hashlib.sha256(self.db.read_bytes()).hexdigest()

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
        self.assertEqual(
            hashlib.sha256(self.db.read_bytes()).hexdigest(),
            before_digest,
        )
        payload = json.loads(json_out.read_text(encoding="utf-8"))
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["recommendation"]["status"], "READY")
        self.assertEqual(payload["daily_batch"]["target_date"], target_date.isoformat())
        self.assertEqual(payload["daily_batch"]["lanes_per_day"], 2)
        self.assertEqual(
            payload["capacity"]["maximum_concurrent_observation_windows"],
            7,
        )

        lanes = payload["recommendation"]["lanes"]
        scheduled_lane = lanes[selector.LANE_SCHEDULED_CONVERSION]
        parent_lane = lanes[selector.LANE_SUCCESSFUL_POST_VARIANT]
        self.assertEqual(scheduled_lane["status"], "READY")
        self.assertEqual(parent_lane["status"], "READY")
        self.assertFalse(scheduled_lane["manual_approval_required"])
        self.assertTrue(parent_lane["manual_approval_required"])

        scheduled_argv = scheduled_lane["dry_run_argv"]
        self.assertIn("trial-convert-scheduled", scheduled_argv)
        self.assertIn("--expected-scheduled-at", scheduled_argv)

        parent_argv = parent_lane["dry_run_argv"]
        self.assertIn("trial-add-from-published", parent_argv)
        self.assertNotIn("--replace-content-hash", parent_argv)
        self.assertIn("--scheduled-at", parent_argv)
        self.assertIn("--expected-scheduled-at", parent_argv)
        scheduled_value = parent_argv[parent_argv.index("--scheduled-at") + 1]
        expected_value = parent_argv[parent_argv.index("--expected-scheduled-at") + 1]
        self.assertEqual(scheduled_value, expected_value)
        self.assertEqual(
            datetime.fromisoformat(scheduled_value).astimezone(selector.JST).hour,
            19,
        )
        self.assertNotIn("--apply", json.dumps(payload))
        markdown = markdown_out.read_text(encoding="utf-8")
        self.assertIn("Strong utility parent", markdown)
        self.assertIn("19:00", markdown)


if __name__ == "__main__":
    unittest.main()
