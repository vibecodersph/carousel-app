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

    def test_additive_published_variant_keeps_regular_queue_and_is_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            scheduled_at = "2026-08-05T19:00:00+09:00"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="add-parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Additive baseline hook",
                    published_at="2026-07-20T04:00:00+00:00",
                    media_id="178900011",
                )
                regular, regular_manifest = self.make_reel(
                    conn,
                    root=root,
                    content_hash="regular-queue",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Regular Reel stays put",
                    scheduled_at="2026-08-05T18:00:00+09:00",
                    source_video="source-b",
                )
            media = root / "additive-variant.mp4"
            media.write_bytes(b"distinct-additive-rerender")
            call = {
                "db_path": db,
                "channel_id": CHANNEL_ID,
                "parent_content_hash": "add-parent",
                "media_path": media,
                "experiment_id": "TRIAL-ADD-001",
                "variant_hook": "Sharper additive hook",
                "scheduled_at": scheduled_at,
                "expected_scheduled_at": scheduled_at,
                "asset_family_id": None,
                "changed_variables": None,
                "graduation_strategy": "MANUAL",
                "out_dir": root / "trial-manifests",
                "caption_mode": "preserve-parent",
                "now": datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
            }

            preview = reel_scheduler.add_trial_from_published(
                **call,
                apply=False,
            )
            self.assertEqual(preview["mode"], "dry-run")
            self.assertTrue(preview["additive"])
            self.assertIsNone(preview["displaced_content_hash"])
            variant_hash = reel_ledger.hash_file(media)
            with reel_ledger.connect(db) as conn:
                self.assertIsNone(
                    reel_ledger.get_reel(conn, variant_hash, CHANNEL_ID)
                )
                unchanged = reel_ledger.get_reel(
                    conn,
                    "regular-queue",
                    CHANNEL_ID,
                )
            self.assertEqual(unchanged["scheduled_at"], regular["scheduled_at"])

            applied = reel_scheduler.add_trial_from_published(
                **call,
                apply=True,
            )
            self.assertEqual(applied["mode"], "apply")
            with reel_ledger.connect(db) as conn:
                variant = reel_ledger.get_reel(conn, variant_hash, CHANNEL_ID)
                unchanged = reel_ledger.get_reel(
                    conn,
                    "regular-queue",
                    CHANNEL_ID,
                )
                experiment = reel_ledger.get_trial_experiment(
                    conn,
                    "TRIAL-ADD-001",
                )
            self.assertEqual(variant["status"], reel_ledger.STATUS_SCHEDULED)
            self.assertEqual(variant["scheduled_at"], scheduled_at)
            self.assertEqual(variant["trial_reel"], 1)
            self.assertEqual(
                variant["caption"],
                "Additive baseline hook\n\nCaption body",
            )
            self.assertEqual(unchanged["status"], reel_ledger.STATUS_SCHEDULED)
            self.assertEqual(unchanged["scheduled_at"], regular["scheduled_at"])
            self.assertEqual(
                Path(unchanged["manifest_path"]),
                regular_manifest,
            )
            self.assertIsNone(experiment["displaced_content_hash"])
            self.assertEqual(
                json.loads(experiment["changed_variables_json"]),
                ["overlay_hook"],
            )
            manifest = reel_scheduler.read_json(Path(variant["manifest_path"]))
            self.assertTrue(manifest["instagram_trial_reel"]["enabled"])
            self.assertEqual(
                manifest["trial_experiment"]["experiment_id"],
                "TRIAL-ADD-001",
            )
            self.assertEqual(
                manifest["instagram_caption"],
                "Additive baseline hook\n\nCaption body",
            )

            repeated_call = dict(call)
            repeated_call["now"] = datetime.fromisoformat(
                "2026-08-06T00:00:00+00:00"
            )
            repeated = reel_scheduler.add_trial_from_published(
                **repeated_call,
                apply=True,
            )
            self.assertEqual(repeated["mode"], "already-applied")
            self.assertTrue(repeated["idempotent"])

            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="second-add-parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Second additive parent",
                    published_at="2026-07-21T04:00:00+00:00",
                    media_id="178900011-b",
                    source_video="source-c",
                )
            permanent_call = {
                **call,
                "scheduled_at": "2026-08-08T19:00:00+09:00",
                "expected_scheduled_at": "2026-08-08T19:00:00+09:00",
                "now": datetime.fromisoformat("2026-08-06T00:00:00+00:00"),
                "apply": False,
            }
            different_media = root / "different-additive-variant.mp4"
            different_media.write_bytes(b"different-additive-rerender")
            with self.assertRaisesRegex(
                SystemExit,
                "Published parent is already linked",
            ):
                reel_scheduler.add_trial_from_published(
                    **{
                        **permanent_call,
                        "media_path": different_media,
                        "experiment_id": "TRIAL-ADD-PARENT-REUSE",
                        "asset_family_id": "different-family",
                    }
                )
            with self.assertRaisesRegex(
                SystemExit,
                "media is already linked to Trial",
            ):
                reel_scheduler.add_trial_from_published(
                    **{
                        **permanent_call,
                        "parent_content_hash": "second-add-parent",
                        "experiment_id": "TRIAL-ADD-CONTENT-REUSE",
                        "asset_family_id": "another-family",
                    }
                )

    def test_fixed_rotation_pool_narrowly_allows_parent_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            first_at = "2026-08-05T19:00:00+09:00"
            second_at = "2026-08-08T19:00:00+09:00"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="pool-parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Pool baseline",
                    published_at="2026-07-20T04:00:00+00:00",
                    media_id="178900011-pool",
                    source_video="pool-source",
                )
            first_media = root / "pool-first.mp4"
            first_media.write_bytes(b"pool-first-rerender")
            base_call = {
                "db_path": db,
                "channel_id": CHANNEL_ID,
                "parent_content_hash": "pool-parent",
                "asset_family_id": "pool-source",
                "changed_variables": None,
                "graduation_strategy": "MANUAL",
                "out_dir": root / "trial-manifests",
                "caption_mode": "preserve-parent",
                "now": datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
            }
            reel_scheduler.add_trial_from_published(
                **base_call,
                media_path=first_media,
                experiment_id="TRIAL-POOL-FIRST",
                variant_hook="First pool hook",
                scheduled_at=first_at,
                expected_scheduled_at=first_at,
                apply=True,
            )

            second_media = root / "pool-second.mp4"
            second_media.write_bytes(b"pool-second-rerender")
            second_call = {
                **base_call,
                "media_path": second_media,
                "experiment_id": "TRIAL-POOL-SECOND",
                "variant_hook": "Second pool hook",
                "scheduled_at": second_at,
                "expected_scheduled_at": second_at,
                "apply": False,
            }
            with self.assertRaisesRegex(SystemExit, "already linked"):
                reel_scheduler.add_trial_from_published(**second_call)

            pool_path = root / "rotation.json"
            pool_path.write_text(
                json.dumps(
                    {
                        "pool_id": "test-pool",
                        "channel_id": CHANNEL_ID,
                        "timezone": "Asia/Tokyo",
                        "daily_time": "19:00",
                        "family_cooldown_hours": 72,
                        "entries": [
                            {
                                "content_hash": "pool-parent",
                                "source_video": "pool-source",
                                "enabled": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            authorized = reel_scheduler.add_trial_from_published(
                **second_call,
                rotation_pool_path=pool_path,
            )
            self.assertEqual(authorized["mode"], "dry-run")
            self.assertEqual(authorized["rotation_pool"]["pool_id"], "test-pool")
            self.assertEqual(authorized["rotation_pool"]["entry_index"], 0)

            wrong_slot = dict(second_call)
            wrong_slot["scheduled_at"] = "2026-08-08T18:00:00+09:00"
            wrong_slot["expected_scheduled_at"] = wrong_slot["scheduled_at"]
            with self.assertRaisesRegex(SystemExit, "configured daily slot"):
                reel_scheduler.add_trial_from_published(
                    **wrong_slot,
                    rotation_pool_path=pool_path,
                )

    def test_rotation_pool_validates_required_hook_phrase_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool_path = Path(temporary) / "rotation.json"
            entry = {
                "content_hash": "pool-parent",
                "source_video": "pool-source",
                "enabled": True,
            }
            pool = {
                "pool_id": "test-pool",
                "channel_id": CHANNEL_ID,
                "timezone": "Asia/Tokyo",
                "daily_time": "19:00",
                "family_cooldown_hours": 72,
                "entries": [entry],
            }
            call = {
                "pool_path": pool_path,
                "channel_id": CHANNEL_ID,
                "parent_content_hash": "pool-parent",
                "parent_is_trial_reel": False,
                "asset_family_id": "pool-source",
                "scheduled_at": datetime.fromisoformat("2026-08-05T19:00:00+09:00"),
            }
            invalid_phrases = (
                None, "中国製AI", [None], [7], [""], ["  "], ["中国製AI", ""]
            )
            for phrases in invalid_phrases:
                with self.subTest(phrases=phrases):
                    entry["required_hook_phrases"] = phrases
                    pool_path.write_text(json.dumps(pool), encoding="utf-8")
                    with self.assertRaisesRegex(SystemExit, "required_hook_phrases"):
                        reel_scheduler.load_trial_rotation_pool_entry(**call)

            entry["required_hook_phrases"] = ["中国製AI"]
            pool_path.write_text(json.dumps(pool), encoding="utf-8")
            loaded = reel_scheduler.load_trial_rotation_pool_entry(**call)
            self.assertEqual(loaded["required_hook_phrases"], ["中国製AI"])

            del entry["required_hook_phrases"]
            pool_path.write_text(json.dumps(pool), encoding="utf-8")
            loaded = reel_scheduler.load_trial_rotation_pool_entry(**call)
            self.assertEqual(loaded["required_hook_phrases"], [])

    def test_rotation_pool_requires_literal_hook_phrase_before_scheduling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="constrained-parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="中国製AIは回答する",
                    published_at="2026-07-20T04:00:00+00:00",
                    media_id="178900011-constrained",
                    source_video="constrained-source",
                )
            media = root / "constrained-variant.mp4"
            media.write_bytes(b"constrained-variant")
            pool_path = root / "rotation.json"
            pool_path.write_text(
                json.dumps(
                    {
                        "pool_id": "test-constrained-pool",
                        "channel_id": CHANNEL_ID,
                        "timezone": "Asia/Tokyo",
                        "daily_time": "19:00",
                        "family_cooldown_hours": 72,
                        "entries": [
                            {
                                "content_hash": "constrained-parent",
                                "source_video": "constrained-source",
                                "enabled": True,
                                "required_hook_phrases": ["中国製AI"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            call = {
                "db_path": db,
                "channel_id": CHANNEL_ID,
                "parent_content_hash": "constrained-parent",
                "media_path": media,
                "experiment_id": "TRIAL-CONSTRAINED-001",
                "scheduled_at": "2026-08-05T19:00:00+09:00",
                "expected_scheduled_at": "2026-08-05T19:00:00+09:00",
                "asset_family_id": "constrained-source",
                "changed_variables": None,
                "graduation_strategy": "MANUAL",
                "out_dir": root / "trial-manifests",
                "now": datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
                "rotation_pool_path": pool_path,
            }
            variant_hash = reel_ledger.hash_file(media)
            invalid_hooks = (
                "米国AIは翻訳も拒否",
                "中国のAIは回答する",
                "中国製 AIは回答する",
                "中国製ＡＩは回答する",
            )
            for hook in invalid_hooks:
                for apply in (False, True):
                    with self.subTest(hook=hook, apply=apply):
                        with self.assertRaisesRegex(SystemExit, "verbatim.*中国製AI"):
                            reel_scheduler.add_trial_from_published(
                                **call,
                                variant_hook=hook,
                                apply=apply,
                            )
                        with reel_ledger.connect(db) as conn:
                            self.assertIsNone(
                                reel_ledger.get_reel(conn, variant_hash, CHANNEL_ID)
                            )
                            self.assertIsNone(
                                reel_ledger.get_trial_experiment(
                                    conn, call["experiment_id"]
                                )
                            )
                        self.assertFalse(call["out_dir"].exists())

            valid_hook = "米国AIは翻訳も拒否、中国製AIはなぜ答えた？"
            preview = reel_scheduler.add_trial_from_published(
                **call, variant_hook=valid_hook, apply=False
            )
            self.assertEqual(
                preview["rotation_pool"]["required_hook_phrases"], ["中国製AI"]
            )
            applied = reel_scheduler.add_trial_from_published(
                **call, variant_hook=valid_hook, apply=True
            )
            self.assertEqual(applied["mode"], "apply")
            with reel_ledger.connect(db) as conn:
                experiment = reel_ledger.get_trial_experiment(
                    conn, call["experiment_id"]
                )
                variant = reel_ledger.get_reel(conn, variant_hash, CHANNEL_ID)
            self.assertEqual(experiment["variant_hook"], valid_hook)
            self.assertEqual(variant["status"], reel_ledger.STATUS_SCHEDULED)

    def test_rotation_pool_requires_explicit_permission_for_trial_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            self.make_active_trial(
                db=db,
                root=root,
                experiment_id="TRIAL-PARENT-ORIGIN",
                content_hash="published-trial-parent",
                published_at="2026-07-20T04:00:00+00:00",
                media_id="178900011-trial-parent",
            )
            media = root / "nested-trial-variant.mp4"
            media.write_bytes(b"nested-trial-variant")
            pool_path = root / "rotation.json"
            pool = {
                "pool_id": "test-trial-parent-pool",
                "channel_id": CHANNEL_ID,
                "timezone": "Asia/Tokyo",
                "daily_time": "19:00",
                "family_cooldown_hours": 72,
                "entries": [
                    {
                        "content_hash": "published-trial-parent",
                        "source_video": "source-a",
                        "enabled": True,
                    }
                ],
            }
            pool_path.write_text(json.dumps(pool), encoding="utf-8")
            call = {
                "db_path": db,
                "channel_id": CHANNEL_ID,
                "parent_content_hash": "published-trial-parent",
                "media_path": media,
                "experiment_id": "TRIAL-NESTED-001",
                "variant_hook": "Nested Trial hook",
                "scheduled_at": "2026-08-05T19:00:00+09:00",
                "expected_scheduled_at": "2026-08-05T19:00:00+09:00",
                "asset_family_id": "source-a",
                "changed_variables": None,
                "graduation_strategy": "MANUAL",
                "out_dir": root / "trial-manifests",
                "apply": False,
                "caption_mode": "preserve-parent",
                "now": datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
                "rotation_pool_path": pool_path,
            }

            with self.assertRaisesRegex(SystemExit, "allow_trial_parent=true"):
                reel_scheduler.add_trial_from_published(**call)

            pool["entries"][0]["allow_trial_parent"] = True
            pool_path.write_text(json.dumps(pool), encoding="utf-8")
            authorized = reel_scheduler.add_trial_from_published(**call)
            self.assertTrue(authorized["rotation_pool"]["allow_trial_parent"])

    def test_additive_published_variant_rejects_slot_drift_past_and_collision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            scheduled_at = "2026-08-05T19:00:00+09:00"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="add-parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Baseline",
                    media_id="178900012",
                )
            media = root / "additive-variant.mp4"
            media.write_bytes(b"additive-variant")
            call = {
                "db_path": db,
                "channel_id": CHANNEL_ID,
                "parent_content_hash": "add-parent",
                "media_path": media,
                "experiment_id": "TRIAL-ADD-GUARDS",
                "variant_hook": "Variant",
                "scheduled_at": scheduled_at,
                "asset_family_id": None,
                "changed_variables": None,
                "graduation_strategy": "MANUAL",
                "out_dir": root / "trial-manifests",
                "apply": False,
            }
            with self.assertRaisesRegex(SystemExit, "timeslot changed"):
                reel_scheduler.add_trial_from_published(
                    **call,
                    expected_scheduled_at="2026-08-06T19:00:00+09:00",
                    now=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
                )
            with self.assertRaisesRegex(SystemExit, "must be in the future"):
                reel_scheduler.add_trial_from_published(
                    **call,
                    expected_scheduled_at=scheduled_at,
                    now=datetime.fromisoformat("2026-08-06T00:00:00+00:00"),
                )
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="occupied",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Already at 19:00",
                    scheduled_at=scheduled_at,
                    source_video="source-b",
                )
            with self.assertRaisesRegex(SystemExit, "already occupied"):
                reel_scheduler.add_trial_from_published(
                    **call,
                    expected_scheduled_at=scheduled_at,
                    now=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
                )

    def test_additive_family_cooldown_is_half_open_and_symmetric(self) -> None:
        existing_at = "2026-08-05T19:00:00+09:00"
        cases = (
            ("2026-08-02T19:00:00+09:00", False),
            ("2026-08-08T19:00:00+09:00", False),
            ("2026-08-02T19:00:01+09:00", True),
            ("2026-08-08T18:59:59+09:00", True),
        )
        for candidate_at, overlaps in cases:
            with self.subTest(candidate_at=candidate_at, overlaps=overlaps):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    db = root / "reels.db"
                    with reel_ledger.connect(db) as conn:
                        self.make_reel(
                            conn,
                            root=root,
                            content_hash="candidate-parent",
                            status=reel_ledger.STATUS_PUBLISHED,
                            title="Candidate baseline",
                            media_id="178900013",
                            source_video="candidate-source",
                        )
                        self.make_reel(
                            conn,
                            root=root,
                            content_hash="existing-trial-content",
                            status=reel_ledger.STATUS_SCHEDULED,
                            title="Existing cross-lane Trial",
                            scheduled_at=existing_at,
                            source_video="shared-family",
                        )
                        reel_ledger.upsert_trial_experiment(
                            conn,
                            experiment_id="EXISTING-CROSS-LANE-FAMILY",
                            content_hash="existing-trial-content",
                            channel_id=CHANNEL_ID,
                            case_type=(
                                reel_ledger.TRIAL_CASE_SCHEDULED_CONVERSION
                            ),
                            parent_content_hash=None,
                            parent_media_id=None,
                            asset_family_id="shared-family/legacy-clip",
                            baseline_hook="Existing baseline",
                            variant_hook="Existing variant",
                            changed_variables_json='["distribution_mode"]',
                            state=reel_ledger.TRIAL_STATE_SCHEDULED,
                            scheduled_at=existing_at,
                        )
                    media = root / "candidate-variant.mp4"
                    media.write_bytes(b"candidate-variant")
                    call = {
                        "db_path": db,
                        "channel_id": CHANNEL_ID,
                        "parent_content_hash": "candidate-parent",
                        "media_path": media,
                        "experiment_id": "TRIAL-ADD-FAMILY-CANDIDATE",
                        "variant_hook": "Candidate variant",
                        "scheduled_at": candidate_at,
                        "expected_scheduled_at": candidate_at,
                        "asset_family_id": "shared-family",
                        "changed_variables": None,
                        "graduation_strategy": "MANUAL",
                        "out_dir": root / "trial-manifests",
                        "caption_mode": "preserve-parent",
                        "now": datetime.fromisoformat(
                            "2026-08-01T00:00:00+00:00"
                        ),
                        "apply": False,
                    }
                    if overlaps:
                        with self.assertRaisesRegex(
                            SystemExit,
                            "overlapping 72-hour observation window",
                        ):
                            reel_scheduler.add_trial_from_published(**call)
                    else:
                        preview = reel_scheduler.add_trial_from_published(
                            **call
                        )
                        self.assertEqual(preview["mode"], "dry-run")

    def test_never_published_terminal_trials_do_not_block_family_cooldown(
        self,
    ) -> None:
        for terminal_state in (
            reel_ledger.TRIAL_STATE_STOPPED,
            reel_ledger.TRIAL_STATE_FAILED,
        ):
            with self.subTest(terminal_state=terminal_state):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    db = root / "reels.db"
                    existing_at = "2026-08-05T19:00:00+09:00"
                    candidate_at = "2026-08-06T19:00:00+09:00"
                    with reel_ledger.connect(db) as conn:
                        self.make_reel(
                            conn,
                            root=root,
                            content_hash="retired-before-launch",
                            status=reel_ledger.STATUS_SKIPPED,
                            title="Retired before launch",
                            scheduled_at=existing_at,
                            source_video="shared-family",
                        )
                        reel_ledger.upsert_trial_experiment(
                            conn,
                            experiment_id=f"TERMINAL-{terminal_state}",
                            content_hash="retired-before-launch",
                            channel_id=CHANNEL_ID,
                            case_type=(
                                reel_ledger.TRIAL_CASE_SUCCESSFUL_POST_VARIANT
                            ),
                            parent_content_hash="old-parent",
                            parent_media_id="old-parent-media",
                            asset_family_id="shared-family",
                            baseline_hook="Old baseline",
                            variant_hook="Retired variant",
                            changed_variables_json='["overlay_hook"]',
                            state=terminal_state,
                            scheduled_at=existing_at,
                        )
                        self.make_reel(
                            conn,
                            root=root,
                            content_hash="new-parent",
                            status=reel_ledger.STATUS_PUBLISHED,
                            title="New parent",
                            published_at="2026-07-20T04:00:00+00:00",
                            media_id="new-parent-media",
                            source_video="shared-family",
                        )
                    media = root / "new-variant.mp4"
                    media.write_bytes(f"new-{terminal_state}".encode())
                    preview = reel_scheduler.add_trial_from_published(
                        db_path=db,
                        channel_id=CHANNEL_ID,
                        parent_content_hash="new-parent",
                        media_path=media,
                        experiment_id=f"NEW-AFTER-{terminal_state}",
                        variant_hook="New pool variant",
                        scheduled_at=candidate_at,
                        expected_scheduled_at=candidate_at,
                        asset_family_id="shared-family",
                        changed_variables=None,
                        graduation_strategy="MANUAL",
                        out_dir=root / "trial-manifests",
                        apply=False,
                        now=datetime.fromisoformat(
                            "2026-08-01T00:00:00+00:00"
                        ),
                    )
                    self.assertEqual(preview["mode"], "dry-run")

    def test_published_stopped_trial_still_blocks_family_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            existing_at = "2026-08-05T19:00:00+09:00"
            candidate_at = "2026-08-06T19:00:00+09:00"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="published-stopped-trial",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Published stopped Trial",
                    scheduled_at=existing_at,
                    published_at="2026-08-05T10:00:00+00:00",
                    media_id="published-stopped-media",
                    source_video="shared-family",
                )
                reel_ledger.upsert_trial_experiment(
                    conn,
                    experiment_id="PUBLISHED-STOPPED",
                    content_hash="published-stopped-trial",
                    channel_id=CHANNEL_ID,
                    case_type=reel_ledger.TRIAL_CASE_SUCCESSFUL_POST_VARIANT,
                    parent_content_hash="old-parent",
                    parent_media_id="old-parent-media",
                    asset_family_id="shared-family",
                    baseline_hook="Old baseline",
                    variant_hook="Published variant",
                    changed_variables_json='["overlay_hook"]',
                    state=reel_ledger.TRIAL_STATE_STOPPED,
                    scheduled_at=existing_at,
                    published_at="2026-08-05T10:00:00+00:00",
                )
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="candidate-parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Candidate parent",
                    published_at="2026-07-20T04:00:00+00:00",
                    media_id="candidate-parent-media",
                    source_video="shared-family",
                )
            media = root / "candidate-after-published-stop.mp4"
            media.write_bytes(b"candidate-after-published-stop")
            with self.assertRaisesRegex(
                SystemExit,
                "overlapping 72-hour observation window",
            ):
                reel_scheduler.add_trial_from_published(
                    db_path=db,
                    channel_id=CHANNEL_ID,
                    parent_content_hash="candidate-parent",
                    media_path=media,
                    experiment_id="CANDIDATE-AFTER-PUBLISHED-STOP",
                    variant_hook="Candidate variant",
                    scheduled_at=candidate_at,
                    expected_scheduled_at=candidate_at,
                    asset_family_id="shared-family",
                    changed_variables=None,
                    graduation_strategy="MANUAL",
                    out_dir=root / "trial-manifests",
                    apply=False,
                    now=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
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

    def test_resequence_queued_trials_keeps_ledgers_and_manifests_in_lockstep(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            queued = (
                ("trial-first", "TRIAL-RESEQUENCE-001", "2026-08-10T19:00:00+09:00"),
                ("trial-second", "TRIAL-RESEQUENCE-002", "2026-08-12T19:00:00+09:00"),
            )
            manifests: dict[str, Path] = {}
            with reel_ledger.connect(db) as conn:
                for content_hash, experiment_id, scheduled_at in queued:
                    _, manifest_path = self.make_reel(
                        conn,
                        root=root,
                        content_hash=content_hash,
                        status=reel_ledger.STATUS_SCHEDULED,
                        title=f"Trial hook {content_hash}",
                        scheduled_at=scheduled_at,
                        source_video=f"source-{content_hash}",
                    )
                    reel_ledger.set_status(
                        conn,
                        content_hash,
                        CHANNEL_ID,
                        reel_ledger.STATUS_SCHEDULED,
                        scheduled_at=scheduled_at,
                        trial_reel=1,
                        trial_graduation_strategy="MANUAL",
                    )
                    reel_ledger.upsert_trial_experiment(
                        conn,
                        experiment_id=experiment_id,
                        content_hash=content_hash,
                        channel_id=CHANNEL_ID,
                        case_type=reel_ledger.TRIAL_CASE_SUCCESSFUL_POST_VARIANT,
                        parent_content_hash=f"parent-{content_hash}",
                        parent_media_id=f"media-{content_hash}",
                        asset_family_id=f"source-{content_hash}",
                        baseline_hook="Baseline",
                        variant_hook=f"Trial hook {content_hash}",
                        changed_variables_json='["overlay_hook"]',
                        state=reel_ledger.TRIAL_STATE_SCHEDULED,
                        scheduled_at=scheduled_at,
                    )
                    manifest = reel_scheduler.read_json(manifest_path)
                    manifest["instagram_trial_reel"] = {
                        "enabled": True,
                        "graduation_strategy": "MANUAL",
                    }
                    manifest["trial_experiment"] = {"experiment_id": experiment_id}
                    reel_scheduler.write_json(manifest_path, manifest)
                    manifests[content_hash] = manifest_path

            call = {
                "db_path": db,
                "channel_id": CHANNEL_ID,
                "start_at_text": "2026-08-10T19:00:00+09:00",
                "now": datetime.fromisoformat("2026-08-09T03:00:00+00:00"),
            }
            preview = reel_scheduler.resequence_queued_trial_reels(
                **call,
                apply=False,
            )
            self.assertEqual(preview["mode"], "dry-run")
            self.assertEqual(
                [move["scheduled_at"] for move in preview["moves"]],
                ["2026-08-10T19:00:00+09:00", "2026-08-11T19:00:00+09:00"],
            )
            with reel_ledger.connect(db) as conn:
                self.assertEqual(
                    reel_ledger.get_reel(conn, "trial-second", CHANNEL_ID)["scheduled_at"],
                    "2026-08-12T19:00:00+09:00",
                )

            applied = reel_scheduler.resequence_queued_trial_reels(
                **call,
                apply=True,
            )
            self.assertEqual(applied["mode"], "apply")
            with reel_ledger.connect(db) as conn:
                second = reel_ledger.get_reel(conn, "trial-second", CHANNEL_ID)
                second_experiment = reel_ledger.get_trial_experiment(
                    conn,
                    "TRIAL-RESEQUENCE-002",
                )
            self.assertEqual(second["scheduled_at"], "2026-08-11T19:00:00+09:00")
            self.assertEqual(
                second_experiment["scheduled_at"],
                "2026-08-11T19:00:00+09:00",
            )
            second_manifest = reel_scheduler.read_json(manifests["trial-second"])
            self.assertEqual(
                second_manifest["scheduled_at"],
                "2026-08-11T19:00:00+09:00",
            )
            self.assertEqual(
                second_manifest["trial_experiment"]["experiment_id"],
                "TRIAL-RESEQUENCE-002",
            )
            self.assertTrue(second_manifest["instagram_trial_reel"]["enabled"])

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

    def test_retire_unpublished_scheduled_conversions_preserves_published_trials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                _, queued_manifest = self.make_reel(
                    conn,
                    root=root,
                    content_hash="queued-conversion",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Queued hook",
                    scheduled_at="2026-08-03T13:00:00+09:00",
                    source_video="source-a",
                )
                _, published_manifest = self.make_reel(
                    conn,
                    root=root,
                    content_hash="published-conversion",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Published hook",
                    scheduled_at="2026-08-04T13:00:00+09:00",
                    source_video="source-b",
                )
            for content_hash, experiment_id in (
                ("queued-conversion", "TRIAL-QUEUED"),
                ("published-conversion", "TRIAL-PUBLISHED"),
            ):
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
                    "published-conversion",
                    CHANNEL_ID,
                    reel_ledger.STATUS_PUBLISHED,
                    published_at="2026-08-04T04:00:00+00:00",
                    media_id="178900004",
                )

            retired = (
                reel_scheduler.retire_unpublished_scheduled_trial_conversions(
                    db_path=db,
                    channel_id=CHANNEL_ID,
                    apply=True,
                )
            )

            self.assertEqual(
                [item["content_hash"] for item in retired],
                ["queued-conversion"],
            )
            with reel_ledger.connect(db) as conn:
                queued = reel_ledger.get_reel(
                    conn, "queued-conversion", CHANNEL_ID
                )
                published = reel_ledger.get_reel(
                    conn, "published-conversion", CHANNEL_ID
                )
                queued_experiment = reel_ledger.get_trial_experiment(
                    conn, "TRIAL-QUEUED"
                )
                published_experiment = reel_ledger.get_trial_experiment(
                    conn, "TRIAL-PUBLISHED"
                )
            self.assertEqual(queued["trial_reel"], 0)
            self.assertIsNone(queued["trial_graduation_strategy"])
            self.assertIsNone(queued_experiment)
            queued_data = reel_scheduler.read_json(queued_manifest)
            self.assertEqual(queued_data["source_type"], "scheduled_reel")
            self.assertNotIn("instagram_trial_reel", queued_data)
            self.assertNotIn("trial_experiment", queued_data)
            self.assertEqual(published["trial_reel"], 1)
            self.assertIsNotNone(published_experiment)
            self.assertIn(
                "instagram_trial_reel",
                reel_scheduler.read_json(published_manifest),
            )

    def test_retire_all_unpublished_trials_preserves_regular_slots_and_audit_records(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            conversion_at = "2026-08-03T13:00:00+09:00"
            additive_at = "2026-08-03T19:00:00+09:00"
            regular_at = "2026-08-03T18:00:00+09:00"
            with reel_ledger.connect(db) as conn:
                _, conversion_manifest = self.make_reel(
                    conn,
                    root=root,
                    content_hash="retire-conversion",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Conversion returns to regular",
                    scheduled_at=conversion_at,
                    source_video="conversion-source",
                )
                regular, regular_manifest = self.make_reel(
                    conn,
                    root=root,
                    content_hash="regular-control",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Regular Reel remains untouched",
                    scheduled_at=regular_at,
                    source_video="regular-source",
                )
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="retire-parent",
                    status=reel_ledger.STATUS_PUBLISHED,
                    title="Published parent",
                    published_at="2026-07-20T04:00:00+00:00",
                    media_id="178900-retire-parent",
                    source_video="additive-source",
                )

            reel_scheduler.convert_scheduled_reel_to_trial(
                db_path=db,
                channel_id=CHANNEL_ID,
                content_hash="retire-conversion",
                experiment_id="TRIAL-RETIRE-CONVERSION",
                hook=None,
                asset_family_id=None,
                changed_variables=None,
                graduation_strategy="MANUAL",
                apply=True,
            )
            additive_media = root / "retire-additive.mp4"
            additive_media.write_bytes(b"retire-additive-variant")
            additive = reel_scheduler.add_trial_from_published(
                db_path=db,
                channel_id=CHANNEL_ID,
                parent_content_hash="retire-parent",
                media_path=additive_media,
                experiment_id="TRIAL-RETIRE-ADDITIVE",
                variant_hook="Additive variant is retired",
                scheduled_at=additive_at,
                expected_scheduled_at=additive_at,
                asset_family_id="additive-source",
                changed_variables=None,
                graduation_strategy="MANUAL",
                out_dir=root / "trial-manifests",
                apply=True,
                now=datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
            )
            additive_hash = str(additive["content_hash"])
            additive_manifest = Path(str(additive["manifest_path"]))

            parser_args = reel_scheduler.build_parser().parse_args(
                [
                    "trial-retire-unpublished",
                    "--channel",
                    CHANNEL_ID,
                    "--db",
                    str(db),
                ]
            )
            self.assertFalse(parser_args.apply)

            reason = "replaced by fixed Trial rotation pool"
            preview = reel_scheduler.retire_unpublished_trial_reels(
                db_path=db,
                channel_id=CHANNEL_ID,
                reason=reason,
                apply=False,
            )
            self.assertEqual(preview["mode"], "dry-run")
            self.assertEqual(preview["count"], 2)
            self.assertEqual(preview["scheduled_conversions"], 1)
            self.assertEqual(preview["additive_variants"], 1)
            with reel_ledger.connect(db) as conn:
                conversion_before = reel_ledger.get_reel(
                    conn,
                    "retire-conversion",
                    CHANNEL_ID,
                )
                additive_before = reel_ledger.get_reel(
                    conn,
                    additive_hash,
                    CHANNEL_ID,
                )
            self.assertEqual(conversion_before["trial_reel"], 1)
            self.assertEqual(additive_before["status"], reel_ledger.STATUS_SCHEDULED)
            self.assertIn(
                "instagram_trial_reel",
                reel_scheduler.read_json(conversion_manifest),
            )
            self.assertIn(
                "instagram_trial_reel",
                reel_scheduler.read_json(additive_manifest),
            )

            applied = reel_scheduler.retire_unpublished_trial_reels(
                db_path=db,
                channel_id=CHANNEL_ID,
                reason=reason,
                apply=True,
            )
            self.assertEqual(applied["mode"], "apply")
            self.assertEqual(applied["count"], 2)
            with reel_ledger.connect(db) as conn:
                conversion = reel_ledger.get_reel(
                    conn,
                    "retire-conversion",
                    CHANNEL_ID,
                )
                additive_row = reel_ledger.get_reel(
                    conn,
                    additive_hash,
                    CHANNEL_ID,
                )
                regular_after = reel_ledger.get_reel(
                    conn,
                    "regular-control",
                    CHANNEL_ID,
                )
                conversion_experiment = reel_ledger.get_trial_experiment(
                    conn,
                    "TRIAL-RETIRE-CONVERSION",
                )
                additive_experiment = reel_ledger.get_trial_experiment(
                    conn,
                    "TRIAL-RETIRE-ADDITIVE",
                )

            self.assertEqual(conversion["status"], reel_ledger.STATUS_SCHEDULED)
            self.assertEqual(conversion["scheduled_at"], conversion_at)
            self.assertEqual(conversion["trial_reel"], 0)
            self.assertIsNone(conversion["trial_graduation_strategy"])
            self.assertEqual(additive_row["status"], reel_ledger.STATUS_SKIPPED)
            self.assertIsNone(additive_row["scheduled_at"])
            self.assertEqual(additive_row["trial_reel"], 0)
            self.assertEqual(additive_row["last_error"], reason)
            self.assertEqual(regular_after["status"], regular["status"])
            self.assertEqual(regular_after["scheduled_at"], regular["scheduled_at"])
            self.assertEqual(Path(regular_after["manifest_path"]), regular_manifest)
            for experiment in (conversion_experiment, additive_experiment):
                self.assertIsNotNone(experiment)
                self.assertEqual(experiment["state"], reel_ledger.TRIAL_STATE_STOPPED)
                self.assertEqual(experiment["decision"], "stop")
                self.assertEqual(experiment["decision_reason"], reason)
                self.assertEqual(experiment["decision_at"], experiment["stopped_at"])

            conversion_data = reel_scheduler.read_json(conversion_manifest)
            self.assertEqual(conversion_data["source_type"], "scheduled_reel")
            self.assertEqual(conversion_data["scheduled_at"], conversion_at)
            self.assertNotIn("instagram_trial_reel", conversion_data)
            self.assertNotIn("trial_experiment", conversion_data)
            additive_data = reel_scheduler.read_json(additive_manifest)
            self.assertEqual(
                additive_data["schedule_status"],
                reel_ledger.STATUS_SKIPPED,
            )
            self.assertNotIn("scheduled_at", additive_data)
            self.assertNotIn("instagram_trial_reel", additive_data)
            self.assertEqual(
                additive_data["trial_experiment"]["experiment_id"],
                "TRIAL-RETIRE-ADDITIVE",
            )
            repeated = reel_scheduler.retire_unpublished_trial_reels(
                db_path=db,
                channel_id=CHANNEL_ID,
                reason=reason,
                apply=True,
            )
            self.assertEqual(repeated["count"], 0)

    def test_scheduled_conversion_rejects_second_conversion_on_same_date(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db = root / "reels.db"
            with reel_ledger.connect(db) as conn:
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="first",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="First hook",
                    scheduled_at="2026-08-03T09:00:00+09:00",
                    source_video="source-a",
                )
                self.make_reel(
                    conn,
                    root=root,
                    content_hash="second",
                    status=reel_ledger.STATUS_SCHEDULED,
                    title="Second hook",
                    scheduled_at="2026-08-03T18:00:00+09:00",
                    source_video="source-b",
                )
            reel_scheduler.convert_scheduled_reel_to_trial(
                db_path=db,
                channel_id=CHANNEL_ID,
                content_hash="first",
                experiment_id="TRIAL-FIRST",
                hook=None,
                asset_family_id=None,
                changed_variables=None,
                graduation_strategy="MANUAL",
                apply=True,
            )

            with self.assertRaisesRegex(
                SystemExit,
                "scheduled-conversion Trial already exists",
            ):
                reel_scheduler.convert_scheduled_reel_to_trial(
                    db_path=db,
                    channel_id=CHANNEL_ID,
                    content_hash="second",
                    experiment_id="TRIAL-SECOND",
                    hook=None,
                    asset_family_id=None,
                    changed_variables=None,
                    graduation_strategy="MANUAL",
                    apply=False,
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
