from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from stt_to_subtitle.job_store import JobStore
from stt_to_subtitle.prompt_improvement import (
    PROMPT_DRAFT_SYSTEM_PROMPT,
    PROMPT_IMPROVEMENT_SYSTEM_PROMPT,
    improvement_request_payload,
    parse_improvement_response,
    parse_prompt_draft_result,
    prompt_draft_request_payload,
    split_feedback_by_job,
)
from stt_to_subtitle.resource_groups import ResourceGroupLimiter


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class PromptImprovementTests(unittest.TestCase):
    def test_split_is_deterministic_and_keeps_jobs_out_of_both_sets(self) -> None:
        feedback = [
            {"id": f"f-{index}", "job_id": f"job-{index % 5}"}
            for index in range(25)
        ]

        first = split_feedback_by_job(feedback, seed="variety:review:r1")
        second = split_feedback_by_job(feedback, seed="variety:review:r1")

        self.assertEqual(first, second)
        train_jobs = {item["job_id"] for item in first[0]}
        holdout_jobs = {item["job_id"] for item in first[1]}
        self.assertFalse(train_jobs & holdout_jobs)
        self.assertEqual(len(first[0]) + len(first[1]), 25)

    def test_rejects_an_insufficient_feedback_sample(self) -> None:
        with self.assertRaisesRegex(ValueError, "20개"):
            split_feedback_by_job(
                [{"id": f"f-{index}", "job_id": "one-job"} for index in range(19)],
                seed="sample",
            )

    def test_builds_a_structured_request_and_parses_scores(self) -> None:
        sample = {
            "id": "f1",
            "source_text": "そうですね",
            "model_text": "그렇군요",
            "edited_text": "그렇네요",
        }
        request = improvement_request_payload(
            stage="translation",
            current_prompt="current",
            train=[sample],
            holdout=[sample],
        )
        proposed, evaluation = parse_improvement_response(
            _Response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": {
                                    "proposed_prompt": "improved",
                                    "summary": "honor tone",
                                    "current_score": 60,
                                    "candidate_score": 80,
                                    "regressions": [],
                                }
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        )

        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertEqual(
            request["messages"][0]["content"],
            PROMPT_IMPROVEMENT_SYSTEM_PROMPT,
        )
        self.assertEqual(proposed, "improved")
        self.assertEqual(evaluation["score_delta"], 20)

    def test_builds_and_parses_domain_prompt_draft(self) -> None:
        request = prompt_draft_request_payload(
            name="버라이어티",
            domain_description="다중 화자와 간사이 사투리가 많은 토크쇼",
        )
        result = parse_prompt_draft_result(
            {
                "translation_prompt": "first pass",
                "review_prompt": "second pass",
                "summary": "도메인 규칙을 반영함",
            }
        )

        self.assertEqual(request["messages"][0]["content"], PROMPT_DRAFT_SYSTEM_PROMPT)
        self.assertEqual(
            request["response_format"]["json_schema"]["name"],
            "subtitle_prompt_draft",
        )
        self.assertEqual(result["translation_prompt"], "first pass")
        self.assertEqual(result["review_prompt"], "second pass")

    def test_feedback_supersession_and_activation_are_immutable(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            category = store.get_prompt_category("variety")
            self.assertIsNotNone(category)
            assert category is not None
            store.create(
                job_id="job-1",
                source_rel="sample.mp4",
                force_overwrite=False,
                options={},
            )

            def generation(generation_id: str, config_hash: str):
                return store.create_translation_generation(
                    generation_id=generation_id,
                    job_id="job-1",
                    transcript_job_id="remote",
                    transcript_hash="transcript",
                    prompt_hash="prompt",
                    prompt_revision_id=category.prompt_revision_id,
                    endpoint_key="endpoint",
                    model="model",
                    config_hash=config_hash,
                    artifact_path=str(Path(directory) / f"{generation_id}.json"),
                    origin="manual" if generation_id.startswith("manual") else "automatic",
                    force_new=True,
                )

            source = generation("source", "source-config")
            manual_one = generation("manual-1", "manual-1-config")
            first = store.record_translation_feedback(
                job_id="job-1",
                category_id="variety",
                base_revision_id=category.prompt_revision_id,
                stage="translation",
                source_generation_id=source["id"],
                manual_generation_id=manual_one["id"],
                segment_id="segment-1",
                source_text="そうですね",
                model_text="그렇군요",
                edited_text="그렇네요",
                context={},
            )
            self.assertIsNotNone(first)
            assert first is not None
            store.set_translation_feedback_included(first["id"], included=False)
            manual_two = generation("manual-2", "manual-2-config")
            second = store.record_translation_feedback(
                job_id="job-1",
                category_id="variety",
                base_revision_id=category.prompt_revision_id,
                stage="translation",
                source_generation_id=manual_one["id"],
                manual_generation_id=manual_two["id"],
                segment_id="segment-1",
                source_text="そうですね",
                model_text="그렇네요",
                edited_text="그렇습니다",
                context={},
            )
            self.assertIsNotNone(second)
            assert second is not None
            current = store.list_translation_feedback(category_id="variety")
            self.assertEqual([item["id"] for item in current], [second["id"]])
            self.assertEqual(second["model_text"], "그렇군요")
            self.assertFalse(second["included"])

            run = store.create_prompt_improvement_run(
                category_id="variety",
                stage="translation",
                base_revision_id=category.prompt_revision_id,
                train_feedback_ids=[second["id"]],
                holdout_feedback_ids=[],
                endpoint_contract="review:endpoint",
                model_contract="review:model",
            )
            self.assertTrue(store.claim_prompt_improvement_run(run["id"]))
            self.assertTrue(
                store.complete_prompt_improvement_run(
                    run["id"],
                    proposed_prompt="improved draft",
                    evaluation={"current_score": 60, "candidate_score": 80},
                )
            )
            activated, activated_run = store.activate_prompt_improvement_run(
                run["id"]
            )
            self.assertEqual(activated.translation_prompt, "improved draft")
            self.assertEqual(activated.prompt_revision_number, category.prompt_revision_number + 1)
            self.assertEqual(activated_run["status"], "activated")
            self.assertEqual(
                store.prompt_revision_by_id(category.prompt_revision_id)["translation_prompt"],
                category.translation_prompt,
            )

    def test_rejects_activation_when_base_revision_is_stale(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            category = store.get_prompt_category("variety")
            assert category is not None
            run = store.create_prompt_improvement_run(
                category_id="variety",
                stage="review",
                base_revision_id=category.prompt_revision_id,
                train_feedback_ids=["f1"],
                holdout_feedback_ids=["f2"],
                endpoint_contract="endpoint",
                model_contract="model",
            )
            store.claim_prompt_improvement_run(run["id"])
            store.complete_prompt_improvement_run(
                run["id"],
                proposed_prompt="candidate",
                evaluation={},
            )
            store.update_prompt_category(
                "variety",
                name=category.name,
                translation_prompt=category.translation_prompt,
                review_prompt=category.review_prompt + "\nmanual change",
            )

            with self.assertRaisesRegex(ValueError, "기준 프롬프트"):
                store.activate_prompt_improvement_run(run["id"])


class ResourceGroupLimiterTests(unittest.TestCase):
    def test_serializes_two_clients_in_the_same_capacity_one_group(self) -> None:
        limiter = ResourceGroupLimiter()
        limiter.configure({"gpu-0": 1})
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def first() -> None:
            with limiter.reserve("gpu-0", timeout=1):
                order.append("first")
                entered.set()
                release.wait(timeout=1)

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertFalse(limiter.try_acquire("gpu-0"))
        release.set()
        thread.join(timeout=1)
        self.assertTrue(limiter.try_acquire("gpu-0"))
        order.append("second")
        limiter.release("gpu-0")
        self.assertEqual(order, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
