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
  options: Record<string, unknown>;
  stt_runtime_id: string | null;
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
}

export interface JobListItem extends PipelineJob {
  nfo_title: string | null;
  poster_path: string | null;
}

export interface RuntimeEndpoint {
  id: string;
  name: string;
  base_url: string;
  token_configured: boolean;
  enabled: boolean;
  capacity: number;
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

export interface TranslationServer {
  id: string;
  stage: TranslationStage;
  name: string;
  base_url: string;
  token_configured: boolean;
  enabled: boolean;
  capacity: number;
  builtin: boolean;
  batch_preferred: boolean;
  selected_model: string;
  models: string[];
  status: string;
  message: string | null;
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
  has_external_subtitle: boolean;
  external_subtitle_formats: string[];
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
  runtimes: RuntimeEndpoint[];
  translation_groups: TranslationGroup[];
  translation_groups_error: string | null;
  path_display_rules: PathDisplayRule[];
  prompt_categories: PromptCategory[];
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
  archived?: boolean;
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
  events: JobEvent[];
  transcript_revisions: Record<string, unknown>[];
  translation_generations: Record<string, unknown>[];
  subtitle_generations: Record<string, unknown>[];
  subtitle_validation: Record<string, unknown> | null;
  external_subtitles: string[];
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

  media: (params: {
    folder?: string;
    q?: string;
    actor?: string;
    folderSort?: string;
    fileSort?: string;
    folderLimit?: number;
  } = {}) => {
    const query = new URLSearchParams();
    if (params.folder) query.set("folder", params.folder);
    if (params.q) query.set("q", params.q);
    if (params.actor) query.set("actor", params.actor);
    if (params.folderSort) query.set("folder_sort", params.folderSort);
    if (params.fileSort) query.set("file_sort", params.fileSort);
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
  }) => request<unknown>("/jobs", { method: "POST", ...json(body) }),

  settings: () => request<SettingsPayload>("/settings"),
  libraryProgress: () =>
    request<ListPayload<LibraryProgressItem>>("/dashboard/library-progress"),
  updateServers: (body: {
    stt_base_url: string;
    stt_token?: string | null;
  }) => request<unknown>("/settings/servers", { method: "PUT", ...json(body) }),
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

  comparisons: () =>
    request<ListPayload<{ id: string; source_rels: string[]; jobs: PipelineJob[]; updated_at: number }>>(
      "/comparisons",
    ),
  comparison: (id: string) =>
    request<{ id: string; jobs: PipelineJob[] }>(`/comparisons/${encodeURIComponent(id)}`),
  retryComparison: (id: string) =>
    request<{ updated: number }>(`/comparisons/${encodeURIComponent(id)}/retry`, { method: "POST" }),

  runtimes: () => request<ListPayload<RuntimeEndpoint>>("/runtimes"),
  createRuntime: (body: {
    name: string;
    base_url: string;
    token: string;
    enabled: boolean;
    capacity: number;
    kotoba_batch_size?: number | null;
    whisperx_batch_size?: number | null;
  }) => request<RuntimeEndpoint>("/runtimes", { method: "POST", ...json(body) }),
  updateRuntime: (
    id: string,
    body: {
      name: string;
      base_url: string;
      token?: string | null;
      clear_token?: boolean;
      enabled: boolean;
      capacity: number;
      kotoba_batch_size?: number | null;
      whisperx_batch_size?: number | null;
      clear_kotoba_batch_size?: boolean;
      clear_whisperx_batch_size?: boolean;
    },
  ) =>
    request<RuntimeEndpoint>(`/runtimes/${encodeURIComponent(id)}`, {
      method: "PUT",
      ...json(body),
    }),
  deleteRuntime: (id: string) =>
    request<unknown>(`/runtimes/${encodeURIComponent(id)}`, { method: "DELETE" }),
  probeRuntime: (id: string) =>
    request<unknown>(`/runtimes/${encodeURIComponent(id)}/probe`, { method: "POST" }),
};
