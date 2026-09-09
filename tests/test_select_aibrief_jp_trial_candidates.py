from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import reel_ledger
from scripts import select_aibrief_jp_trial_candidates as selector


CHANNEL = "aibrief_jp"


def content_hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class TrialRotationPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "reels.db"
        self.config = self.root / "rotation.json"
        self.as_of = datetime(2026, 8, 30, 0, 30, tzinfo=selector.JST)
        self.entries = [
            self.entry("a", family="family-a"),
            self.entry("b", family="family-b"),
            self.entry("c", family="family-c"),
            self.entry("d", family="family-d"),
        ]
        self.write_config(self.entries)
        with reel_ledger.connect(self.db):
            pass

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def entry(
        self,
        label: str,
        *,
        family: str,
        enabled: bool = True,
    ) -> dict[str, object]:
        return {
            "content_hash": content_hash(f"parent-{label}"),
            "source_video": family,
            "clip_name": f"001-{label}",
            "title": f"Parent {label.upper()}",
            "enabled": enabled,
        }

    def write_config(self, entries: list[dict[str, object]], **updates: object) -> None:
        payload: dict[str, object] = {
            "schema_version": 1,
            "channel_id": CHANNEL,
            "pool_id": "test-photo-pool",
            "timezone": "Asia/Tokyo",
            "daily_time": "19:00",
            "family_cooldown_hours": 72,
            "entries": entries,
        }
        payload.update(updates)
        self.config.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def insert_parent(
        self,
        entry: dict[str, object],
        *,
        status: str = reel_ledger.STATUS_PUBLISHED,
        trial_reel: bool = False,
    ) -> None:
        clip_dir = self.root / "clips" / str(entry["clip_name"])
        clip_dir.mkdir(parents=True, exist_ok=True)
        media = clip_dir / "reel.mp4"
        media.write_bytes(str(entry["content_hash"]).encode("utf-8"))
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=str(entry["content_hash"]),
                channel_id=CHANNEL,
                lang="ja",
                clip_dir=clip_dir,
                media_path=media,
                source_video=str(entry["source_video"]),
                title=str(entry["title"]),
                status=status,
                published_at=(
                    (self.as_of - timedelta(days=14)).isoformat()
                    if status == reel_ledger.STATUS_PUBLISHED
                    else None
                ),
                media_id=(
                    f"media-{str(entry['content_hash'])[:8]}"
                    if status == reel_ledger.STATUS_PUBLISHED
                    else None
                ),
            )
            if trial_reel:
                connection.execute(
                    "UPDATE reels SET trial_reel=1 WHERE content_hash=? AND channel_id=?",
                    (str(entry["content_hash"]), CHANNEL),
                )

    def insert_regular_scheduled(self, *, label: str, scheduled_at: datetime) -> None:
        clip_dir = self.root / "regular" / label
        clip_dir.mkdir(parents=True, exist_ok=True)
        media = clip_dir / "reel.mp4"
        media.write_bytes(label.encode("utf-8"))
        manifest = clip_dir / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=content_hash(f"regular-{label}"),
                channel_id=CHANNEL,
                lang="ja",
                clip_dir=clip_dir,
                media_path=media,
                source_video=f"regular-{label}",
                title=f"Regular {label}",
                status=reel_ledger.STATUS_SCHEDULED,
                scheduled_at=scheduled_at.isoformat(),
                manifest_path=str(manifest),
            )

    def insert_pool_experiment(
        self,
        *,
        ordinal: int,
        parent: dict[str, object],
        launch: datetime,
        state: str = reel_ledger.TRIAL_STATE_ACTIVE,
        published: bool = True,
    ) -> str:
        variant_hash = content_hash(
            f"variant-{ordinal}-{parent['content_hash']}-{launch.isoformat()}"
        )
        clip_dir = self.root / "variants" / variant_hash[:10]
        clip_dir.mkdir(parents=True, exist_ok=True)
        media = clip_dir / "reel.mp4"
        media.write_bytes(variant_hash.encode("utf-8"))
        experiment_id = (
            selector.rotation_experiment_prefix("test-photo-pool")
            + f"{ordinal:04d}-A19-{str(parent['content_hash'])[:8]}"
        )
        with reel_ledger.connect(self.db) as connection:
            reel_ledger.upsert_imported(
                connection,
                content_hash=variant_hash,
                channel_id=CHANNEL,
                lang="ja",
                clip_dir=clip_dir,
                media_path=media,
                source_video=str(parent["source_video"]),
                title=f"Variant {ordinal}",
                status=(
                    reel_ledger.STATUS_PUBLISHED
                    if published
                    else reel_ledger.STATUS_SCHEDULED
                ),
                scheduled_at=launch.isoformat(),
                published_at=launch.isoformat() if published else None,
                media_id=f"variant-media-{ordinal}" if published else None,
            )
            reel_ledger.set_status(
                connection,
                variant_hash,
                CHANNEL,
                (
                    reel_ledger.STATUS_PUBLISHED
                    if published
                    else reel_ledger.STATUS_SCHEDULED
                ),
                scheduled_at=launch.isoformat(),
                published_at=launch.isoformat() if published else None,
                media_id=f"variant-media-{ordinal}" if published else None,
                trial_reel=1 if state in selector.NONTERMINAL_STATES else 0,
            )
            reel_ledger.upsert_trial_experiment(
                connection,
                experiment_id=experiment_id,
                content_hash=variant_hash,
                channel_id=CHANNEL,
                case_type=reel_ledger.TRIAL_CASE_SUCCESSFUL_POST_VARIANT,
                parent_content_hash=str(parent["content_hash"]),
                parent_media_id="parent-media",
                asset_family_id=str(parent["source_video"]),
                baseline_hook=str(parent["title"]),
                variant_hook=f"Variant {ordinal}",
                changed_variables_json='["overlay_hook"]',
                state=state,
                scheduled_at=launch.isoformat(),
                published_at=launch.isoformat() if published else None,
            )
        return experiment_id

    def build(self, **updates: object) -> dict[str, object]:
        call: dict[str, object] = {
            "db_path": self.db,
            "channel_id": CHANNEL,
            "as_of": self.as_of,
            "config_path": self.config,
            "report_path": self.root / "does-not-exist-report.json",
            "facebook_db": self.root / "does-not-exist-facebook.db",
        }
        call.update(updates)
        return selector.build_selection(**call)  # type: ignore[arg-type]

    def test_checked_in_photo_pool_has_eighteen_canonical_entries(self) -> None:
        config = selector.load_rotation_config(
            selector.DEFAULT_ROTATION_CONFIG,
            channel_id=CHANNEL,
        )

        self.assertEqual(len(config["entries"]), 18)
        self.assertEqual(config["daily_time"], "19:00")
        self.assertEqual(config["family_cooldown_hours"], 72.0)
        hashes = {item["content_hash"] for item in config["entries"]}
        self.assertIn(
            "babd790dbce0151be380893e792f9f916251bc7f8562614d30e8880926024854",
            hashes,
        )
        self.assertIn(
            "cf6d2db633d7ed556ca8fe2f450ab6868994d2059350fd21aec9a58954853a9a",
            hashes,
        )
        self.assertIn(
            "7054a21868aa647dee6de3161df81fe94d8520aa4d38368bd6ed126624ca6146",
            hashes,
        )
        boris_entry = next(
            item
            for item in config["entries"]
            if item.get("subject") == "Boris Cherny"
        )
        self.assertEqual(
            boris_entry["content_hash"],
            "cf6d2db633d7ed556ca8fe2f450ab6868994d2059350fd21aec9a58954853a9a",
        )
        self.assertEqual(boris_entry["source_video"], "We7BZVKbCVw")
        self.assertIn("joined Cursor", boris_entry["topic_constraint"])
        self.assertEqual(
            boris_entry["supersedes_content_hash"],
            "e70d47ab2a799e32fe098cbe858b57101682c822a6417bbfb0ff1bc255ea6452",
        )
        plan_mode_trial = next(
            item
            for item in config["entries"]
            if item["content_hash"]
            == "fc2c67427d419ebec17cad44e3db83cdde2a0da85fcf72f62d5e892ba5b7afb3"
        )
        self.assertTrue(plan_mode_trial["allow_trial_parent"])
        self.assertEqual(
            plan_mode_trial["origin_parent_content_hash"],
            "b36d92b1fe197ca2379d24a6f3265d725cc4291b002f0c468e2b1a153c6af77a",
        )
        self.assertEqual(
            sum(item["source_video"] == "qyPCVqFUyDo" for item in config["entries"]),
            1,
        )
        chinese_ai = config["entries"][-1]
        self.assertEqual(
            chinese_ai["content_hash"],
            "37868f7e990179216d8b4ab47f82b53bfa4aeced84b8a894746c8131051dfacd",
        )
        self.assertEqual(chinese_ai["required_hook_phrases"], ["中国製AI"])

    def test_required_hook_phrase_reaches_authoring_recommendation(self) -> None:
        self.entries[0]["required_hook_phrases"] = ["中国製AI"]
        self.write_config(self.entries)
        self.insert_parent(self.entries[0])

        selection = self.build()
        lane = selection["recommendation"]["lanes"][selector.ROTATION_LANE]

        self.assertEqual(lane["required_hook_phrases"], ["中国製AI"])
        self.assertIn(
            "Keep the exact phrase 「中国製AI」 in every opening hook variant.",
            lane["required_manual_checks"],
        )
        self.assertIn(
            "Every opening hook must retain the exact phrase: 「中国製AI」",
            selector.render_markdown(selection),
        )

    def test_invalid_required_hook_phrases_fail_config_loading(self) -> None:
        for invalid in ("中国製AI", None, [""], ["   "], [123]):
            with self.subTest(value=invalid):
                self.entries[0]["required_hook_phrases"] = invalid
                self.write_config(self.entries)
                with self.assertRaisesRegex(ValueError, "required_hook_phrases"):
                    selector.load_rotation_config(self.config, channel_id=CHANNEL)

    def test_config_is_the_only_candidate_source(self) -> None:
        for entry in self.entries:
            self.insert_parent(entry)
        self.insert_regular_scheduled(
            label="analytics-candidate",
            scheduled_at=self.as_of.replace(hour=13, minute=0) + timedelta(days=1),
        )

        selection = self.build()

        self.assertEqual(selection["policy_version"], selector.ROTATION_POLICY_VERSION)
        lane = selection["recommendation"]["lanes"][selector.ROTATION_LANE]
        self.assertEqual(lane["parent"]["content_hash"], self.entries[0]["content_hash"])
        self.assertEqual(selection["shortlists"]["published_parents"], [])
        self.assertEqual(selection["shortlists"]["scheduled_candidates"], [])
        self.assertFalse(selection["capacity"]["analytics_candidate_sourcing"])
        self.assertFalse(selection["capacity"]["scheduled_candidate_sourcing"])
        argv = lane["dry_run_argv"]
        self.assertIn("trial-add-from-published", argv)
        self.assertIn("--rotation-pool", argv)
        self.assertNotIn("trial-convert-scheduled", argv)
        self.assertNotIn("--apply", argv)

    def test_enabled_unpublished_entry_stays_in_pool_but_is_skipped(self) -> None:
        self.insert_parent(self.entries[0], status=reel_ledger.STATUS_SCHEDULED)
        for entry in self.entries[1:]:
            self.insert_parent(entry)

        selection = self.build()
        lane = selection["recommendation"]["lanes"][selector.ROTATION_LANE]
        first = selection["shortlists"]["rotation_pool"][0]

        self.assertTrue(first["enabled"])
        self.assertIn("POOL_PARENT_NOT_PUBLISHED", first["base_exclusions"])
        self.assertIn("POOL_PARENT_MISSING_MEDIA_ID", first["base_exclusions"])
        self.assertEqual(lane["parent"]["content_hash"], self.entries[1]["content_hash"])

    def test_published_trial_parent_requires_explicit_pool_permission(self) -> None:
        for entry in self.entries:
            self.insert_parent(entry, trial_reel=entry is self.entries[0])

        blocked = self.build()
        blocked_first = blocked["shortlists"]["rotation_pool"][0]
        self.assertIn("POOL_TRIAL_PARENT_NOT_ALLOWED", blocked_first["base_exclusions"])
        self.assertEqual(
            blocked["recommendation"]["lanes"][selector.ROTATION_LANE]["parent"][
                "content_hash"
            ],
            self.entries[1]["content_hash"],
        )

        self.entries[0]["allow_trial_parent"] = True
        self.write_config(self.entries)
        allowed = self.build()
        allowed_first = allowed["shortlists"]["rotation_pool"][0]
        self.assertTrue(allowed_first["ledger_trial_reel"])
        self.assertTrue(allowed_first["base_eligible"])
        self.assertEqual(
            allowed["recommendation"]["lanes"][selector.ROTATION_LANE]["parent"][
                "content_hash"
            ],
            self.entries[0]["content_hash"],
        )

    def test_rotation_continues_after_last_pool_parent(self) -> None:
        for entry in self.entries:
            self.insert_parent(entry)
        self.insert_pool_experiment(
            ordinal=1,
            parent=self.entries[0],
            launch=self.as_of - timedelta(days=4),
        )

        selection = self.build()
        lane = selection["recommendation"]["lanes"][selector.ROTATION_LANE]

        self.assertEqual(lane["parent"]["content_hash"], self.entries[1]["content_hash"])
        self.assertIn("-0002-A19-", lane["experiment_id"])

    def test_family_cooldown_skips_correlated_next_entry(self) -> None:
        self.entries[1]["source_video"] = self.entries[0]["source_video"]
        self.write_config(self.entries)
        for entry in self.entries:
            self.insert_parent(entry)
        self.insert_pool_experiment(
            ordinal=1,
            parent=self.entries[0],
            launch=self.as_of - timedelta(hours=5),
        )

        selection = self.build()
        lane = selection["recommendation"]["lanes"][selector.ROTATION_LANE]
        second = selection["shortlists"]["rotation_pool"][1]

        self.assertEqual(lane["parent"]["content_hash"], self.entries[2]["content_hash"])
        self.assertIn("ASSET_FAMILY_OBSERVATION_COOLDOWN", second["selection_exclusions"])

    def test_family_cooldown_is_half_open_at_exact_boundary(self) -> None:
        self.entries[1]["source_video"] = self.entries[0]["source_video"]
        self.write_config(self.entries)
        for entry in self.entries:
            self.insert_parent(entry)
        launch = self.as_of.replace(hour=19, minute=0)
        self.insert_pool_experiment(
            ordinal=1,
            parent=self.entries[0],
            launch=launch - timedelta(hours=72),
        )

        selection = self.build()
        lane = selection["recommendation"]["lanes"][selector.ROTATION_LANE]

        self.assertEqual(lane["scheduled_at"], launch.isoformat())
        self.assertEqual(lane["parent"]["content_hash"], self.entries[1]["content_hash"])

    def test_stopped_unpublished_attempt_does_not_rotate_or_cool_down(self) -> None:
        self.entries[1]["source_video"] = self.entries[0]["source_video"]
        self.write_config(self.entries)
        for entry in self.entries:
            self.insert_parent(entry)
        self.insert_pool_experiment(
            ordinal=1,
            parent=self.entries[0],
            launch=self.as_of - timedelta(days=4),
        )
        self.insert_pool_experiment(
            ordinal=2,
            parent=self.entries[1],
            launch=self.as_of.replace(hour=19, minute=0),
            state=reel_ledger.TRIAL_STATE_STOPPED,
            published=False,
        )

        selection = self.build()
        lane = selection["recommendation"]["lanes"][selector.ROTATION_LANE]

        self.assertEqual(lane["parent"]["content_hash"], self.entries[1]["content_hash"])
        self.assertIn("-0003-A19-", lane["experiment_id"])
        self.assertNotIn(
            "ASSET_FAMILY_OBSERVATION_COOLDOWN",
            selection["shortlists"]["rotation_pool"][1]["selection_exclusions"],
        )

    def test_nonterminal_trial_occupies_its_date(self) -> None:
        for entry in self.entries:
            self.insert_parent(entry)
        target = self.as_of.replace(hour=19, minute=0)
        self.insert_pool_experiment(
            ordinal=1,
            parent=self.entries[-1],
            launch=target,
            state=reel_ledger.TRIAL_STATE_SCHEDULED,
            published=False,
        )

        selection = self.build()

        self.assertEqual(
            selection["recommendation"]["target_date"],
            (target.date() + timedelta(days=1)).isoformat(),
        )
        self.assertIn("TRIAL_ALREADY_ON_DATE", selection["dates_considered"][0]["hold_reasons"])

    def test_scheduled_conversion_api_is_policy_disabled(self) -> None:
        result = selector.build_scheduled_conversion_selection(
            db_path=self.db,
            facebook_db=self.root / "facebook.db",
            channel_id=CHANNEL,
            as_of=self.as_of,
        )

        self.assertEqual(result["status"], "HOLD")
        self.assertEqual(result["dry_run_argv"], [])
        self.assertEqual(result["excluded"], {"FIXED_ROTATION_POOL_ONLY": 1})

    def test_config_validation_rejects_duplicates_and_channel_drift(self) -> None:
        self.write_config([self.entries[0], dict(self.entries[0])])
        with self.assertRaisesRegex(ValueError, "repeats content_hash"):
            selector.load_rotation_config(self.config, channel_id=CHANNEL)

        self.write_config(self.entries, channel_id="wrong-channel")
        with self.assertRaisesRegex(ValueError, "channel_id mismatch"):
            selector.load_rotation_config(self.config, channel_id=CHANNEL)

    def test_selection_is_deterministic_and_config_changes_fingerprint(self) -> None:
        for entry in self.entries:
            self.insert_parent(entry)
        first = self.build()
        second = self.build()
        self.assertEqual(first["recommendation"], second["recommendation"])
        self.assertEqual(
            first["provenance"]["input_fingerprint"],
            second["provenance"]["input_fingerprint"],
        )

        self.entries[-1]["enabled"] = False
        self.write_config(self.entries)
        changed = self.build()
        self.assertNotEqual(
            first["provenance"]["input_fingerprint"],
            changed["provenance"]["input_fingerprint"],
        )

    def test_markdown_names_only_the_rotation_pool(self) -> None:
        for entry in self.entries:
            self.insert_parent(entry)
        markdown = selector.render_markdown(self.build())

        self.assertIn("Trial rotation pool", markdown)
        self.assertIn("fixed config pool only", markdown)
        self.assertNotIn("Published-parent shortlist", markdown)
        self.assertNotIn("Scheduled shortlist", markdown)


if __name__ == "__main__":
    unittest.main()
