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

export interface RuntimeEndpoint {
  id: string;
  name: string;
  base_url: string;
  token_configured: boolean;
  enabled: boolean;
  capacity: number;
  builtin: boolean;
  status: string;
  message: string | null;
  checked_at: number | null;
  running_jobs: number;
  available_slots: number;
}

export interface GpuDevice {
  index?: string;
  display_name?: string;
  utilization_percent: number | null;
  memory_percent: number | null;
  memory_used_gib: number | null;
  memory_total_gib: number | null;
  temperature_celsius: number | null;
  power_watts: number | null;
}

export interface GpuSnapshot {
  available: boolean;
  configured: boolean;
  devices: GpuDevice[];
}

export interface DependencyState {
  state?: string;
  reason_code?: string | null;
  detail?: string | null;
  [key: string]: unknown;
}

export interface DashboardPayload {
  state_counts: Record<string, number>;
  phase_counts: Record<string, number>;
  recent_jobs: PipelineJob[];
  dependencies: { transcription: DependencyState; translation: DependencyState };
  gpu: GpuSnapshot | null;
}

export interface ListPayload<T> {
  items: T[];
  total: number;
  limit?: number;
  offset?: number;
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

  jobs: (params: {
    limit?: number;
    offset?: number;
    state?: readonly string[];
    phase?: readonly string[];
  } = {}) => {
    const query = new URLSearchParams();
    if (params.limit != null) query.set("limit", String(params.limit));
    if (params.offset != null) query.set("offset", String(params.offset));
    for (const value of params.state ?? []) query.append("state", value);
    for (const value of params.phase ?? []) query.append("phase", value);
    const suffix = query.toString();
    return request<ListPayload<PipelineJob>>(`/jobs${suffix ? `?${suffix}` : ""}`);
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
  retryJob: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" }),
  stopJob: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/stop`, { method: "POST" }),
  resumeTranslation: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}/resume-translation`, {
      method: "POST",
    }),
  deleteJob: (jobId: string) =>
    request<unknown>(`/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" }),

  runtimes: () => request<ListPayload<RuntimeEndpoint>>("/runtimes"),
  createRuntime: (body: {
    name: string;
    base_url: string;
    token: string;
    enabled: boolean;
    capacity: number;
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
