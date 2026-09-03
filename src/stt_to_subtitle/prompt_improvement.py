"""Deterministic feedback sampling and prompt-improvement contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


MIN_FEEDBACK_ITEMS = 20
MIN_FEEDBACK_JOBS = 3
MAX_FEEDBACK_ITEMS = 200

PROMPT_IMPROVEMENT_INSTRUCTION_VERSION = "prompt-improvement-v2"
PROMPT_IMPROVEMENT_SYSTEM_PROMPT = (
    "당신은 일본어 음성 전사를 한국어 자막으로 변환하는 프롬프트를 "
    "개선하는 수석 프롬프트 설계자다. 사용자가 실제로 수정한 사례에서 "
    "반복되는 오류와 도메인 규칙을 추출하되 개별 문장을 암기하지 마라. "
    "기존 프롬프트의 요구사항 중 수정 사례와 충돌하지 않는 항목은 모두 "
    "보존하고, 즉시 대체할 수 있는 완전한 프롬프트 하나를 작성하라. "
    "train 사례로 개선 규칙을 도출하고 holdout 사례를 기준으로 현재안과 "
    "후보안의 예상 품질 및 회귀 위험을 평가하라. 점수는 모델의 추정치이며 "
    "실제 재번역 시험 결과가 아니다. 비공개 미디어 경로를 복사하거나 "
    "제공되지 않은 사실을 만들지 마라."
)

PROMPT_DRAFT_INSTRUCTION_VERSION = "prompt-draft-v1"
PROMPT_DRAFT_SYSTEM_PROMPT = (
    "당신은 일본어 음성 전사를 한국어 자막으로 만드는 2단계 번역 시스템의 "
    "수석 프롬프트 설계자다. 사용자가 설명한 도메인에 맞춰 1차 초벌 번역 "
    "프롬프트와 2차 검사·교정 프롬프트를 각각 완전한 형태로 작성하라. "
    "두 프롬프트 모두 일본어 원문을 최종 근거로 삼고, 입력 세그먼트의 id와 "
    "순서를 보존하며, 각 id에 정확히 하나의 비어 있지 않은 한국어 번역을 "
    "반환하도록 요구해야 한다. 기계적으로 끊긴 문장과 단어는 인접 문맥으로 "
    "복원하되 누락된 음성을 추측하지 않게 하라. 1차 프롬프트는 의미·구조 "
    "보존과 누락 방지에 집중하고, 2차 프롬프트는 일본어 원문과 1차 결과를 "
    "대조해 오역·누락·문체·호칭·용어 일관성을 교정하게 하라. 최종 출력 "
    "계약은 {\"translations\":[{\"id\":\"원래 id\",\"text\":\"한국어 자막\"}]} "
    "형식만 허용하도록 명시하라. 도메인 설명에 없는 사실·인물·용어를 "
    "만들지 말고, 사용자 설명에 민감한 내용이 있더라도 임의로 검열하지 마라."
)


def split_feedback_by_job(
    feedback: Sequence[Mapping[str, Any]],
    *,
    seed: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return a deterministic, job-stratified 80/20 train/holdout split."""

    current = [dict(item) for item in feedback[:MAX_FEEDBACK_ITEMS]]
    job_ids = {str(item["job_id"]) for item in current}
    if len(current) < MIN_FEEDBACK_ITEMS or len(job_ids) < MIN_FEEDBACK_JOBS:
        raise ValueError(
            "개선안 생성에는 포함된 피드백 20개와 서로 다른 작업 3개가 "
            "필요합니다."
        )
    ranked_jobs = sorted(
        job_ids,
        key=lambda job_id: hashlib.sha256(
            f"{seed}\0{job_id}".encode("utf-8")
        ).hexdigest(),
    )
    holdout_count = max(1, round(len(ranked_jobs) * 0.2))
    holdout_jobs = set(ranked_jobs[:holdout_count])
    train = [item for item in current if str(item["job_id"]) not in holdout_jobs]
    holdout = [item for item in current if str(item["job_id"]) in holdout_jobs]
    if not train or not holdout:
        raise ValueError("학습/검증 피드백 분할을 만들 수 없습니다.")
    return train, holdout


