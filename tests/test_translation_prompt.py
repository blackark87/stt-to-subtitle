import unittest

from stt_to_subtitle.job_store import PROMPT_TEXT_MAX_LENGTH
from stt_to_subtitle.translation_prompt import (
    KOREAN_JAV_DRAFT_PROMPT,
    KOREAN_JAV_REVIEW_PROMPT,
    KOREAN_JAV_SYSTEM_PROMPT,
    KOREAN_VARIETY_DRAFT_PROMPT,
    KOREAN_VARIETY_REVIEW_PROMPT,
    KOREAN_VARIETY_SYSTEM_PROMPT,
)


class TranslationPromptTests(unittest.TestCase):
    def test_draft_and_review_prompts_have_independent_pass_roles(self) -> None:
        for draft_prompt, review_prompt in (
            (KOREAN_JAV_DRAFT_PROMPT, KOREAN_JAV_REVIEW_PROMPT),
            (KOREAN_VARIETY_DRAFT_PROMPT, KOREAN_VARIETY_REVIEW_PROMPT),
        ):
            self.assertIn("ROLE — FIRST-PASS", draft_prompt)
            self.assertNotIn("draft_translations", draft_prompt)
            self.assertNotIn("ROLE — SECOND-PASS", draft_prompt)
            self.assertIn("ROLE — SECOND-PASS", review_prompt)
            self.assertIn("draft_translations", review_prompt)
            self.assertIn("preserve it exactly", review_prompt)
            self.assertNotIn("ROLE — FIRST-PASS", review_prompt)
            self.assertNotEqual(draft_prompt, review_prompt)

    def test_both_passes_share_source_and_output_contracts(self) -> None:
        prompts = (
            KOREAN_JAV_DRAFT_PROMPT,
            KOREAN_JAV_REVIEW_PROMPT,
            KOREAN_VARIETY_DRAFT_PROMPT,
            KOREAN_VARIETY_REVIEW_PROMPT,
        )
        for prompt in prompts:
            self.assertIn("Japanese text in target_segments", prompt)
            self.assertIn("Preserve every target id exactly", prompt)
            self.assertIn('"translations"', prompt)
            self.assertIn("Accuracy comes before surface fluency", prompt)
            self.assertIn("FINAL KOREAN DELIVERY GATE", prompt)
            self.assertIn("cold-read every target", prompt)
            self.assertIn("Never hide uncertainty", prompt)
            self.assertIn("Never return an isolated Korean particle", prompt)
            self.assertIn("Japanese long-vowel mark", prompt)
            self.assertLessEqual(len(prompt), PROMPT_TEXT_MAX_LENGTH)

    def test_review_pass_independently_rechecks_the_draft(self) -> None:
        for prompt in (KOREAN_JAV_REVIEW_PROMPT, KOREAN_VARIETY_REVIEW_PROMPT):
            self.assertIn("draft is untrusted evidence", prompt)
            self.assertIn("do not let a fluent-looking", prompt)
            self.assertIn("Korean-only cold read", prompt)
            self.assertIn("unexplained nonword", prompt)

    def test_jav_passes_share_explicitness_and_terminology_policy(self) -> None:
        for prompt in (KOREAN_JAV_DRAFT_PROMPT, KOREAN_JAV_REVIEW_PROMPT):
            self.assertIn("without censorship", prompt)
            self.assertIn("never reverse sexual direction", prompt)
            self.assertIn("生ハメ→노콘", prompt)
            self.assertIn("性癖→성적 취향, never 성벽", prompt)
            self.assertIn("シキュウ→자궁", prompt)
            self.assertIn("invented Hangul nonword", prompt)

    def test_variety_passes_share_humor_register_and_dialect_policy(self) -> None:
        for prompt in (
            KOREAN_VARIETY_DRAFT_PROMPT,
            KOREAN_VARIETY_REVIEW_PROMPT,
        ):
            self.assertIn("boke and tsukkomi", prompt)
            self.assertIn("Do not mechanically replace Kansai speech", prompt)
            self.assertIn("Keep hierarchy and address credible", prompt)
            self.assertIn("never invent a punchline", prompt)

    def test_legacy_system_names_remain_draft_aliases(self) -> None:
        self.assertIs(KOREAN_JAV_SYSTEM_PROMPT, KOREAN_JAV_DRAFT_PROMPT)
        self.assertIs(
            KOREAN_VARIETY_SYSTEM_PROMPT,
            KOREAN_VARIETY_DRAFT_PROMPT,
        )


if __name__ == "__main__":
    unittest.main()
