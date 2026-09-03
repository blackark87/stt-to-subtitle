/** Backend(/api/v1) 호출 계층. 화면은 이 파일 밖에서 fetch 하지 않는다. */

export interface PipelineJob {
  id: string;
  source_rel: string;
  status: string;
  operation: string;
  phase: string;
  state: string;
  reason_code: string | null;
  attempt: number;
  force_overwrite: boolean;
  is_test: boolean;
  options: Record<string, unknown>;
  transcriber_id: string | null;
  blocked_stage: string | null;
  error: string | null;
  chunks_created: number;
  chunks_completed: number;
  chunks_total_estimate: number;
  transcription_stage: string | null;
  transcription_stage_index: number;
  transcription_stage_total: number;
  translation_chunks_total: number;
  translation_chunks_completed: number;
  translation_pause_requested: boolean;
  job_stop_requested: boolean;
  created_at: string;
  updated_at: string;
  completion_summary?: JobCompletionSummary;
}

export interface JobCompletionSummary {
  transcription_backend: string;
  transcription_model_revision: string | null;
  translation_prompt_name: string;
  translation_prompt_version: number | null;
  started_at: string;
  ended_at: string;
  processing_seconds: number;
  timing_source: "events" | "job";
}

export interface JobListItem extends PipelineJob {
  nfo_title: string | null;
  poster_path: string | null;
  workflow_root_job_id?: string;
}

export interface TranscriberEndpoint {
  id: string;
  name: string;
  base_url: string;
  token_configured: boolean;
  enabled: boolean;
  capacity: number;
  resource_group_id: string;
  kotoba_batch_size: number | null;
  whisperx_batch_size: number | null;
  builtin: boolean;
  status: string;
  message: string | null;
  checked_at: number | null;
  running_jobs: number;
  available_slots: number;
}

export type TranslationStage = "draft" | "review";
export type TranslationMode = "draft_only" | "review_existing" | "draft_and_review";

export type ExternalModelProvider = "openrouter" | "bedrock" | "nvidia_build";

export interface ExternalModelProfile {
  provider: ExternalModelProvider;
  base_url: string;
  credential_configured: boolean;
  region: string;
  selected_model: string;
  models: string[];
  status: "unchecked" | "checking" | "ready" | "failed";
  message: string | null;
  checked_at: number | null;
  updated_at: number;
  configured: boolean;
}

export interface TranslationServer {
  id: string;
  stage: TranslationStage;
  name: string;
  base_url: string;
  token_configured: boolean;
  enabled: boolean;
  capacity: number;
  resource_group_id: string;
  thinking_enabled: boolean;
  builtin: boolean;
  batch_preferred: boolean;
  selected_model: string;
  models: string[];
  status: string;
  message: string | null;
  routing_state: "available" | "suspended" | "disabled" | "unconfigured";
  routing_reason: "stt_hard_breaker" | "review_priority" | "disabled" | "unconfigured" | null;
  routing_message: string | null;
  checked_at: number | null;
  running_jobs: number;
  available_slots: number;
}

export interface TranslationGroup {
  stage: TranslationStage;
  label: string;
  servers: TranslationServer[];
}

export interface GpuDevice {
  index?: string;
  display_name?: string;
  utilization_percent: number | null;
  memory_percent: number | null;
  memory_used_gib: number | null;
  memory_total_gib: number | null;
  memory_used_mib?: number | null;
  memory_total_mib?: number | null;
  temperature_celsius: number | null;
  power_watts: number | null;
}

export interface GpuSnapshot {
  available: boolean;
  configured: boolean;
  devices: GpuDevice[];
  error: string;
  error_code: string;
  observed_at: number | null;
  last_success_at: number | null;
  stale: boolean;
}

export interface DependencyState {
  state?: string;
  reason_code?: string | null;
  detail?: string | null;
  [key: string]: unknown;
}

export type DependencyStatus = DependencyState | string;

export interface DashboardPayload {
  state_counts: Record<string, number>;
  phase_counts: Record<string, number>;
  completion_counts: {
    audio: number;
    transcription: number;
    subtitle: number;
  };
  recent_jobs: PipelineJob[];
  active_jobs: PipelineJob[];
  attention_jobs: PipelineJob[];
  recent_completed: PipelineJob[];
  state_samples?: Record<string, PipelineJob[]>;
  dependencies: { transcription: DependencyStatus; translation: DependencyStatus };
  gpu: GpuSnapshot | null;
}