def improvement_request_payload(
    *,
    stage: str,
    current_prompt: str,
    train: Sequence[Mapping[str, Any]],
    holdout: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "proposed_prompt": {"type": "string"},
            "summary": {"type": "string"},
            "current_score": {"type": "number", "minimum": 0, "maximum": 100},
            "candidate_score": {"type": "number", "minimum": 0, "maximum": 100},
            "regressions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "feedback_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["feedback_id", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "proposed_prompt",
            "summary",
            "current_score",
            "candidate_score",
            "regressions",
        ],
        "additionalProperties": False,
    }

    def public_item(item: Mapping[str, Any]) -> dict[str, str]:
        return {
            "feedback_id": str(item["id"]),
            "source": str(item["source_text"]),
            "model": str(item["model_text"]),
            "edited": str(item["edited_text"]),
        }

    return {
        "model": "router-selected",
        "temperature": 0,
        "max_tokens": 8192,
        "reasoning_effort": "none",
        "messages": [
            {
                "role": "system",
                "content": PROMPT_IMPROVEMENT_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "stage": stage,
                        "current_prompt": current_prompt,
                        "train": [public_item(item) for item in train],
                        "holdout": [public_item(item) for item in holdout],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "prompt_improvement",
                "strict": True,
                "schema": schema,
            },
        },
    }


def prompt_draft_request_payload(
    *,
    name: str,
    domain_description: str,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "translation_prompt": {"type": "string"},
            "review_prompt": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["translation_prompt", "review_prompt", "summary"],
        "additionalProperties": False,
    }
    return {
        "model": "user-selected",
        "temperature": 0,
        "max_tokens": 16_384,
        "messages": [
            {"role": "system", "content": PROMPT_DRAFT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "prompt_name": name.strip(),
                        "domain_description": domain_description.strip(),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "subtitle_prompt_draft",
                "strict": True,
                "schema": schema,
            },
        },
    }


def parse_prompt_draft_result(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("기초 프롬프트 생성 응답 형식이 올바르지 않습니다.")
    translation_prompt = str(value.get("translation_prompt", "")).strip()
    review_prompt = str(value.get("review_prompt", "")).strip()
    summary = str(value.get("summary", "")).strip()
    if not translation_prompt or not review_prompt:
        raise ValueError("기초 프롬프트 생성 결과가 비어 있습니다.")
    if len(translation_prompt) > 50_000 or len(review_prompt) > 50_000:
        raise ValueError("생성된 프롬프트는 각각 50,000자 이하여야 합니다.")
    return {
        "translation_prompt": translation_prompt,
        "review_prompt": review_prompt,
        "summary": summary,
    }


def parse_improvement_result(value: Any) -> tuple[str, dict[str, Any]]:
    try:
        if not isinstance(value, Mapping):
            raise TypeError("result must be an object")
        proposed_prompt = str(value["proposed_prompt"]).strip()
        current_score = float(value["current_score"])
        candidate_score = float(value["candidate_score"])
        regressions = value["regressions"]
        summary = str(value["summary"]).strip()
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("프롬프트 개선 모델 응답 형식이 올바르지 않습니다.") from error
    if not proposed_prompt or len(proposed_prompt) > 50_000:
        raise ValueError("개선 프롬프트 내용이 올바르지 않습니다.")
    if not 0 <= current_score <= 100 or not 0 <= candidate_score <= 100:
        raise ValueError("프롬프트 평가 점수 범위가 올바르지 않습니다.")
    if not isinstance(regressions, list):
        raise ValueError("프롬프트 회귀 목록 형식이 올바르지 않습니다.")
    normalized_regressions = [
        {
            "feedback_id": str(item["feedback_id"]),
            "reason": str(item["reason"]),
        }
        for item in regressions
        if isinstance(item, Mapping)
        and str(item.get("feedback_id", "")).strip()
        and str(item.get("reason", "")).strip()
    ]
    return proposed_prompt, {
        "summary": summary,
        "current_score": current_score,
        "candidate_score": candidate_score,
        "score_delta": candidate_score - current_score,
        "regressions": normalized_regressions,
    }


def parse_improvement_response(response: Any) -> tuple[str, dict[str, Any]]:
    try:
        choice = response.json()["choices"][0]
        content = choice["message"]["content"]
        if str(choice.get("finish_reason", "")).lower() == "length":
            raise ValueError("프롬프트 개선 모델의 출력 한도를 초과했습니다.")
        payload = json.loads(content) if isinstance(content, str) else content
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("프롬프트 개선 모델 응답 형식이 올바르지 않습니다.") from error
    return parse_improvement_result(payload)
