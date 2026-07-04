import unittest

import build_text_reel


class BuildTextReelMetadataTests(unittest.TestCase):
    def test_replacement_metadata_preserves_live_row_mapping(self) -> None:
        metadata = build_text_reel.brief_manifest_metadata(
            {
                "replacementPriority": 4,
                "replacesContentHash": "abc123",
                "replacesScheduledAt": "2026-07-17T13:04:00+09:00",
                "replacementDirection": "AI product safety / user agency",
                "sourceLabel": "arXiv 2605.04785",
                "sourceNote": "AgentTrust / runtime safety",
                "sourceChip": "安全設計",
            }
        )

        self.assertEqual(metadata["recommended_publish_at"], "2026-07-17T13:04:00+09:00")
        self.assertEqual(metadata["source_label"], "arXiv 2605.04785")
        self.assertEqual(metadata["source_note"], "AgentTrust / runtime safety")
        self.assertEqual(metadata["source_chip"], "安全設計")
        self.assertEqual(
            metadata["replacement"],
            {
                "priority": 4,
                "replaces_content_hash": "abc123",
                "replaces_scheduled_at": "2026-07-17T13:04:00+09:00",
                "direction": "AI product safety / user agency",
            },
        )

    def test_explicit_recommended_publish_time_wins(self) -> None:
        metadata = build_text_reel.brief_manifest_metadata(
            {
                "recommendedPublishAt": "2026-07-05T20:30:00+09:00",
                "replacesScheduledAt": "2026-07-05T21:59:00+09:00",
            }
        )

        self.assertEqual(metadata["recommended_publish_at"], "2026-07-05T20:30:00+09:00")
        self.assertEqual(
            metadata["replacement"],
            {"replaces_scheduled_at": "2026-07-05T21:59:00+09:00"},
        )


if __name__ == "__main__":
    unittest.main()