export interface MediaFolder {
  path: string;
  name: string;
  display_path?: string;
  display_name?: string;
  modified_at: number | null;
  actor_image_path?: string | null;
  has_subtitle?: boolean;
}

export interface MediaFile {
  path: string;
  paths?: string[];
  name: string;
  display_path?: string;
  display_paths?: string[];
  display_name?: string;
  size: number;
  created_at: number | null;
  modified_at: number | null;
  duration_seconds: number | null;
  has_subtitle: boolean;
  has_system_subtitle: boolean;
  has_untracked_subtitle: boolean;
  has_external_subtitle: boolean;
  external_subtitle_formats: string[];
  latest_subtitle_job_id: string | null;
  has_nfo: boolean;
  title: string;
  nfo_title: string | null;
  nfo_release_date: string | null;
  poster_path: string | null;
  actors: string[];
}

export interface MediaListing {
  current_folder: string;
  parent_folder: string | null;
  breadcrumbs: { name: string; path: string }[];
  folders: MediaFolder[];
  folder_total?: number;
  folder_offset?: number;
  folder_limit?: number | null;
  files: MediaFile[];
}

export interface ServerSettings {
  configured: boolean;
  transcription_configured: boolean;
  translation_configured: boolean;
  stt_base_url: string;
  stt_token_configured: boolean;
  stt_gate_state: string;
  stt_gate_message: string | null;
}

export interface SettingsPayload {
  servers: ServerSettings;
  transcribers: TranscriberEndpoint[];
  translation_groups: TranslationGroup[];
  translation_groups_error: string | null;
  external_models: ExternalModelProfile[];
  path_display_rules: PathDisplayRule[];
  prompt_categories: PromptCategory[];
  prompt_authoring: PromptAuthoringSettings;
  translation_feedback: TranslationFeedback[];
  prompt_improvement_runs: PromptImprovementRun[];
}

export interface PathDisplayRule {
  id: string;
  source_pattern: string;
  display_pattern: string;
  created_at: number;
  updated_at: number;
}

export interface PromptCategory {
  id: string;
  name: string;
  translation_prompt?: string;
  review_prompt?: string;
  prompt_revision_id: string;
  prompt_revision_number: number;
  archived?: boolean;
}

export interface PromptAuthoringSettings {
  improvement_instruction_version: string;
  improvement_system_prompt: string;
  draft_instruction_version: string;
  draft_system_prompt: string;
}

export interface TranslationFeedback {
  id: string;
  job_id: string;
  category_id: string;
  base_revision_id: string;
  stage: "translation" | "review";
  segment_id: string;
  source_text: string;
  model_text: string;
  edited_text: string;
  included: boolean;
  created_at: number;
}

export interface PromptDraft {
  translation_prompt: string;
  review_prompt: string;
  summary: string;
  provider: ExternalModelProvider;
  model: string;
  instruction_version: string;
}

export interface PromptImprovementRun {
  id: string;
  category_id: string;
  stage: "translation" | "review";
  base_revision_id: string;
  endpoint_contract: string;
  model_contract: string;
  train_feedback_ids: string[];
  holdout_feedback_ids: string[];
  proposed_prompt: string | null;
  evaluation: {
    summary: string;
    current_score: number;
    candidate_score: number;
    score_delta: number;
    regressions: { feedback_id: string; reason: string }[];
  } | null;
  status: "queued" | "running" | "ready" | "failed" | "cancelled" | "rejected" | "activated";
  error: string | null;
  activated_revision_id: string | null;
  created_at: number;
}

export interface JobEvent {
  created_at?: string | number;
  level?: string;
  message?: string;
  event_code?: string;
  from_state?: string | null;
  to_state?: string | null;
  phase?: string | null;
  attempt?: number | null;
  payload?: Record<string, unknown>;
}

export interface JobDetailPayload {
  job: PipelineJob;
  parent_job: PipelineJob | null;
  child_jobs: PipelineJob[];
  workflow_root_job_id: string;
  workflow_jobs: PipelineJob[];
  workflow_history_jobs: PipelineJob[];
  previous_subtitle_workflows: SubtitleWorkflowHistorySummary[];
  events: JobEvent[];
  transcript_revisions: Record<string, unknown>[];
  translation_generations: Record<string, unknown>[];
  subtitle_generations: Record<string, unknown>[];
  subtitle_validation: Record<string, unknown> | null;
  external_subtitles: string[];
}

