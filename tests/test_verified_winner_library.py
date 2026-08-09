import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import moneyball_analytics as moneyball
import verified_winner_library as winners


def ass_script(lines):
    events = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for index, text in enumerate(lines):
        events.append(
            f"Dialogue: 0,0:00:0{index}.00,0:00:0{index + 1}.00,"
            f"Default,,0,0,0,,{text}"
        )
    return "\n".join(events) + "\n"


def metric_bucket(specification, rows):
    return {
        "label": specification["label"],
        "short_label": specification["short_label"],
        "source": specification["source"],
        "direction": specification["direction"],
        "format": specification["format"],
        "coverage": {"count": 2, "total": 2, "percentage": 100.0},
        "top_10": rows,
    }


def ranking_row(media_id, value, rank=1, supporting_metrics=None):
    return {
        "rank": rank,
        "media_id": media_id,
        "permalink": f"https://www.instagram.com/reel/{media_id}/",
        "published_at": "2026-07-01T00:00:00+00:00",
        "actual_age_hours": 24.5,
        "title": f"Mutable generated title {media_id}",
        "series": None,
        "value": value,
        "directional_percentile": 90.0,
        "supporting_metrics": supporting_metrics or {},
    }


def post(
    *,
    media_id,
    content_hash,
    clip_dir,
    caption,
    reach=200,
    interactions=10,
    saves=5,
):
    return {
        "identity": {
            "media_id": media_id,
            "permalink": f"https://www.instagram.com/reel/{media_id}/",
            "published_at": "2026-07-01T00:00:00+00:00",
            "content_hash": content_hash,
            "caption": caption,
        },
        "content_metadata": {
            "hook_text": f"Mutable generated hook {media_id}",
            "source": f"https://www.youtube.com/watch?v=source-{media_id}",
        },
        "generation_artifact": {
            "clip_dir": str(clip_dir),
            "notes_path": str(clip_dir / "notes.json"),
            "manifest_path": "",
            "source_title": f"Source {media_id}",
            "source_uploader": "Fixture uploader",
        },
        "maturity_windows": {
            "24h": {
                "actual_age_hours": 24.5,
                "raw_metrics": {
                    "views": 260,
                    "reach": reach,
                    "interactions": interactions,
                    "saves": saves,
                    "reels_skip_rate": 40.0,
                    "duration_seconds": 20.0,
                },
                "derived_metrics": {
                    "engagement_rate_by_reach": interactions / reach,
                    "watch_depth": 0.5,
                    "saves_per_1000_reach": saves / reach * 1000,
                    "views_per_reached_account": 260 / reach,
                    "average_watch_time_seconds": 10.0,
                },
            }
        },
    }


class VerifiedWinnerLibraryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.direct = self.root / "source-one" / "clips" / "001-direct"
        self.direct.mkdir(parents=True)
        (self.direct / "subtitles.ja.ass").write_text(
            ass_script(["公開された冒頭フック", "具体的な仕組みを説明します"]),
            encoding="utf-8",
        )
        (self.direct / "subtitles.en.ass").write_text(
            ass_script(["Published opening", "Here is the mechanism"]),
            encoding="utf-8",
        )
        (self.direct / "notes.json").write_text(
            json.dumps(
                {
                    "duration": 20,
                    "one_liner": "Current source one-liner",
                    "transcript": "Original source transcript.",
                    "hook_variants": ["Source option A", "Source option B"],
                }
            ),
            encoding="utf-8",
        )
        (self.direct / "one_liners.json").write_text(
            json.dumps({"ja": "Mutable localized option"}, ensure_ascii=False),
            encoding="utf-8",
        )
        media_bytes = b"published-fixture"
        (self.direct / "reel.ja.aibrief_jp.mp4").write_bytes(media_bytes)
        self.direct_hash = hashlib.sha256(media_bytes).hexdigest()

        stale_parent = self.root / "source-two" / "clips"
        stale_parent.mkdir(parents=True)
        self.stale = stale_parent / "002-old-slug"
        replacement = stale_parent / "002-renamed-slug"
        replacement.mkdir()
        (replacement / "subtitles.ja.ass").write_text(
            ass_script(["名前変更後も根拠付きの台本です"]),
            encoding="utf-8",
        )
        (replacement / "notes.json").write_text(
            json.dumps({"transcript": "Recovered original transcript."}),
            encoding="utf-8",
        )

        self.posts = [
            post(
                media_id="m-balanced",
                content_hash=self.direct_hash,
                clip_dir=self.direct,
                caption="実際に公開されたフック\n\n本文",
            ),
            post(
                media_id="m-specialist",
                content_hash="missing-published-hash",
                clip_dir=self.stale,
                caption="専門指標の公開フック\n\n本文",
                reach=80,
                interactions=2,
                saves=1,
            ),
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def report(self):
        metric_rankings = {}
        for specification in moneyball.PERFORMANCE_RANKING_METRICS:
            key = specification["key"]
            value = {
                "total_interactions_per_reach": 0.05,
                "watch_depth": 0.5,
                "three_second_skip_rate": 40.0,
                "saves_per_1000_reach": 25.0,
                "views_per_reached_account": 1.3,
            }[key]
            support = {
                "total_interactions_per_reach": {
                    "interactions": 10,
                    "reach": 200,
                    "denominator_type": "reach",
                },
                "watch_depth": {
                    "average_watch_time_seconds": 10,
                    "duration_seconds": 20,
                },
                "three_second_skip_rate": {"reels_skip_rate": 40},
                "saves_per_1000_reach": {
                    "saves": 5,
                    "reach": 200,
                    "denominator_type": "reach",
                },
                "views_per_reached_account": {
                    "views": 260,
                    "reach": 200,
                    "denominator_type": "reach",
                },
            }[key]
            rows = [
                ranking_row(
                    "m-balanced",
                    value,
                    supporting_metrics=support,
                )
            ]
            if key == "views_per_reached_account":
                rows.append(
                    ranking_row(
                        "m-specialist",
                        3.25,
                        rank=2,
                        supporting_metrics={
                            "views": 260,
                            "reach": 80,
                            "denominator_type": "reach",
                        },
                    )
                )
            metric_rankings[key] = metric_bucket(specification, rows)

        aggregate = {
            "media_id": "m-balanced",
            "rank": 1,
            "actual_age_hours": 24.5,
            "average_directional_percentile": 95.0,
            "components": {},
            "strong_points": [],
        }
        return {
            "report_metadata": {
                "account": "aibrief_jp",
                "generated_at": "2026-07-30T00:00:00+00:00",
                "generated_at_jst": "2026-07-30T09:00:00+09:00",
            },
            "performance_rankings": {
                "instagram": {
                    "maturity_window": "24h",
                    "cohort_size": 2,
                    "methodology": {"eligible_post_count": 1},
                    "metric_rankings": metric_rankings,
                    "aggregate_top_10": [aggregate],
                }
            },
            "posts": self.posts,
        }

    def test_builds_union_and_preserves_published_hook_precedence(self):
        library = winners.build_winner_library(self.report())
        self.assertEqual(library["library_metadata"]["unique_winner_posts"], 2)
        self.assertEqual(library["library_metadata"]["ranking_placement_count"], 7)
        by_id = {
            row["identity"]["media_id"]: row for row in library["winners"]
        }
        balanced = by_id["m-balanced"]
        self.assertEqual(
            balanced["content"]["published_hook"]["value"],
            "実際に公開されたフック",
        )
        self.assertEqual(
            balanced["content"]["published_hook"]["source"],
            "published_caption_first_line",
        )
        self.assertNotEqual(
            balanced["content"]["published_hook"]["value"],
            "Mutable localized option",
        )
        self.assertEqual(
            balanced["winner_evidence"]["tier"],
            "BALANCED_REFERENCE",
        )
        self.assertEqual(
            balanced["winner_evidence"]["independent_family_count"],
            2,
        )
        self.assertEqual(
            balanced["asset_provenance"]["published_asset"]["status"],
            "VERIFIED",
        )

    def test_recovers_unique_same_index_script_without_overstating_confidence(self):
        library = winners.build_winner_library(self.report())
        specialist = next(
            row
            for row in library["winners"]
            if row["identity"]["media_id"] == "m-specialist"
        )
        resolution = specialist["asset_provenance"]["clip_resolution"]
        self.assertEqual(
            resolution["resolution_method"],
            "source_video_clip_index_unique",
        )
        self.assertEqual(resolution["confidence"], "medium")
        self.assertEqual(
            specialist["content"]["japanese_script"]["status"],
            "AVAILABLE",
        )
        self.assertIn(
            "TRANSCRIPT_MEDIUM_CONFIDENCE",
            specialist["evidence_flags"],
        )
        self.assertIn("LOW_BASE_REACH", specialist["evidence_flags"])

    def test_markdown_and_json_are_complete_and_finite(self):
        library = winners.build_winner_library(self.report())
        markdown = winners.render_winner_library_markdown(library)
        json_text = winners.render_winner_library_json(library)
        self.assertIn(
            "https://www.instagram.com/reel/m-balanced/",
            markdown,
        )
        self.assertIn("実際に公開されたフック", markdown)
        self.assertIn("具体的な仕組みを説明します", markdown)
        self.assertIn("ADVANCE AS REPLICATION TEST", markdown)
        self.assertIn("Original source transcript.", markdown)
        self.assertNotIn("NaN", json_text)
        self.assertNotIn("Infinity", json_text)
        decoded = json.loads(json_text)
        self.assertEqual(
            decoded["data_coverage"]["japanese_scripts"]["count"],
            2,
        )


if __name__ == "__main__":
    unittest.main()
