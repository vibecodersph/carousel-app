from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

import reel_ledger
import reel_scheduler


CHANNEL_ID = "aibrief_jp"


class TrialWorkflowTests(unittest.TestCase):
    def make_reel(
        self,
        conn,
        *,
        root: Path,
        content_hash: str,
        status: str,
        title: str,
        scheduled_at: str | None = None,
        published_at: str | None = None,
        media_id: str | None = None,
        source_video: str = "source-a",
    ):
        clip_dir = root / source_video / "clips" / content_hash
        clip_dir.mkdir(parents=True, exist_ok=True)
        media_path = clip_dir / "reel.ja.aibrief_jp.mp4"
        media_path.write_bytes(f"video-{content_hash}".encode())
        manifest_path = root / "manifests" / content_hash / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "channel_id": CHANNEL_ID,
                    "topic": title,
                    "instagram_caption": f"{title}\n\nCaption body",
                    "slides": [
                        {
                            "index": 1,
                            "type": "video",
                            "path": str(media_path),
                        }
                    ],
                    **({"scheduled_at": scheduled_at} if scheduled_at else {}),
                }
            ),
            encoding="utf-8",
        )
        reel_ledger.upsert_imported(
            conn,
            content_hash=content_hash,
            channel_id=CHANNEL_ID,
            lang="ja",
            clip_dir=clip_dir,
            media_path=media_path,
            source_video=source_video,
            title=title,
            status=status,
            scheduled_at=scheduled_at,
            published_at=published_at,
            media_id=media_id,
            manifest_path=str(manifest_path),
        )
        conn.execute(
            "UPDATE reels SET caption=? WHERE content_hash=? AND channel_id=?",
            (f"{title}\n\nCaption body", content_hash, CHANNEL_ID),
        )
        return reel_ledger.get_reel(conn, content_hash, CHANNEL_ID), manifest_path

    def make_active_trial(
        self,
        *,
        db: Path,
        root: Path,
        experiment_id: str,
        content_hash: str,
        published_at: str,
        media_id: str,
    ) -> None:
        with reel_ledger.connect(db) as conn:
            self.make_reel(
                conn,
                root=root,
                content_hash=content_hash,
                status=reel_ledger.STATUS_SCHEDULED,
                title=f"Trial hook {content_hash}",
                scheduled_at=published_at,
            )
        reel_scheduler.convert_scheduled_reel_to_trial(
            db_path=db,
            channel_id=CHANNEL_ID,
            content_hash=content_hash,
            experiment_id=experiment_id,
            hook=None,
            asset_family_id=None,
            changed_variables=None,
            graduation_strategy="MANUAL",
            apply=True,
        )
        with reel_ledger.connect(db) as conn:
            reel_ledger.set_status(
                conn,
                content_hash,
                CHANNEL_ID,
                reel_ledger.STATUS_PUBLISHED,
                published_at=published_at,
                media_id=media_id,
            )

    def record_trial_checkpoint(
        self,
        *,
        db: Path,
        content_hash: str,
        media_id: str,
        captured_at: str,
        complete: bool = True,
    ) -> None:
        metrics = {
            "views": 100,
            "reach": 80,
            "likes": 7,
            "comments": 2,
            "saved": 4,
            "shares": 3,
            "total_interactions": 16,
        }
        if not complete:
            metrics.pop("comments")
        with reel_ledger.connect(db) as conn:
            reel_ledger.record_insight(
                conn,
                content_hash=content_hash,
                channel_id=CHANNEL_ID,
                media_id=media_id,
                metrics=metrics,
                captured_at=captured_at,
            )

    def test_published_variant_replaces_exact_slot_and_returns_displaced_to_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            scheduled_at = "2026-08-01T13:00:00+09:00"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Baseline hook",
                    published_at="2026-07-20T04:00:00+00:00",
                    media_id="178900001",
                )
                _, displaced_manifest = self.make_reel(
                    conn,
                    root=root,
                    content_hash="displaced",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Queued hook",
                    scheduled_at=scheduled_at,
                    source_video="source-b",
                )
            rerendered = root / "trial-rerender.mp4"
            rerendered.write_bytes(b"distinct-rerendered-video")
            result = reel_scheduler.queue_trial_from_published(
                db_path=db,
                channel_id=CHANNEL_ID,
                parent_content_hash="parent",
                replacement_content_hash="displaced",
                media_path=rerendered,
                experiment_id="TRIAL-001",
                variant_hook="Sharper variant hook",
                asset_family_id=None,
                changed_variables=None,
                graduation_strategy="MANUAL",
                out_dir=root / "trial-manifests",
                apply=True,
            )

            variant_hash = reel_ledger.hash_file(rerendered)
            self.assertEqual(result["content_hash"], variant_hash)
            with reel_ledger.connect(db) as conn:
                variant = reel_ledger.get_reel(conn, variant_hash, CHANNEL_ID)
                displaced = reel_ledger.get_reel(conn, "displaced", CHANNEL_ID)
                experiment = reel_ledger.get_trial_experiment(conn, "TRIAL-001")
                due = reel_ledger.due_reels(
                    conn,
                    now=datetime(2026, 7, 1, tzinfo=ZoneInfo("UTC")),
                    channel_id=CHANNEL_ID,
                    include_future=True,
                )
            self.assertEqual(variant["status"], reel_ledger.STATUS_SCHEDULED)
            self.assertEqual(variant["scheduled_at"], scheduled_at)
            self.assertEqual(variant["trial_reel"], 1)
            self.assertEqual(variant["trial_graduation_strategy"], "MANUAL")
            self.assertEqual(displaced["status"], reel_ledger.STATUS_NEW)
            self.assertIsNone(displaced["scheduled_at"])
            self.assertIsNone(displaced["manifest_path"])
            self.assertEqual(experiment["case_type"], "successful_post_variant")
            self.assertEqual(experiment["parent_content_hash"], "parent")
            self.assertEqual(experiment["displaced_content_hash"], "displaced")
            self.assertEqual(experiment["state"], reel_ledger.TRIAL_STATE_SCHEDULED)
            self.assertEqual([row["content_hash"] for row in due], [variant_hash])

            trial_manifest = reel_scheduler.read_json(Path(variant["manifest_path"]))
            self.assertTrue(trial_manifest["instagram_trial_reel"]["enabled"])
            self.assertEqual(
                trial_manifest["trial_experiment"]["experiment_id"],
                "TRIAL-001",
            )
            self.assertEqual(
                trial_manifest["reel_ledger"]["content_hash"],
                variant_hash,
            )
            self.assertEqual(
                trial_manifest["instagram_caption"],
                "Baseline hook\n\nCaption body",
            )
            self.assertEqual(
                json.loads(experiment["changed_variables_json"]),
                ["overlay_hook"],
            )
            self.assertEqual(
                Path(variant["manifest_path"]).with_name("caption.txt").read_text(
                    encoding="utf-8"
                ),
                "Baseline hook\n\nCaption body\n",
            )
            displaced_data = reel_scheduler.read_json(displaced_manifest)
            self.assertEqual(displaced_data["schedule_status"], reel_ledger.STATUS_NEW)
            self.assertNotIn("scheduled_at", displaced_data)

    def test_published_variant_is_dry_run_by_default_function_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Baseline",
                    media_id="178900002",
                )
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="displaced",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Queued",
                    scheduled_at="2026-08-02T09:00:00+09:00",
                )
            media = root / "variant.mp4"
            media.write_bytes(b"variant")
            result = reel_scheduler.queue_trial_from_published(
                db_path=db,
                channel_id=CHANNEL_ID,
                parent_content_hash="parent",
                replacement_content_hash="displaced",
                media_path=media,
                experiment_id="TRIAL-DRY",
                variant_hook="Variant",
                asset_family_id=None,
                changed_variables=None,
                graduation_strategy="MANUAL",
                out_dir=root / "out",
                apply=False,
                expected_scheduled_at="2026-08-02T09:00:00+09:00",
            )
            self.assertEqual(result["mode"], "dry-run")
            with reel_ledger.connect(db) as conn:
                self.assertIsNone(
                    reel_ledger.get_reel(
                        conn,
                        reel_ledger.hash_file(media),
                        CHANNEL_ID,
                    )
                )
                displaced = reel_ledger.get_reel(conn, "displaced", CHANNEL_ID)
                self.assertEqual(displaced["status"], reel_ledger.STATUS_SCHEDULED)

    def test_published_variant_rejects_queue_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Baseline",
                    media_id="178900010",
                )
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="displaced",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Queued",
                    scheduled_at="2026-08-02T09:00:00+09:00",
                )
            media = root / "variant.mp4"
            media.write_bytes(b"variant")
            with self.assertRaisesRegex(SystemExit, "timeslot changed"):
                reel_scheduler.queue_trial_from_published(
                    db_path=db,
                    channel_id=CHANNEL_ID,
                    parent_content_hash="parent",
                    replacement_content_hash="displaced",
                    media_path=media,
                    experiment_id="TRIAL-DRIFT-A",
                    variant_hook="Variant",
                    asset_family_id=None,
                    changed_variables=None,
                    graduation_strategy="MANUAL",
                    out_dir=root / "out",
                    apply=False,
                    expected_scheduled_at="2026-08-02T13:00:00+09:00",
                )

    def test_scheduled_conversion_keeps_slot_and_reflow_cannot_move_or_demote_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            trial_at = "2026-08-03T13:00:00+09:00"
            with reel_ledger.connect(db) as conn:
                _, manifest_path = self.make_reel(
                    conn,
                    root=root,
                    content_hash="convert-me",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Existing rendered hook",
                    scheduled_at=trial_at,
                )
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="regular",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Regular queued hook",
                    scheduled_at="2026-08-03T18:00:00+09:00",
                    source_video="source-b",
                )
            reel_scheduler.convert_scheduled_reel_to_trial(
                db_path=db,
                channel_id=CHANNEL_ID,
                content_hash="convert-me",
                experiment_id="TRIAL-002",
                hook=None,
                asset_family_id=None,
                changed_variables=None,
                graduation_strategy="MANUAL",
                apply=True,
            )
            reel_scheduler.reflow_queue_rows(
                db_path=db,
                channel_filter=CHANNEL_ID,
                start_at_text="2026-08-03T09:00:00+09:00",
                jitter_minutes=0,
                settings_key="instagram_reels",
                apply=True,
            )

            with reel_ledger.connect(db) as conn:
                converted = reel_ledger.get_reel(conn, "convert-me", CHANNEL_ID)
                experiment = reel_ledger.get_trial_experiment(conn, "TRIAL-002")
            self.assertEqual(converted["scheduled_at"], trial_at)
            self.assertEqual(converted["trial_reel"], 1)
            self.assertEqual(experiment["case_type"], "scheduled_conversion")
            self.assertEqual(experiment["variant_hook"], "Existing rendered hook")
            manifest = reel_scheduler.read_json(manifest_path)
            self.assertEqual(manifest["scheduled_at"], trial_at)
            self.assertEqual(
                manifest["instagram_trial_reel"]["graduation_strategy"],
                "MANUAL",
            )

    def test_scheduled_conversion_rejects_queue_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="convert-me",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Existing hook",
                    scheduled_at="2026-08-03T13:00:00+09:00",
                )
            with self.assertRaisesRegex(SystemExit, "timeslot changed"):
                reel_scheduler.convert_scheduled_reel_to_trial(
                    db_path=db,
                    channel_id=CHANNEL_ID,
                    content_hash="convert-me",
                    experiment_id="TRIAL-DRIFT-B",
                    hook=None,
                    asset_family_id=None,
                    changed_variables=None,
                    graduation_strategy="MANUAL",
                    apply=False,
                    expected_scheduled_at="2026-08-03T18:00:00+09:00",
                )

    def test_trial_experiment_state_follows_ledger_publish_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="lifecycle",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Lifecycle hook",
                    scheduled_at="2026-08-04T09:00:00+09:00",
                )
            reel_scheduler.convert_scheduled_reel_to_trial(
                db_path=db,
                channel_id=CHANNEL_ID,
                content_hash="lifecycle",
                experiment_id="TRIAL-003",
                hook=None,
                asset_family_id=None,
                changed_variables=None,
                graduation_strategy="MANUAL",
                apply=True,
            )
            with reel_ledger.connect(db) as conn:
                self.assertTrue(
                    reel_ledger.claim_for_publish(conn, "lifecycle", CHANNEL_ID)
                )
                self.assertEqual(
                    reel_ledger.get_trial_experiment(conn, "TRIAL-003")["state"],
                    reel_ledger.TRIAL_STATE_PUBLISHING,
                )
                reel_ledger.set_status(
                    conn,
                    "lifecycle",
                    CHANNEL_ID,
                    reel_ledger.STATUS_PUBLISHED,
                    published_at="2026-08-04T00:00:00+00:00",
                    media_id="178900003",
                )
                experiment = reel_ledger.get_trial_experiment(conn, "TRIAL-003")
            self.assertEqual(experiment["state"], reel_ledger.TRIAL_STATE_ACTIVE)
            self.assertEqual(
                experiment["published_at"],
                "2026-08-04T00:00:00+00:00",
            )

    def test_trial_decision_requires_72h_age_and_core_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            self.make_active_trial(
                db=db,
                root=root,
                experiment_id="TRIAL-DECIDE-GATE",
                content_hash="decision-gate",
                published_at="2026-08-01T00:00:00+00:00",
                media_id="178900020",
            )

            with self.assertRaisesRegex(SystemExit, "at least 72 hours"):
                reel_scheduler.decide_trial_experiment(
                    db_path=db,
                    experiment_id="TRIAL-DECIDE-GATE",
                    decision="graduate",
                    reason="Strong reach and saves.",
                    apply=False,
                    now=datetime.fromisoformat("2026-08-03T23:00:00+00:00"),
                )

            with self.assertRaisesRegex(SystemExit, r"core-valid \+72h"):
                reel_scheduler.decide_trial_experiment(
                    db_path=db,
                    experiment_id="TRIAL-DECIDE-GATE",
                    decision="graduate",
                    reason="Strong reach and saves.",
                    apply=False,
                    now=datetime.fromisoformat("2026-08-04T08:00:00+00:00"),
                )

            self.record_trial_checkpoint(
                db=db,
                content_hash="decision-gate",
                media_id="178900020",
                captured_at="2026-08-04T00:30:00+00:00",
                complete=False,
            )
            with self.assertRaisesRegex(SystemExit, r"core-valid \+72h"):
                reel_scheduler.decide_trial_experiment(
                    db_path=db,
                    experiment_id="TRIAL-DECIDE-GATE",
                    decision="graduate",
                    reason="Strong reach and saves.",
                    apply=False,
                    now=datetime.fromisoformat("2026-08-04T08:00:00+00:00"),
                )

    def test_trial_graduate_decision_is_dry_run_then_idempotent_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            self.make_active_trial(
                db=db,
                root=root,
                experiment_id="TRIAL-DECIDE-GRADUATE",
                content_hash="decision-graduate",
                published_at="2026-08-01T00:00:00+00:00",
                media_id="178900021",
            )
            self.record_trial_checkpoint(
                db=db,
                content_hash="decision-graduate",
                media_id="178900021",
                captured_at="2026-08-04T00:30:00+00:00",
            )
            decision_at = datetime.fromisoformat("2026-08-04T01:00:00+00:00")

            preview = reel_scheduler.decide_trial_experiment(
                db_path=db,
                experiment_id="TRIAL-DECIDE-GRADUATE",
                decision="graduate",
                reason="The mature Trial cleared the reach and save thresholds.",
                apply=False,
                now=decision_at,
            )
            self.assertEqual(preview["mode"], "dry-run")
            self.assertEqual(preview["checkpoint_72h"]["age_hours"], 72.5)
            with reel_ledger.connect(db) as conn:
                before = reel_ledger.get_trial_experiment(
                    conn,
                    "TRIAL-DECIDE-GRADUATE",
                )
            self.assertEqual(before["state"], reel_ledger.TRIAL_STATE_ACTIVE)
            self.assertIsNone(before["decision"])

            applied = reel_scheduler.decide_trial_experiment(
                db_path=db,
                experiment_id="TRIAL-DECIDE-GRADUATE",
                decision="graduate",
                reason="The mature Trial cleared the reach and save thresholds.",
                apply=True,
                now=decision_at,
            )
            self.assertEqual(applied["mode"], "apply")
            with reel_ledger.connect(db) as conn:
                experiment = reel_ledger.get_trial_experiment(
                    conn,
                    "TRIAL-DECIDE-GRADUATE",
                )
            self.assertEqual(experiment["state"], reel_ledger.TRIAL_STATE_GRADUATED)
            self.assertEqual(experiment["decision"], "graduate")
            self.assertEqual(
                experiment["decision_reason"],
                "The mature Trial cleared the reach and save thresholds.",
            )
            self.assertEqual(experiment["decision_at"], "2026-08-04T01:00:00+00:00")
            self.assertEqual(experiment["graduated_at"], experiment["decision_at"])
            self.assertIsNone(experiment["stopped_at"])

            repeated = reel_scheduler.decide_trial_experiment(
                db_path=db,
                experiment_id="TRIAL-DECIDE-GRADUATE",
                decision="graduate",
                reason="The mature Trial cleared the reach and save thresholds.",
                apply=True,
                now=datetime.fromisoformat("2026-08-04T02:00:00+00:00"),
            )
            self.assertEqual(repeated["mode"], "already-applied")
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(
                repeated["decision_at"],
                "2026-08-04T01:00:00+00:00",
            )

            with self.assertRaisesRegex(SystemExit, "refusing to overwrite"):
                reel_scheduler.decide_trial_experiment(
                    db_path=db,
                    experiment_id="TRIAL-DECIDE-GRADUATE",
                    decision="stop",
                    reason="Conflicting second decision.",
                    apply=True,
                    override_72h_checkpoint_and_age=True,
                    now=datetime.fromisoformat("2026-08-04T02:00:00+00:00"),
                )

    def test_trial_stop_decision_requires_reason_and_supports_explicit_72h_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            self.make_active_trial(
                db=db,
                root=root,
                experiment_id="TRIAL-DECIDE-STOP",
                content_hash="decision-stop",
                published_at="2026-08-10T00:00:00+00:00",
                media_id="178900022",
            )
            now = datetime.fromisoformat("2026-08-10T01:00:00+00:00")

            with self.assertRaisesRegex(SystemExit, "--reason"):
                reel_scheduler.decide_trial_experiment(
                    db_path=db,
                    experiment_id="TRIAL-DECIDE-STOP",
                    decision="stop",
                    reason="  ",
                    apply=False,
                    override_72h_checkpoint_and_age=True,
                    now=now,
                )

            result = reel_scheduler.decide_trial_experiment(
                db_path=db,
                experiment_id="TRIAL-DECIDE-STOP",
                decision="stop",
                reason="Safety review requires this Trial to close early.",
                apply=True,
                override_72h_checkpoint_and_age=True,
                now=now,
            )
            self.assertTrue(result["override_72h_checkpoint_and_age"])
            self.assertIsNone(result["checkpoint_72h"])
            with reel_ledger.connect(db) as conn:
                experiment = reel_ledger.get_trial_experiment(
                    conn,
                    "TRIAL-DECIDE-STOP",
                )
            self.assertEqual(experiment["state"], reel_ledger.TRIAL_STATE_STOPPED)
            self.assertEqual(experiment["decision"], "stop")
            self.assertEqual(experiment["stopped_at"], experiment["decision_at"])
            self.assertIsNone(experiment["graduated_at"])

    def test_register_existing_publish_backfills_active_trial_and_manifest_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                parent, _ = self.make_reel(
                    conn,
                    root=root,
                    content_hash="parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Original winner hook",
                    published_at="2026-07-20T00:00:00+00:00",
                    media_id="178900004",
                )
            media = root / "published-trial.mp4"
            media.write_bytes(b"already-live-trial")
            manifest_path = root / "published-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "channel_id": CHANNEL_ID,
                        "topic": "New Trial hook",
                        "instagram_caption": "New Trial hook\n\nBody",
                        "instagram_trial_reel": {
                            "enabled": True,
                            "graduation_strategy": "MANUAL",
                        },
                        "slides": [
                            {
                                "index": 1,
                                "type": "video",
                                "path": str(media),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            report_path = root / "instagram_publish.json"
            report_path.write_text(
                json.dumps(
                    {
                        "created_at": "2026-07-27T02:45:07+00:00",
                        "trial_reel": True,
                        "result": {
                            "published": {"id": "18544612528074609"},
                            "permalink": {
                                "permalink": "https://www.instagram.com/reel/example/"
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            reel_scheduler.register_published_trial(
                db_path=db,
                channel_id=CHANNEL_ID,
                manifest_path=manifest_path,
                report_path=report_path,
                experiment_id="PILOT-000",
                parent_content_hash="parent",
                baseline_hook=None,
                variant_hook=None,
                asset_family_id=None,
                changed_variables=None,
                published_at=None,
                apply=True,
            )

            trial_hash = reel_ledger.hash_file(media)
            with reel_ledger.connect(db) as conn:
                row = reel_ledger.get_reel(conn, trial_hash, CHANNEL_ID)
                experiment = reel_ledger.get_trial_experiment(conn, "PILOT-000")
            self.assertEqual(row["status"], reel_ledger.STATUS_PUBLISHED)
            self.assertEqual(row["media_id"], "18544612528074609")
            self.assertEqual(row["trial_reel"], 1)
            self.assertEqual(experiment["state"], reel_ledger.TRIAL_STATE_ACTIVE)
            self.assertEqual(experiment["parent_media_id"], parent["media_id"])
            manifest = reel_scheduler.read_json(manifest_path)
            self.assertEqual(
                manifest["reel_ledger"],
                {"content_hash": trial_hash, "channel_id": CHANNEL_ID},
            )
            self.assertEqual(
                manifest["trial_experiment"]["experiment_id"],
                "PILOT-000",
            )


if __name__ == "__main__":
    unittest.main()