export interface SubtitleWorkflowHistorySummary {
  workflow_root_job_id: string;
  latest_job_id: string;
  latest_generation_created_at: number;
  transcription_backend: string;
  transcription_model_revision: string | null;
  translation_prompt_name: string;
  translation_prompt_version: number | null;
  is_test: boolean;
  transcript_job_id: string | null;
  translation_job_id: string | null;
}

export interface TranslationGenerationItem {
  id: string;
  text: string;
  segment_index?: number;
  batch_index?: number;
}

export interface TranslationGenerationItemsPayload {
  generation: Record<string, unknown>;
  items: TranslationGenerationItem[];
  batches: Record<string, unknown>[];
}

export interface TranslationItemUpdatePayload {
  generation: Record<string, unknown>;
  subtitle_generation: Record<string, unknown> | null;
  item: TranslationGenerationItem;
}

export interface SubtitleTimelineCueInput {
  id: string | null;
  start: number;
  end: number;
  speaker: string;
  sourceText: string;
  text: string;
}

export interface ListPayload<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
}

export interface LibraryProgressSegment {
  state: "done" | "running" | "waiting" | "attention" | "unprocessed";
  percent: number;
}

export interface LibraryProgressItem {
  name: string;
  path: string;
  image_path: string | null;
  done: number;
  total: number;
  remaining: number;
  active: number;
  attention: number;
  segments: LibraryProgressSegment[];
}

export interface MediaDurationMetricGroup {
  media_duration_bucket_minutes: number;
  phase: "transcription" | "translation";
  runtime_id: string | null;
  runtime_name: string | null;
  outcome: string;
  sample_count: number;
  processing_total_seconds: number;
  processing_average_seconds: number;
  processing_minimum_seconds: number;
  processing_p50_seconds: number;
  processing_p95_seconds: number;
  processing_maximum_seconds: number;
  media_average_seconds: number;
  media_minimum_seconds: number;
  media_maximum_seconds: number;
}

export interface TranslationPassMetric {
  media_duration_bucket_minutes: number;
  pass: "draft" | "review";
  outcome: string;
  sample_count: number;
  active_total_seconds: number;
  active_average_seconds: number;
  active_minimum_seconds: number;
  active_p50_seconds: number;
  active_p95_seconds: number;
  active_maximum_seconds: number;
  request_total_seconds: number;
  request_count: number;
  media_average_seconds: number;
  media_minimum_seconds: number;
  media_maximum_seconds: number;
}

