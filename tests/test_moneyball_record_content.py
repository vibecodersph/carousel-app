import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moneyball_record_content import build_record_holder_content
from tests.test_verified_winner_library import ass_script, post


def records(*media_ids):
    return {"current_records": {
        f"{window}:reach": {"leaders": [{"media_id": media_id}]}
        for window, media_id in zip(("24h", "72h", "7d", "lifetime"), media_ids)
    }}


class RecordHolderContentTests(unittest.TestCase):
    def test_library_fields_are_reused_exactly_without_reading_artifacts(self):
        winner = {
            "identity": {"media_id": "holder", "caption": "Published caption"},
            "content": {"japanese_script": {"text": "原稿"}}, "source": {"url": "source"},
            "asset_provenance": {"published_asset": {"status": "VERIFIED"}},
            "evidence_flags": [], "winner_evidence": {"ranking_memberships": ["24h"]},
        }
        expected = {key: copy.deepcopy(winner[key]) for key in winner if key != "winner_evidence"}
        with patch("moneyball_record_content.winner_library.resolve_clip_dir") as resolver:
            result = build_record_holder_content({}, records("holder"), {"winners": [winner]})
        resolver.assert_not_called()
        self.assertEqual(result["holder"], expected)
        result["holder"]["content"]["japanese_script"]["text"] = "changed"
        self.assertEqual(winner["content"]["japanese_script"]["text"], "原稿")

    def test_missing_content_is_unavailable_and_does_not_invent_hook_or_rank(self):
        result = build_record_holder_content({}, records("missing"), {})["missing"]
        self.assertIsNone(result["content"]["published_hook"]["value"])
        self.assertEqual(result["content"]["source_transcript"]["status"], "UNAVAILABLE")
        self.assertEqual(result["content"]["japanese_script"]["status"], "UNAVAILABLE")
        self.assertNotIn("winner_evidence", result)
        self.assertEqual(result["identity"]["media_id"], "missing")
        json.dumps(result, allow_nan=False)

    def test_extra_72h_and_lifetime_holders_preserve_scripts_and_published_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory)
            (clip / "notes.json").write_text(json.dumps({"transcript": "Source from notes", "source_chapter": "Intro"}))
            (clip / "subtitles.ja.ass").write_text(ass_script(["最初の文章", "次の文章"]), encoding="utf-8")
            (clip / "subtitles.en.ass").write_text(ass_script(["Source subtitle"]), encoding="utf-8")
            media = clip / "reel.ja.aibrief_jp.mp4"
            media.write_bytes(b"fixture media")
            holder = post(media_id="extra", content_hash=hashlib.sha256(media.read_bytes()).hexdigest(),
                          clip_dir=clip, caption="\n Published first line\nSecond line")
            data = {"current_records": {"72h:reach": {"leaders": [{"media_id": "extra"}]},
                                        "lifetime:views": {"leaders": [{"media_id": "extra"}]}}}
            result = build_record_holder_content({"posts": [holder]}, data, {})
            self.assertEqual(list(result), ["extra"])
            content = result["extra"]["content"]
            self.assertEqual(content["published_hook"]["value"], "Published first line")
            self.assertEqual(content["published_hook"]["source"], "published_caption_first_line")
            self.assertEqual(content["japanese_script"]["text"], "最初の文章 次の文章")
            self.assertEqual(content["source_transcript"]["text"], "Source from notes")
            self.assertEqual(result["extra"]["asset_provenance"]["published_asset"]["status"], "VERIFIED")
            self.assertNotIn("winner_evidence", result["extra"])

    def test_generation_hook_english_fallback_and_mismatched_asset_are_labelled(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory)
            (clip / "notes.json").write_text("{}")
            (clip / "subtitles.en.ass").write_text(ass_script(["English fallback"]), encoding="utf-8")
            (clip / "reel.ja.aibrief_jp.mp4").write_bytes(b"different render")
            holder = post(media_id="holder", content_hash="wronghash", clip_dir=clip, caption="")
            result = build_record_holder_content({"posts": [holder]}, records("holder"), {})["holder"]
            self.assertEqual(result["content"]["published_hook"]["source"], "generation_pipeline_hook_text")
            self.assertEqual(result["content"]["published_hook"]["value"], "Mutable generated hook holder")
            self.assertEqual(result["content"]["source_transcript"]["text"], "English fallback")
            self.assertTrue(result["content"]["source_transcript"]["source_path"].endswith("subtitles.en.ass"))
            self.assertIn("PUBLISHED_ASSET_HASH_MISMATCH", result["evidence_flags"])
            self.assertIn("JAPANESE_SCRIPT_UNAVAILABLE", result["evidence_flags"])

    def test_only_record_holders_have_artifacts_resolved(self):
        holder = {"identity": {"media_id": "holder", "caption": "Title"}}
        unrelated = {"identity": {"media_id": "unrelated"}, "generation_artifact": {"clip_dir": "/unused"}}
        unresolved = {"path": None, "status": "UNAVAILABLE", "confidence": "unavailable"}
        with patch("moneyball_record_content.winner_library.resolve_clip_dir", return_value=unresolved) as resolver:
            result = build_record_holder_content({"posts": [unrelated, holder]}, records("holder"), {})
        resolver.assert_called_once_with(holder)
        self.assertEqual(list(result), ["holder"])


if __name__ == "__main__":
    unittest.main()
