import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import reel_candidate_evaluator as evaluator
import scheduled_reel_evaluator as scheduled


class ScheduledReelEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "state" / "reels.db"
        self.db.parent.mkdir(parents=True)
        self.source = self.root / "outputs" / "source-video"
        self.clip_dir = self.source / "clips" / "001-scheduled"
        self.clip_dir.mkdir(parents=True)
        self.media = self.clip_dir / "reel.ja.aibrief_jp.mp4"
        self.media.write_bytes(b"fixture")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "source_url": (
                        "https://www.youtube.com/watch?v=source-video"
                    ),
                    "source_title": "Source title",
                    "source_uploader": "Source uploader",
                }
            ),
            encoding="utf-8",
        )
        self.scheduled_clip = {
            "index": 1,
            "slug": "001-scheduled",
            "start": 10,
            "end": 40,
            "duration": 30,
            "score": 9,
            "hook_score": 9,
            "value_score": 9,
            "one_liner": "A concrete AI workflow with no measured analogue.",
            "hook_variants": [],
            "reason": "A concrete workflow.",
            "source_chapter": "Workflow",
            "transcript": "This is a concrete AI workflow with a clear payoff.",
            "opening_assessment": {},
        }
        unscheduled_clip = {
            **self.scheduled_clip,
            "index": 2,
            "slug": "002-unscheduled",
            "one_liner": "This sibling must not be evaluated.",
        }
        (self.source / "candidates.json").write_text(
            json.dumps(
                {
                    "selection_mode": "ai",
                    "clips": [self.scheduled_clip, unscheduled_clip],
                }
            ),
            encoding="utf-8",
        )
        (self.source / "metadata.json").write_text(
            json.dumps(
                {
                    "title": "Source title",
                    "uploader": "Source uploader",
                    "webpage_url": (
                        "https://www.youtube.com/watch?v=source-video"
                    ),
                }
            ),
            encoding="utf-8",
        )
        (self.clip_dir / "notes.json").write_text(
            json.dumps(self.scheduled_clip),
            encoding="utf-8",
        )
        self._create_database()
        self.config = evaluator.load_config()
        self.winner_library = {
            "library_metadata": {
                "schema_version": 1,
                "generated_at": "2026-07-30T00:00:00+00:00",
                "account": "aibrief_jp",
                "platform": "instagram",
                "maturity_window": "24h",
            },
            "winners": [],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _create_database(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                CREATE TABLE reels (
                    content_hash TEXT,
                    channel_id TEXT,
                    lang TEXT,
                    clip_dir TEXT,
                    media_path TEXT,
                    source_video TEXT,
                    title TEXT,
                    caption TEXT,
                    status TEXT,
                    scheduled_at TEXT,
                    published_at TEXT,
                    media_id TEXT,
                    permalink TEXT,
                    manifest_path TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    trial_reel INTEGER,
                    trial_graduation_strategy TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE trial_experiments (
                    experiment_id TEXT,
                    content_hash TEXT,
                    channel_id TEXT,
                    case_type TEXT,
                    parent_content_hash TEXT,
                    parent_media_id TEXT,
                    asset_family_id TEXT,
                    baseline_hook TEXT,
                    variant_hook TEXT,
                    changed_variables_json TEXT,
                    state TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO reels VALUES (
                    'hash-scheduled', 'aibrief_jp', 'ja', ?, ?,
                    'source-video', '予定フック', '予定フック\n本文',
                    'scheduled', '2026-08-01T09:00:00+09:00',
                    NULL, NULL, NULL, ?, '2026-07-01T00:00:00+00:00',
                    '2026-07-01T00:00:00+00:00', 0, NULL
                )
                """,
                (str(self.clip_dir), str(self.media), str(self.manifest)),
            )

    def _build(self):
        rows = scheduled.load_scheduled_rows(
            self.db,
            channel_id="aibrief_jp",
            statuses=["scheduled"],
        )
        sources, audit = scheduled.normalize_scheduled_sources(
            rows,
            self.config,
            db_path=self.db,
        )
        report = evaluator.build_candidate_evaluation_from_sources(
            sources,
            self.winner_library,
            self.config,
        )
        return scheduled.apply_schedule_triage(
            report,
            input_audit=audit,
        )

    def test_filters_unscheduled_sibling_before_evaluation_and_is_read_only(self):
        before = self.db.read_bytes()
        report = self._build()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(report["summary"]["candidates"], 1)
        self.assertEqual(
            report["scheduled_pipeline"]["input_audit"]["coverage"][
                "candidate_exact_match"
            ]["count"],
            1,
        )
        candidate = report["sources"][0]["evaluations"][0]["candidate"]
        self.assertEqual(
            candidate["candidate_id"],
            "scheduled:aibrief_jp:hash-scheduled",
        )
        self.assertEqual(candidate["schedule"]["clip_slug"], "001-scheduled")
        self.assertNotIn(
            "This sibling must not be evaluated",
            scheduled.render_scheduled_markdown(report),
        )

    def test_missing_opening_score_routes_to_rescore_not_remove(self):
        report = self._build()
        row = report["scheduled_pipeline"]["rows"][0]
        self.assertEqual(row["action"], scheduled.ACTION_RESCORE)
        self.assertEqual(report["summary"]["safe_automatic_removals"], 0)
        rendered_json = evaluator.render_candidate_evaluation_json(report)
        rendered_markdown = scheduled.render_scheduled_markdown(report)
        rendered_csv = scheduled.render_scheduled_csv(report)
        for output in (rendered_json, rendered_markdown, rendered_csv):
            self.assertNotIn("NaN", output)
            self.assertNotIn("Infinity", output)

    def test_existing_trial_is_locked_as_existing_experiment(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "UPDATE reels SET trial_reel=1 WHERE content_hash='hash-scheduled'"
            )
            connection.execute(
                """
                INSERT INTO trial_experiments VALUES (
                    'TRIAL-1', 'hash-scheduled', 'aibrief_jp',
                    'successful_post_variant', 'parent-hash', 'parent-media',
                    'source-video', 'baseline', 'variant',
                    '["overlay_hook"]', 'scheduled'
                )
                """
            )
        report = self._build()
        row = report["scheduled_pipeline"]["rows"][0]
        self.assertEqual(row["action"], scheduled.ACTION_KEEP_TRIAL)
        self.assertEqual(
            row["experiment"]["changed_variables"],
            ["overlay_hook"],
        )

    def test_llm_preparation_uses_only_actual_scheduled_hook(self):
        rows = scheduled.load_scheduled_rows(
            self.db,
            channel_id="aibrief_jp",
            statuses=["scheduled"],
        )
        sources, _ = scheduled.normalize_scheduled_sources(
            rows,
            self.config,
            db_path=self.db,
        )
        prepared = scheduled.prepare_scheduled_sources_for_llm(sources)
        candidate = prepared[0]["candidates"][0]
        self.assertEqual(candidate["hook"], "予定フック")
        self.assertEqual(
            candidate["source_selection_hook"],
            "A concrete AI workflow with no measured analogue.",
        )
        self.assertEqual(
            prepared[0]["input_scope"],
            "scheduled_pipeline_exact_clips",
        )

    def test_llm_preparation_uses_registered_trial_overlay_not_baseline_caption(self):
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                UPDATE reels
                   SET title='実際のTrialオーバーレイ',
                       caption='ベースラインのキャプション\n本文',
                       trial_reel=1
                 WHERE content_hash='hash-scheduled'
                """
            )
            connection.execute(
                """
                INSERT INTO trial_experiments VALUES (
                    'TRIAL-OVERLAY', 'hash-scheduled', 'aibrief_jp',
                    'successful_post_variant', 'parent-hash', 'parent-media',
                    'source-video', 'ベースラインのキャプション',
                    '実際のTrialオーバーレイ', '["overlay_hook"]', 'scheduled'
                )
                """
            )
        rows = scheduled.load_scheduled_rows(
            self.db,
            channel_id="aibrief_jp",
            statuses=["scheduled"],
        )
        sources, _ = scheduled.normalize_scheduled_sources(
            rows,
            self.config,
            db_path=self.db,
        )
        prepared = scheduled.prepare_scheduled_sources_for_llm(sources)
        candidate = prepared[0]["candidates"][0]
        self.assertEqual(candidate["hook"], "実際のTrialオーバーレイ")
        self.assertEqual(candidate["schedule"]["title"], "実際のTrialオーバーレイ")
        self.assertEqual(
            candidate["schedule"]["caption_hook"],
            "ベースラインのキャプション",
        )
        self.assertEqual(
            candidate["schedule"]["experiment"]["variant_hook"],
            "実際のTrialオーバーレイ",
        )


if __name__ == "__main__":
    unittest.main()
