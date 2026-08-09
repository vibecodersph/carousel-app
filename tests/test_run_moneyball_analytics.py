import csv
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import reel_ledger
from scripts import run_moneyball_analytics


ROOT = Path(__file__).resolve().parents[1]


class RunMoneyballAnalyticsIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "state" / "reels.db"
        self.facebook_db = self.root / "state" / "facebook.db"
        self.config = self.root / "config" / "moneyball_analytics.json"
        self.annotations = self.root / "data" / "reel_annotations.json"
        self.markdown_out = self.root / "out" / "reel_report.moneyball.md"
        self.html_out = self.root / "out" / "reel_report.moneyball.html"
        self.json_out = self.root / "out" / "reel_report.moneyball.json"
        self.winner_markdown_out = (
            self.root / "out" / "reel_report.moneyball.winner_library.md"
        )
        self.winner_json_out = (
            self.root / "out" / "reel_report.moneyball.winner_library.json"
        )
        self.csv_out = self.root / "out" / "reel_report.moneyball.csv"
        self.facebook_csv_out = (
            self.root / "out" / "reel_report.moneyball.facebook.csv"
        )
        self.audit_out = self.root / "out" / "moneyball_data_audit.md"

        self.config.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / "config" / "moneyball_analytics.json", self.config)
        self.annotations.parent.mkdir(parents=True)
        self.annotations.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "annotations": [
                        {
                            "account": "aibrief_jp",
                            "media_id": "m-full",
                            "series": "utility-lab",
                            "content_goal": "utility",
                            "production_minutes": 30,
                            "direct_cost_jpy": 500,
                            "metadata_source": "manual",
                            "metadata_confidence": "high",
                        },
                        {
                            "account": "aibrief_jp",
                            "media_id": "m-late",
                            "series": "utility-lab",
                            "content_goal": "utility",
                            "metadata_source": "manual",
                            "metadata_confidence": "high",
                        },
                    ],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self._seed_ledger()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed_ledger(self) -> None:
        full_clip = self.root / "artifacts" / "full" / "clip_001"
        late_clip = self.root / "artifacts" / "late" / "clip_001"
        for clip_dir, duration in ((full_clip, 10), (late_clip, 20)):
            clip_dir.mkdir(parents=True)
            (clip_dir / "notes.json").write_text(
                json.dumps({"duration": duration}) + "\n",
                encoding="utf-8",
            )

        with reel_ledger.connect(self.db) as conn:
            reel_ledger.upsert_imported(
                conn,
                content_hash="hash-full",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir=full_clip,
                media_path=full_clip / "reel.ja.aibrief_jp.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                source_video="source-full",
                title="A complete maturity curve",
                published_at="2026-07-01T00:00:00+00:00",
                media_id="m-full",
                permalink="https://www.instagram.com/reel/m-full/",
            )
            reel_ledger.upsert_imported(
                conn,
                content_hash="hash-late",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir=late_clip,
                media_path=late_clip / "reel.ja.aibrief_jp.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                source_video="source-late",
                title="A lifetime-only observation",
                published_at="2026-07-02T00:00:00+00:00",
                media_id="m-late",
                permalink="https://www.instagram.com/reel/m-late/",
            )
            conn.execute(
                "UPDATE reels SET caption=? WHERE content_hash=? AND channel_id=?",
                ("Full curve caption", "hash-full", "aibrief_jp"),
            )
            conn.execute(
                "UPDATE reels SET caption=? WHERE content_hash=? AND channel_id=?",
                ("Late-only caption", "hash-late", "aibrief_jp"),
            )

            full_observations = (
                ("2026-07-01T02:15:00+00:00", 100, 80, 1),
                ("2026-07-02T00:30:00+00:00", 240, 200, 3),
                ("2026-07-04T00:15:00+00:00", 410, 350, 5),
                ("2026-07-08T00:30:00+00:00", 600, 500, 8),
                ("2026-07-09T08:00:00+00:00", 700, 575, 9),
            )
            for captured_at, views, reach, follows in full_observations:
                inserted = reel_ledger.record_insight(
                    conn,
                    content_hash="hash-full",
                    channel_id="aibrief_jp",
                    media_id="m-full",
                    captured_at=captured_at,
                    metrics={
                        "views": views,
                        "reach": reach,
                        "likes": max(1, views // 20),
                        "comments": 1,
                        "saved": max(1, views // 40),
                        "shares": max(1, views // 50),
                        "total_interactions": max(4, views // 10),
                        "ig_reels_video_view_total_time": views * 12_000,
                        "ig_reels_avg_watch_time": 12_000,
                        "follows": follows,
                    },
                )
                self.assertTrue(inserted)

            # This observation is 200 hours old. It is a valid latest lifetime
            # total but is outside every configured fixed-window tolerance.
            self.assertTrue(
                reel_ledger.record_insight(
                    conn,
                    content_hash="hash-late",
                    channel_id="aibrief_jp",
                    media_id="m-late",
                    captured_at="2026-07-10T08:00:00+00:00",
                    metrics={
                        "views": 900,
                        "reach": 700,
                        "likes": 20,
                        "comments": 0,
                        "saved": 5,
                        "shares": 2,
                        "total_interactions": 27,
                        "ig_reels_video_view_total_time": 5_400_000,
                        "ig_reels_avg_watch_time": 6_000,
                    },
                )
            )

    def _run(self, *extra_arguments: str) -> None:
        result = run_moneyball_analytics.main(
            [
                "--channel",
                "aibrief_jp",
                "--db",
                str(self.db),
                "--config",
                str(self.config),
                "--annotations",
                str(self.annotations),
                "--markdown-out",
                str(self.markdown_out),
                "--html-out",
                str(self.html_out),
                "--json-out",
                str(self.json_out),
                "--csv-out",
                str(self.csv_out),
                "--audit-out",
                str(self.audit_out),
                "--generated-at",
                "2026-07-20T00:00:00+00:00",
                *extra_arguments,
            ]
        )
        self.assertEqual(result, 0)

    def _seed_facebook_ledger(self) -> None:
        facebook_clip = self.root / "artifacts" / "facebook" / "clip_001"
        facebook_clip.mkdir(parents=True)
        (facebook_clip / "notes.json").write_text(
            json.dumps({"duration": 10}) + "\n",
            encoding="utf-8",
        )
        with reel_ledger.connect(self.facebook_db) as conn:
            reel_ledger.upsert_imported(
                conn,
                content_hash="hash-full",
                channel_id="aibrief_jp",
                lang="ja",
                clip_dir=facebook_clip,
                media_path=facebook_clip / "reel.ja.aibrief_jp.mp4",
                status=reel_ledger.STATUS_PUBLISHED,
                source_video="source-full",
                title="Independent Facebook upload",
                published_at="2026-07-01T01:00:00+00:00",
                media_id="fb-full",
                permalink="/reel/fb-full/",
            )
            fallback_metrics = {
                "views": 180,
                "likes": 9,
                "comments": 2,
            }
            self.assertTrue(
                reel_ledger.record_insight(
                    conn,
                    content_hash="hash-full",
                    channel_id="aibrief_jp",
                    media_id="fb-full",
                    captured_at="2026-07-02T01:30:00+00:00",
                    metrics=fallback_metrics,
                    raw=json.dumps(
                        {
                            "data": [
                                {
                                    "name": name,
                                    "period": "lifetime",
                                    "values": [{"value": value}],
                                }
                                for name, value in fallback_metrics.items()
                            ]
                        },
                        ensure_ascii=False,
                    ),
                )
            )

    def test_cli_outputs_are_complete_age_matched_and_deterministic(self) -> None:
        annotations_before = self.annotations.read_bytes()
        self._run()

        output_paths = (
            self.markdown_out,
            self.html_out,
            self.json_out,
            self.winner_markdown_out,
            self.winner_json_out,
            self.csv_out,
            self.audit_out,
        )
        for path in output_paths:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertGreater(path.stat().st_size, 0)

        report = json.loads(self.json_out.read_text(encoding="utf-8"))
        winner_library = json.loads(
            self.winner_json_out.read_text(encoding="utf-8")
        )
        html_output = self.html_out.read_text(encoding="utf-8")
        self.assertIn('data-testid="moneyball-dashboard"', html_output)
        self.assertIn('id="account-growth-kpis"', html_output)
        self.assertIn('id="attribution-warning"', html_output)
        required_sections = {
            "report_metadata",
            "data_coverage",
            "account_summary",
            "account_growth",
            "maturity_windows",
            "account_baselines",
            "posts",
            "series",
            "experiments",
            "classifications",
            "funnel_diagnostics",
            "recommendations",
            "data_gaps",
        }
        self.assertTrue(required_sections.issubset(report))
        self.assertEqual(report["report_metadata"]["generated_at"], "2026-07-20T00:00:00+00:00")
        self.assertEqual(report["data_coverage"]["published_posts"], 2)
        self.assertEqual(
            winner_library["library_metadata"]["maturity_window"],
            "24h",
        )
        self.assertEqual(
            winner_library["library_metadata"]["unique_winner_posts"],
            1,
        )
        self.assertIn(
            "How to judge new candidates",
            self.winner_markdown_out.read_text(encoding="utf-8"),
        )

        by_media_id = {
            post["identity"]["media_id"]: post for post in report["posts"]
        }
        complete = by_media_id["m-full"]["maturity_windows"]
        self.assertTrue(
            all(complete[window] is not None for window in ("2h", "24h", "72h", "7d", "latest"))
        )
        self.assertAlmostEqual(complete["2h"]["actual_age_hours"], 2.25)
        self.assertAlmostEqual(
            complete["2h"]["derived_metrics"]["watch_depth"],
            1.2,
        )

        late = by_media_id["m-late"]["maturity_windows"]
        for window in ("2h", "24h", "72h", "7d"):
            with self.subTest(missed_window=window):
                self.assertIsNone(late[window])
        self.assertIsNotNone(late["latest"])
        self.assertAlmostEqual(late["latest"]["actual_age_hours"], 200.0)
        self.assertIsNone(
            by_media_id["m-late"]["growth_curve_metrics"][
                "reach_delta_2h_to_24h"
            ]
        )
        self.assertEqual(
            report["data_coverage"]["snapshot_maturity"]["24h"]["count"],
            1,
        )

        csv_rows = list(
            csv.DictReader(io.StringIO(self.csv_out.read_text(encoding="utf-8")))
        )
        available_observations = {
            (
                post["identity"]["content_hash"],
                window,
            )
            for post in report["posts"]
            for window, observation in post["maturity_windows"].items()
            if observation is not None
        }
        csv_observations = {
            (row["content_hash"], row["maturity_window"]) for row in csv_rows
        }
        self.assertEqual(len(csv_rows), len(available_observations))
        self.assertEqual(csv_observations, available_observations)
        self.assertEqual(
            len(csv_observations),
            len(csv_rows),
            "each available post/window must have exactly one CSV row",
        )

        first_bytes = {path: path.read_bytes() for path in output_paths}
        self._run()
        for path in output_paths:
            with self.subTest(deterministic=path.name):
                self.assertEqual(path.read_bytes(), first_bytes[path])

        self.assertEqual(self.annotations.read_bytes(), annotations_before)
        for path in output_paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(finite_output=path.name):
                self.assertNotIn("NaN", text)
                self.assertNotIn("Infinity", text)

    def test_cli_writes_a_separate_facebook_csv_without_mixing_platform_rows(self) -> None:
        self._seed_facebook_ledger()
        self._run(
            "--facebook-db",
            str(self.facebook_db),
            "--facebook-csv-out",
            str(self.facebook_csv_out),
        )

        self.assertTrue(self.facebook_csv_out.is_file())
        facebook_rows = list(
            csv.DictReader(
                io.StringIO(self.facebook_csv_out.read_text(encoding="utf-8"))
            )
        )
        self.assertEqual(
            {row["maturity_window"] for row in facebook_rows},
            {"24h", "latest"},
        )
        self.assertTrue(all(row["platform"] == "facebook" for row in facebook_rows))
        self.assertTrue(all(row["media_id"] == "fb-full" for row in facebook_rows))
        self.assertTrue(
            all(row["paired_instagram_media_id"] == "m-full" for row in facebook_rows)
        )

        instagram_rows = list(
            csv.DictReader(io.StringIO(self.csv_out.read_text(encoding="utf-8")))
        )
        self.assertTrue(
            all(row["platform"] == "instagram" for row in instagram_rows)
        )
        self.assertNotIn("fb-full", self.csv_out.read_text(encoding="utf-8"))

        report = json.loads(self.json_out.read_text(encoding="utf-8"))
        facebook = report["platform_analytics"]["facebook"]
        self.assertEqual(facebook["status"], "AVAILABLE")
        self.assertEqual(facebook["data_coverage"]["published_posts"], 1)
        self.assertEqual(facebook["maturity_windows"]["24h"]["post_count"], 1)
        html_output = self.html_out.read_text(encoding="utf-8")
        self.assertIn('id="facebook-per-reel-table"', html_output)
        self.assertIn(
            'href="https://www.facebook.com/reel/fb-full/"',
            html_output,
        )
        for path in (
            self.facebook_csv_out,
            self.csv_out,
            self.json_out,
            self.html_out,
        ):
            text = path.read_text(encoding="utf-8")
            with self.subTest(finite_output=path.name):
                self.assertNotIn("NaN", text)
                self.assertNotIn("Infinity", text)


if __name__ == "__main__":
    unittest.main()
