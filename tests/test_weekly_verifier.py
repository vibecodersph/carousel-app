import unittest

from weekly_verifier import SlideRecord, verify_record


class WeeklyVerifierTests(unittest.TestCase):
    def test_blocks_model_becoming_company(self) -> None:
        record = SlideRecord(
            slide=8,
            label="07 SECURITY",
            headline="サイモン氏がFable閉鎖の脱獄手法に疑問",
            body="開発者のサイモン氏がFable社を閉鎖させた脱獄手法の低さに失望を示しました。",
            category="SECURITY",
            source_url="https://www.anthropic.com/news/fable-mythos-access",
            source_text=(
                "The US government issued an export control directive to suspend access "
                "to Fable 5 and Mythos 5. Anthropic disabled access for all customers."
            ),
        )

        verified = verify_record(record)

        self.assertEqual(verified.verdict, "blocked")
        self.assertTrue(any("company" in note for note in verified.notes))

    def test_blocks_untraced_number(self) -> None:
        record = SlideRecord(
            slide=6,
            label="05 PARTNERSHIP",
            headline="DeepMindがサッカー戦術AIで提携",
            body="試合展開を8秒先まで予測する実証実験を開始します。",
            category="PARTNERSHIP",
            source_url="https://deepmind.google",
            source_text="Google DeepMind announced TacticAI work with a football club.",
        )

        verified = verify_record(record)

        self.assertEqual(verified.verdict, "blocked")
        self.assertTrue(any("8秒" in note for note in verified.notes))

    def test_blocks_summary_without_specifics(self) -> None:
        record = SlideRecord(
            slide=2,
            label="01 POLICY",
            headline="Anthropicが政策機関との溝を埋める新構想",
            body="同社はAI政策の溝を埋める新構想を開始します。",
            category="POLICY",
            source_url="https://www.anthropic.com/policy-on-the-ai-exponential",
            source_text="Anthropic published Policy on the AI Exponential.",
        )

        verified = verify_record(record)

        self.assertEqual(verified.verdict, "blocked")
        self.assertTrue(any("specific" in note for note in verified.notes))

    def test_verifies_corrected_fable_story(self) -> None:
        record = SlideRecord(
            slide=2,
            label="01 SECURITY",
            headline="米政府がClaudeの最新モデルを世界で停止",
            body="米政府は6月12日、輸出管理を理由にFable 5とMythos 5を全世界で停止。Anthropicは軽微な脱獄だと反論。",
            category="SECURITY",
            source_url="https://www.anthropic.com/news/fable-mythos-access",
            source_text=(
                "Claude Fable 5 and Claude Mythos 5 are Anthropic models. "
                "On June 12, 2026, the US government issued an export control directive "
                "to suspend all access to Fable 5 and Mythos 5. Anthropic says the "
                "jailbreak was narrow and minor."
            ),
        )

        verified = verify_record(record)

        self.assertEqual(verified.verdict, "verified")


if __name__ == "__main__":
    unittest.main()
