"""Versioned JSON request contracts for the Backend control plane."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


JobOperation = Literal[
    "extract",
    "transcribe",
    "translate",
    "full",
    "draft_translate",
    "review_translate",
    "external_review",
]
TranslationStage = Literal["draft", "review"]
TranslationMode = Literal["draft_only", "review_existing", "draft_and_review"]


class JobCreateRequest(BaseModel):
    source_rels: list[str] = Field(default_factory=list)
    folder_rels: list[str] = Field(default_factory=list)
    force_overwrite: bool = False
    is_test: bool = False
    operation: JobOperation = "transcribe"
    prompt_category_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class JobIdsRequest(BaseModel):
    job_ids: list[str] = Field(default_factory=list)


class TranslationSelectionRequest(JobIdsRequest):
    prompt_category_id: str
    translation_mode: TranslationMode = "draft_only"
    target_stage: TranslationStage | None = None


ExternalModelProvider = Literal[
    "openrouter",
    "bedrock",
    "nvidia_build",
]


class ExternalReviewSelectionRequest(JobIdsRequest):
    provider: ExternalModelProvider
    model: str = Field(min_length=1)


class ExternalModelProfileRequest(BaseModel):
    base_url: str = ""
    credential: str | None = None
    clear_credential: bool = False
    region: str = ""


class ExternalModelSelectionRequest(BaseModel):
    model: str = Field(min_length=1)


class ReprocessRequest(BaseModel):
    operation: Literal["transcribe"]
    prompt_category_id: str | None = None


class RestartTranslationRequest(BaseModel):
    prompt_category_id: str | None = None
    transcript_revision_id: str | None = None


class ArtifactUpdateRequest(BaseModel):
    content: str


class TranslationItemUpdateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)


class SubtitleTimelineCueRequest(BaseModel):
    id: str | None = Field(default=None, max_length=200)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    speaker: str = Field(default="UNKNOWN", min_length=1, max_length=200)
    source_text: str = Field(min_length=1, max_length=10_000)
    text: str = Field(min_length=1, max_length=10_000)


class SubtitleTimelineUpdateRequest(BaseModel):
    cues: list[SubtitleTimelineCueRequest] = Field(min_length=1, max_length=20_000)


class ServerSettingsUpdateRequest(BaseModel):
    stt_base_url: str
    stt_token: str | None = None
    clear_stt_token: bool = False


class TranslationEndpointCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str = ""
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)
    resource_group_id: str = Field(default="local-gpu", min_length=1, max_length=80)
    thinking_enabled: bool = False


class TranslationEndpointUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str | None = None
    clear_token: bool = False
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)
    resource_group_id: str = Field(default="local-gpu", min_length=1, max_length=80)
    thinking_enabled: bool | None = None


class TranslationEndpointRoutingRequest(BaseModel):
    enabled: bool = True
    batch_preferred: bool = False


class TranslationServerModelRequest(BaseModel):
    model: str = Field(min_length=1)


class TranscriberEndpointCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str = ""
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)
    resource_group_id: str = Field(default="local-gpu", min_length=1, max_length=80)
    kotoba_batch_size: int | None = Field(default=None, ge=1, le=64)
    whisperx_batch_size: int | None = Field(default=None, ge=1, le=64)


class TranscriberEndpointUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str | None = None
    clear_token: bool = False
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)
    resource_group_id: str = Field(default="local-gpu", min_length=1, max_length=80)
    kotoba_batch_size: int | None = Field(default=None, ge=1, le=64)
    whisperx_batch_size: int | None = Field(default=None, ge=1, le=64)
    clear_kotoba_batch_size: bool = False
    clear_whisperx_batch_size: bool = False


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


class TranslationFeedbackStateRequest(BaseModel):
    included: bool


class PromptImprovementCreateRequest(BaseModel):
    category_id: str = Field(min_length=1)
    stage: Literal["translation", "review"]
    provider: ExternalModelProvider
    model: str = Field(min_length=1)


class PromptDraftCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    domain_description: str = Field(min_length=1, max_length=10_000)
    provider: ExternalModelProvider
    model: str = Field(min_length=1)


class ArtifactCleanupRequest(BaseModel):
    minimum_age_days: int = Field(default=7, ge=1, le=3650)
    cleanup_token: str


class SubtitleGenerationPublishRequest(BaseModel):
    generation_id: str
