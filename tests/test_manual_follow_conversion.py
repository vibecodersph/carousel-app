import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path

import manual_follow_conversion as manual


ROOT = Path(__file__).resolve().parents[1]


def observation(creative_id="one", **changes):
    return {
        "creative_id": creative_id, "creative": f"Creative {creative_id}",
        "recorded_at": "2026-09-07", "observed_at": None,
        "viewers": 1000, "follows": 10, "user_reported_rate_percent": "1.0",
        "watch_seconds": None, "attribution_scope": "post_level",
        "boost_status": "UNKNOWN", "identity_status": "UNRESOLVED",
        "media_id": None, "permalink": None,
    } | changes


class ManualFollowConversionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "manual.json"

    def build(self, *rows):
        source = {
            "schema_version": 1, "account": "aibrief_jp", "platform": "instagram",
            "observations": list(rows),
        }
        self.path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        result = manual.build_manual_follow_conversion(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(result["observations"], list(rows))
        return result

    def test_supplied_eleven_rows_preserve_values_and_unknown_dates(self):
        result = manual.build_manual_follow_conversion(ROOT / "data/aibrief_jp_manual_follow_conversion.json")
        self.assertEqual(result["observation_count"], 11)
        self.assertEqual(result["creative_count"], 11)
        self.assertEqual(result["exact_rate_count"], 10)
        self.assertEqual(result["unknown_observed_at_count"], 11)
        rows = {row["creative_id"]: row for row in result["rows"]}
        expected = {
            "tiananmen-ai": (1541, 73, "4.74", 22),
            "claude-code-author": (917, 4, "0.44", 11),
            "jit-planning": (1619, 7, "0.432", 13),
            "keep-prototype": (2337, 9, "0.385", 14),
            "hoodie-prompt-clear": (5500, 18, "0.33", None),
            "reddit-five-answers": (3232, 8, "0.247", 8),
            "applescript": (3686, 8, "0.217", 11),
            "ai-productivity-fourfold": (4632, 10, "0.216", 21),
            "netflix-black-box": (10339, 17, "0.164", 15),
            "mit-new-test": (28911, 14, "0.048", 11),
            "ai-2024-geometry": ("~20000", "<9", "<0.045", None),
        }
        for creative_id, values in expected.items():
            row = rows[creative_id]
            self.assertEqual(tuple(row[key] for key in (
                "viewers", "follows", "user_reported_rate_percent", "watch_seconds"
            )), values)
            self.assertIsNone(row["observed_at"])
            self.assertEqual(row["recorded_at"], "2026-09-07")
        self.assertIn("LOW_COUNT", rows["claude-code-author"]["flags"])
        self.assertNotIn("LOW_COUNT", rows["hoodie-prompt-clear"]["flags"])
        self.assertEqual(rows["tiananmen-ai"]["boost_status"], "UNVERIFIED")
        self.assertEqual(rows["netflix-black-box"]["boost_status"], "UNBOOSTED")
        self.assertAlmostEqual(rows["tiananmen-ai"]["calculated_rate_percent"], 100 * 73 / 1541)
        self.assertEqual(result["rows"][0]["creative_id"], "tiananmen-ai")
        self.assertIsNone(rows["ai-2024-geometry"]["group_rank"])

    def test_exact_ratios_rank_with_competition_ties_within_groups(self):
        result = self.build(
            observation("half", follows=10, viewers=2000),
            observation("equal-one", follows=5, viewers=500),
            observation("equal-two", follows=10, viewers=1000),
            observation("trial", follows=100, attribution_scope="trial"),
            observation("unboosted", follows=100, boost_status="UNBOOSTED"),
            observation("unverified", follows=100, boost_status="UNVERIFIED"),
        )
        rows = {row["creative_id"]: row for row in result["rows"]}
        self.assertEqual([rows[key]["group_rank"] for key in ("equal-one", "equal-two", "half")], [1, 1, 3])
        self.assertEqual(rows["half"]["group_exact_count"], 3)
        self.assertEqual(rows["trial"]["group_exact_count"], 1)
        self.assertEqual(len(result["groups"]), 4)
        self.assertIsNone(result["methodology"]["overall_rank"])
        self.assertTrue(all("rank" not in row for row in result["rows"]))

    def test_approximate_bounded_missing_and_zero_counts_are_never_exact_ranked(self):
        result = self.build(
            observation("approx", viewers="~20000", follows="<9", user_reported_rate_percent="<0.045"),
            observation("bounded-only", follows="<9"),
            observation("missing-denominator", viewers=None),
            observation("zero-denominator", viewers=0),
            observation("missing-follows", follows=None),
        )
        self.assertEqual(result["exact_rate_count"], 0)
        for row in result["rows"]:
            self.assertIsNone(row["group_rank"])
            self.assertIsNone(row["calculated_rate_percent"])
        rows = {row["creative_id"]: row for row in result["rows"]}
        self.assertEqual(rows["approx"]["viewers"], "~20000")
        self.assertEqual(rows["approx"]["follows"], "<9")
        self.assertNotIn("LOW_COUNT", rows["approx"]["flags"])
        self.assertIn("MISSING_DENOMINATOR", rows["missing-denominator"]["flags"])
        self.assertIn("ZERO_DENOMINATOR", rows["zero-denominator"]["flags"])
        self.assertIn("MISSING_FOLLOWS", rows["missing-follows"]["flags"])

    def test_latest_recorded_snapshot_selected_without_overwriting_older_entries(self):
        oldest = observation(follows=2, recorded_at="2026-09-06")
        newest = observation(follows=12, recorded_at="2026-09-08", observed_at="2026-09-07")
        late_in_file = observation(follows=5, recorded_at="2026-09-07")
        result = self.build(oldest, newest, late_in_file)
        self.assertEqual(result["rows"][0]["follows"], 12)
        self.assertEqual(result["rows"][0]["observed_at"], "2026-09-07")
        self.assertEqual(result["observation_count"], 3)
        self.assertEqual(result["creative_count"], 1)
        self.assertEqual(result["latest_recorded_at"], "2026-09-08")
        self.assertEqual(result["observations"][0]["follows"], 2)
        final_correction = copy.deepcopy(newest)
        final_correction["follows"] = 13
        repeated = self.build(oldest, newest, late_in_file, final_correction)
        self.assertEqual(repeated["rows"][0]["follows"], 13)
        self.assertEqual(repeated["observation_count"], 4)

    def test_unknown_watch_and_unresolved_identity_are_not_invented(self):
        result = self.build(observation(permalink="https://example.com/unverified"))
        row = result["rows"][0]
        self.assertIsNone(row["watch_seconds"])
        self.assertIsNone(row["media_id"])
        self.assertEqual(row["identity_status"], "UNRESOLVED")
        markup = manual.render_manual_follow_conversion_html(result)
        markdown = manual.render_manual_follow_conversion_markdown(result)
        self.assertNotIn("href=", markup)
        self.assertNotIn("https://example.com/unverified", markdown)
        self.assertIn("Unknown", markup)
        self.assertIn("Recorded date is not the measurement date", markdown)

    def test_renderers_escape_source_text_and_reject_unsafe_links(self):
        result = self.build(observation(
            creative='<script>alert("x")</script> | [link]',
            identity_status="VERIFIED", permalink="javascript:alert(1)",
            user_reported_rate_percent="<0.045",
        ))
        markup = manual.render_manual_follow_conversion_html(result)
        markdown = manual.render_manual_follow_conversion_markdown(result)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertIn("&lt;0.045%", markup)
        self.assertNotIn("javascript:", markup)
        self.assertNotIn("javascript:", markdown)
        self.assertIn(r"\| \[link\]", markdown)
        self.assertNotIn("<script>", markdown)

    def test_verified_safe_link_is_displayed_without_joining_api_rows(self):
        result = self.build(observation(
            creative="Verified post", identity_status="VERIFIED",
            media_id="123", permalink="https://www.instagram.com/reel/123/",
        ))
        markup = manual.render_manual_follow_conversion_html(result)
        self.assertIn('href="https://www.instagram.com/reel/123/"', markup)
        self.assertIn("[Verified post](https://www.instagram.com/reel/123/)", manual.render_manual_follow_conversion_markdown(result))
        imports = [node for node in ast.walk(ast.parse(Path(manual.__file__).read_text()))
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        imported = {node.module if isinstance(node, ast.ImportFrom) else alias.name
                    for node in imports for alias in (node.names if isinstance(node, ast.Import) else [None])}
        self.assertFalse(any("moneyball" in (name or "") or "ledger" in (name or "") for name in imported))

    def test_matched_alias_links_to_reel_and_published_title_is_auditable(self):
        result = self.build(observation(
            creative="MITの新試験", identity_status="MATCHED",
            media_id="17943770034057030", permalink="https://www.instagram.com/reel/DbfmSZflC71/",
            published_title="MITは試験でAIを禁止せず、全員に配った",
            published_at="2026-08-01T10:14:00+00:00", distribution="trial",
            identity_match={"method": "caption and earlier manual mapping", "confidence": "HIGH"},
            viewers=28911, follows=14, user_reported_rate_percent="0.048", watch_seconds=11,
        ))
        markup = manual.render_manual_follow_conversion_html(result)
        markdown = manual.render_manual_follow_conversion_markdown(result)
        self.assertIn('href="https://www.instagram.com/reel/DbfmSZflC71/">MITの新試験</a>', markup)
        self.assertIn("Published title: MITは試験でAIを禁止せず、全員に配った", markup)
        self.assertIn("[MITの新試験](https://www.instagram.com/reel/DbfmSZflC71/)<br>Published title:", markdown)
        self.assertIn("<th>Attribution</th><th>Distribution</th>", markup)
        self.assertIn("<td>post_level</td><td>trial</td>", markup)
        self.assertIn("| Attribution | Distribution |", markdown)
        self.assertIn(r"| post\_level | trial |", markdown)
        self.assertEqual(result["resolved_identity_count"], 1)
        self.assertIn("1 of 1 creatives are linked to published Reels", markup)
        row = result["rows"][0]
        self.assertEqual(row["attribution_scope"], "post_level")
        self.assertEqual(row["distribution"], "trial")
        self.assertEqual(row["identity_match"]["confidence"], "HIGH")
        self.assertIsNone(row["observed_at"])
        self.assertEqual(row["user_reported_rate_percent"], "0.048")

    def test_identity_enrichment_preserves_supplied_bounds_and_group_ranks(self):
        sources = [
            observation("regular", viewers=1000, follows=10),
            observation("trial", viewers=2000, follows=10),
            observation("bounded", viewers="~20000", follows="<9", user_reported_rate_percent="<0.045"),
        ]
        before = self.build(*sources)
        enriched = copy.deepcopy(sources)
        for index, row in enumerate(enriched):
            row.update(
                identity_status="MATCHED", media_id=str(index + 100),
                permalink=f"https://www.instagram.com/reel/{index + 100}/",
                published_title='<Actual title> | [with source punctuation]',
                distribution="trial" if row["creative_id"] == "trial" else "regular",
            )
        after = self.build(*enriched)
        for old, new in zip(before["rows"], after["rows"]):
            self.assertEqual(
                {key: old[key] for key in ("viewers", "follows", "user_reported_rate_percent", "group_key", "group_rank", "group_exact_count", "calculated_rate_percent", "flags")},
                {key: new[key] for key in ("viewers", "follows", "user_reported_rate_percent", "group_key", "group_rank", "group_exact_count", "calculated_rate_percent", "flags")},
            )
        self.assertEqual(len(after["groups"]), 1)
        markup = manual.render_manual_follow_conversion_html(after)
        markdown = manual.render_manual_follow_conversion_markdown(after)
        self.assertIn("&lt;Actual title&gt;", markup)
        self.assertNotIn("<Actual title>", markup)
        self.assertIn(r"&lt;Actual title&gt; \| \[with source punctuation\]", markdown)
        self.assertIn("~20000", markup)
        self.assertIn("&lt;9", markup)
        self.assertIn("&lt;0.045%", markup)

    def test_matched_identity_requires_numeric_id_and_instagram_reel_link(self):
        matched = observation(
            identity_status="MATCHED", media_id="123",
            permalink="https://www.instagram.com/reel/Abc-123_/",
        )
        for changes in (
            {"media_id": None}, {"media_id": "guess"},
            {"permalink": None}, {"permalink": "javascript:alert(1)"},
            {"permalink": "https://instagram.com.example.com/reel/Abc/"},
            {"permalink": "https://www.instagram.com/p/Abc/"},
            {"permalink": "http://www.instagram.com/reel/Abc/"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "MATCHED"):
                self.build(matched | changes)
        valid = self.build(matched)
        self.assertEqual(valid["resolved_identity_count"], 1)

    def test_missing_source_is_unavailable_and_mismatched_source_is_rejected(self):
        result = manual.build_manual_follow_conversion(self.path)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertIn("unavailable", manual.render_manual_follow_conversion_html(result))
        self.build(observation())
        with self.assertRaisesRegex(ValueError, "account/platform mismatch"):
            manual.build_manual_follow_conversion(self.path, account="another_account")
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Cannot read"):
            manual.build_manual_follow_conversion(self.path)


if __name__ == "__main__":
    unittest.main()
