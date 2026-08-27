"""Versioned JSON request contracts for the Backend control plane."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


JobOperation = Literal["extract", "transcribe", "translate", "full", "compare"]


class JobCreateRequest(BaseModel):
    source_rels: list[str] = Field(default_factory=list)
    folder_rels: list[str] = Field(default_factory=list)
    force_overwrite: bool = False
    operation: JobOperation = "full"
    prompt_category_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class JobIdsRequest(BaseModel):
    job_ids: list[str] = Field(default_factory=list)


class TranslationSelectionRequest(JobIdsRequest):
    prompt_category_id: str


class ReprocessRequest(BaseModel):
    operation: Literal["extract", "transcribe", "translate", "full"]
    prompt_category_id: str | None = None


class RestartTranslationRequest(BaseModel):
    prompt_category_id: str | None = None
    transcript_revision_id: str | None = None


class ArtifactUpdateRequest(BaseModel):
    content: str


class ServerSettingsUpdateRequest(BaseModel):
    stt_base_url: str
    stt_token: str | None = None
    clear_stt_token: bool = False
    lm_base_url: str = ""
    lm_token: str | None = None
    clear_lm_token: bool = False
    lm_model: str = ""
    translation_workers: int = 1


class TranslationModelLookupRequest(BaseModel):
    lm_base_url: str
    lm_token: str | None = None
    clear_lm_token: bool = False


class RuntimeEndpointCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str = ""
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)


class RuntimeEndpointUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str | None = None
    clear_token: bool = False
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)


class SubtitleValidatorUpdateRequest(BaseModel):
    provider: Literal["openai_compatible", "openrouter", "bedrock"]
    base_url: str = ""
    token: str | None = None
    clear_token: bool = False
    model: str
    region: str = ""


class PathDisplayRuleRequest(BaseModel):
    source_pattern: str
    display_pattern: str


class PromptCategoryRequest(BaseModel):
    name: str
    translation_prompt: str
    review_prompt: str


class PromptCategoryStateRequest(BaseModel):
    archived: bool


class ArtifactCleanupRequest(BaseModel):
    minimum_age_days: int = Field(default=7, ge=1, le=3650)
    cleanup_token: str


class ComparisonTranslationRequest(JobIdsRequest):
    prompt_category_id: str


class ComparisonRerunRequest(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)


class SubtitleGenerationPublishRequest(BaseModel):
    generation_id: str