export interface MediaDurationMetricsPayload {
  schema_version: number;
  generated_at: number;
  window_start: number;
  window_end: number;
  window_seconds: number;
  bucket_interval_minutes: number;
  bucket_strategy: string;
  unmatched_terminal_events: number;
  missing_media_duration_events: number;
  groups: MediaDurationMetricGroup[];
  invalid_translation_pass_events: number;
  translation_passes: TranslationPassMetric[];
}

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let detail = `Backend 응답 ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      // JSON 오류 본문이 없으면 HTTP 상태를 그대로 쓴다.
    }
    throw new ApiError(detail, response.status);
  }
  return response.status === 204 ? (null as T) : ((await response.json()) as T);
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

export const api = {
  dashboard: () => request<DashboardPayload>("/dashboard"),

  mediaDurationMetrics: (windowDays = 30) =>
    request<MediaDurationMetricsPayload>(
      `/operations/metrics/media-durations?window_days=${encodeURIComponent(windowDays)}`,
    ),

  jobs: (params: {
    limit?: number;
    offset?: number;
    state?: readonly string[];
    phase?: readonly string[];
    operation?: readonly string[];
    reasonCode?: readonly string[];
  } = {}) => {
    const query = new URLSearchParams();
    if (params.limit != null) query.set("limit", String(params.limit));
    if (params.offset != null) query.set("offset", String(params.offset));
    for (const value of params.state ?? []) query.append("state", value);
    for (const value of params.phase ?? []) query.append("phase", value);
    for (const value of params.operation ?? []) query.append("operation", value);
    for (const value of params.reasonCode ?? []) query.append("reason_code", value);
    const suffix = query.toString();
    return request<ListPayload<JobListItem>>(`/jobs${suffix ? `?${suffix}` : ""}`);
  },

  retryJobs: (jobIds: string[]) =>
    request<unknown>("/jobs/actions/retry", { method: "POST", ...json({ job_ids: jobIds }) }),
  stopJobs: (jobIds: string[]) =>
    request<unknown>("/jobs/actions/stop", { method: "POST", ...json({ job_ids: jobIds }) }),
  pauseTranslations: (jobIds: string[]) =>
    request<unknown>("/jobs/actions/pause-translations", {
      method: "POST",
      ...json({ job_ids: jobIds }),
    }),
  translateJobs: (
    jobIds: string[],
    promptCategoryId: string,
    translationMode: TranslationMode,
  ) =>
    request<unknown>("/jobs/actions/translate", {
      method: "POST",
      ...json({
        job_ids: jobIds,
        prompt_category_id: promptCategoryId,
        translation_mode: translationMode,
      }),
    }),
  draftTranslateJobs: (jobIds: string[], promptCategoryId: string) =>
    request<unknown>("/jobs/actions/draft-translate", {
      method: "POST",
      ...json({ job_ids: jobIds, prompt_category_id: promptCategoryId, translation_mode: "draft_only" }),
    }),
  reviewTranslateJobs: (jobIds: string[], promptCategoryId: string) =>
    request<unknown>("/jobs/actions/review-translate", {
      method: "POST",
      ...json({ job_ids: jobIds, prompt_category_id: promptCategoryId, translation_mode: "review_existing" }),
    }),
  externalReviewJobs: (
    jobIds: string[],
    provider: ExternalModelProvider,
    model: string,
  ) => request<unknown>("/jobs/actions/external-review", {
    method: "POST",
    ...json({ job_ids: jobIds, provider, model }),
  }),
  retryJob: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" }),
  stopJob: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/stop`, { method: "POST" }),
  pauseTranslation: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/pause-translation`, { method: "POST" }),
  resumeTranslation: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/resume-translation`, {
      method: "POST",
    }),
  deleteJob: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }),
  publishSubtitleGeneration: (jobId: string, generationId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/subtitle-generations/publish`, {
      method: "POST",
      ...json({ generation_id: generationId }),
    }),

  media: (params: {
    folder?: string;
    q?: string;
    actor?: string;
    folderSort?: string;
    fileSort?: string;
    folderOffset?: number;
    folderLimit?: number;
  } = {}) => {
    const query = new URLSearchParams();
    if (params.folder) query.set("folder", params.folder);
    if (params.q) query.set("q", params.q);
    if (params.actor) query.set("actor", params.actor);
    if (params.folderSort) query.set("folder_sort", params.folderSort);
    if (params.fileSort) query.set("file_sort", params.fileSort);
    if (params.folderOffset != null) query.set("folder_offset", String(params.folderOffset));
    if (params.folderLimit != null) query.set("folder_limit", String(params.folderLimit));
    const suffix = query.toString();
    return request<MediaListing>(`/media${suffix ? `?${suffix}` : ""}`);
  },

  promptCategories: () =>
    request<ListPayload<PromptCategory>>("/settings/prompt-categories"),
  createPromptCategory: (body: {
    name: string;
    translation_prompt: string;
    review_prompt: string;
  }) => request<PromptCategory>("/settings/prompt-categories", { method: "POST", ...json(body) }),
  updatePromptCategory: (
    id: string,
    body: { name: string; translation_prompt: string; review_prompt: string },
  ) => request<PromptCategory>(`/settings/prompt-categories/${encodeURIComponent(id)}`, { method: "PUT", ...json(body) }),
  setPromptCategoryArchived: (id: string, archived: boolean) =>
    request<PromptCategory>(`/settings/prompt-categories/${encodeURIComponent(id)}`, {
      method: "PATCH",
      ...json({ archived }),
    }),

  createJobs: (body: {
    source_rels: string[];
    folder_rels?: string[];
    operation?: string;
    prompt_category_id?: string | null;
    force_overwrite?: boolean;
    is_test?: boolean;
    options?: Record<string, unknown>;
  }) => request<unknown>("/jobs", { method: "POST", ...json(body) }),

  settings: () => request<SettingsPayload>("/settings"),
  libraryProgress: () =>
    request<ListPayload<LibraryProgressItem>>("/dashboard/library-progress"),
  updateServers: (body: {
    stt_base_url: string;
    stt_token?: string | null;
  }) => request<unknown>("/settings/servers", { method: "PUT", ...json(body) }),
  updateExternalModel: (
    provider: ExternalModelProvider,
    body: {
      base_url: string;
      credential?: string | null;
      clear_credential?: boolean;
      region: string;
    },
  ) => request<ExternalModelProfile>(
    `/settings/external-models/${provider}`,
    { method: "PUT", ...json(body) },
  ),
  probeExternalModel: (provider: ExternalModelProvider) =>
    request<ExternalModelProfile>(
      `/settings/external-models/${provider}/probe`,
      { method: "POST" },
    ),
  selectExternalModel: (
    provider: ExternalModelProvider,
    model: string,
  ) => request<ExternalModelProfile>(
    `/settings/external-models/${provider}/model`,
    { method: "PUT", ...json({ model }) },
  ),
  updateTranslationServerModel: (
    stage: TranslationStage,
    id: string,
    model: string,
  ) => request<TranslationServer>(
    `/translation-groups/${stage}/servers/${encodeURIComponent(id)}/model`,
    { method: "PUT", ...json({ model }) },
  ),
  createTranslationEndpoint: (stage: TranslationStage, body: {
    name: string;
    base_url: string;
    token: string;
    enabled: boolean;
    capacity: number;
    resource_group_id: string;
    thinking_enabled: boolean;
  }) => request<TranslationServer>(`/translation-groups/${stage}/servers`, {
    method: "POST",
    ...json(body),
  }),
  updateTranslationEndpoint: (
    stage: TranslationStage,
    id: string,
    body: {
      name: string;
      base_url: string;
      token?: string | null;
      clear_token?: boolean;
      enabled: boolean;
      capacity: number;
      resource_group_id: string;
      thinking_enabled: boolean;
    },
  ) => request<TranslationServer>(
    `/translation-groups/${stage}/servers/${encodeURIComponent(id)}`,
    { method: "PUT", ...json(body) },
  ),
  deleteTranslationEndpoint: (stage: TranslationStage, id: string) => request<unknown>(
    `/translation-groups/${stage}/servers/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  ),
  probeTranslationEndpoint: (stage: TranslationStage, id: string) => request<TranslationServer>(
    `/translation-groups/${stage}/servers/${encodeURIComponent(id)}/probe`,
    { method: "POST" },
  ),
  updateTranslationEndpointRouting: (
    stage: TranslationStage,
    id: string,
    body: {
      enabled: boolean;
      batch_preferred: boolean;
    },
  ) => request<TranslationServer>(
    `/translation-groups/${stage}/servers/${encodeURIComponent(id)}/routing`,
    { method: "PUT", ...json(body) },
  ),
  createPathDisplayRule: (body: {
    source_pattern: string;
    display_pattern: string;
  }) => request<PathDisplayRule>("/settings/path-display-rules", {
    method: "POST",
    ...json(body),
  }),
  updatePathDisplayRule: (
    id: string,
    body: { source_pattern: string; display_pattern: string },
  ) => request<PathDisplayRule>(
    `/settings/path-display-rules/${encodeURIComponent(id)}`,
    { method: "PUT", ...json(body) },
  ),
  deletePathDisplayRule: (id: string) => request<unknown>(
    `/settings/path-display-rules/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  ),

  job: (id: string) => request<JobDetailPayload>(`/jobs/${encodeURIComponent(id)}`),

  translationGenerationItems: (jobId: string, generationId: string) =>
    request<TranslationGenerationItemsPayload>(
      `/jobs/${encodeURIComponent(jobId)}/translation-generations/${encodeURIComponent(generationId)}/items`,
    ),
  updateTranslationItem: (
    jobId: string,
    generationId: string,
    segmentId: string,
    text: string,
  ) => request<TranslationItemUpdatePayload>(
    `/jobs/${encodeURIComponent(jobId)}/translation-generations/${encodeURIComponent(generationId)}/items/${encodeURIComponent(segmentId)}`,
    { method: "PUT", ...json({ text }) },
  ),
  updateSubtitleTimeline: (
    jobId: string,
    generationId: string,
    cues: SubtitleTimelineCueInput[],
  ) => request<{
    transcript_revision: Record<string, unknown>;
    generation: Record<string, unknown>;
    subtitle_generation: Record<string, unknown> | null;
  }>(
    `/jobs/${encodeURIComponent(jobId)}/translation-generations/${encodeURIComponent(generationId)}/timeline`,
    {
      method: "PUT",
      ...json({
        cues: cues.map((cue) => ({
          id: cue.id,
          start: cue.start,
          end: cue.end,
          speaker: cue.speaker,
          source_text: cue.sourceText,
          text: cue.text,
        })),
      }),
    },
  ),

  jobEvents: (id: string) =>
    request<{ items: JobEvent[] }>(
      `/jobs/${encodeURIComponent(id)}/events?limit=200`,
    ),

  /** 전사(일본어) / 번역(한국어) 산출물. 없으면 null. */
  artifact: async (id: string, kind: "transcript" | "translation"): Promise<unknown | null> => {
    const response = await fetch(
      `/api/v1/jobs/${encodeURIComponent(id)}/artifacts/${kind}`,
      { headers: { Accept: "application/json" } },
    );
    if (response.status === 404) return null;
    if (!response.ok) throw new ApiError(`산출물 응답 ${response.status}`, response.status);
    try {
      return (await response.json()) as unknown;
    } catch {
      throw new ApiError("산출물 JSON을 읽지 못했습니다.", response.status);
    }
  },

  mediaFileUrl: (sourceRel: string) => `/api/v1/media/file?path=${encodeURIComponent(sourceRel)}`,
  posterUrl: (posterPath: string) =>
    `/api/v1/media/posters/${posterPath.split("/").map(encodeURIComponent).join("/")}`,
  actorImageUrl: (actorPath: string) =>
    `/api/v1/media/actors/${actorPath.split("/").map(encodeURIComponent).join("/")}`,
  subtitlesUrl: (id: string) => `/api/v1/jobs/${encodeURIComponent(id)}/subtitles.vtt`,

  transcribers: () => request<ListPayload<TranscriberEndpoint>>("/transcribers"),
  createTranscriber: (body: {
    name: string;
    base_url: string;
    token: string;
    enabled: boolean;
    capacity: number;
    resource_group_id: string;
    kotoba_batch_size?: number | null;
    whisperx_batch_size?: number | null;
  }) => request<TranscriberEndpoint>("/transcribers", { method: "POST", ...json(body) }),
  updateTranscriber: (
    id: string,
    body: {
      name: string;
      base_url: string;
      token?: string | null;
      clear_token?: boolean;
      enabled: boolean;
      capacity: number;
      resource_group_id: string;
      kotoba_batch_size?: number | null;
      whisperx_batch_size?: number | null;
      clear_kotoba_batch_size?: boolean;
      clear_whisperx_batch_size?: boolean;
    },
  ) =>
    request<TranscriberEndpoint>(`/transcribers/${encodeURIComponent(id)}`, {
      method: "PUT",
      ...json(body),
    }),
  deleteTranscriber: (id: string) =>
    request<unknown>(`/transcribers/${encodeURIComponent(id)}`, { method: "DELETE" }),
  probeTranscriber: (id: string) =>
    request<unknown>(`/transcribers/${encodeURIComponent(id)}/probe`, { method: "POST" }),
  setTranslationFeedbackIncluded: (id: string, included: boolean) =>
    request<TranslationFeedback>(
      `/settings/translation-feedback/${encodeURIComponent(id)}`,
      { method: "PATCH", ...json({ included }) },
    ),
  createPromptImprovement: (body: {
    category_id: string;
    stage: "translation" | "review";
    provider: ExternalModelProvider;
    model: string;
  }) => request<PromptImprovementRun>("/settings/prompt-improvements", {
    method: "POST",
    ...json(body),
  }),
  createPromptDraft: (body: {
    name: string;
    domain_description: string;
    provider: ExternalModelProvider;
    model: string;
  }) => request<PromptDraft>("/settings/prompt-drafts", {
    method: "POST",
    ...json(body),
  }),
  cancelPromptImprovement: (id: string) => request<PromptImprovementRun>(
    `/settings/prompt-improvements/${encodeURIComponent(id)}/cancel`,
    { method: "POST" },
  ),
  rejectPromptImprovement: (id: string) => request<PromptImprovementRun>(
    `/settings/prompt-improvements/${encodeURIComponent(id)}/reject`,
    { method: "POST" },
  ),
  activatePromptImprovement: (id: string) => request<unknown>(
    `/settings/prompt-improvements/${encodeURIComponent(id)}/activate`,
    { method: "POST" },
  ),
};
