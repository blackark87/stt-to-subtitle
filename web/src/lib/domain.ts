/**
 * 백엔드 job_state.py 의 열거형을 그대로 옮긴 것.
 *
 * 라벨·색은 전부 Record<...> 전수 맵으로 둔다. 상태가 하나라도 빠지면
 * 타입 에러가 나므로, 구 UI 에서 waiting 이 네 곳에서 스타일 없이 방치되고
 * paused/stopped 배지가 무색이던 결함(N1·N2)이 구조적으로 재발할 수 없다.
 * 백엔드에 상태가 추가되면 여기서 즉시 드러난다.
 */

export const JOB_STATES = [
  "waiting",
  "running",
  "paused",
  "blocked",
  "stopped",
  "failed",
  "done",
] as const;
export type JobState = (typeof JOB_STATES)[number];

export const JOB_PHASES = [
  "extraction",
  "transcription",
  "translation",
  "draft_translation",
  "review_translation",
  "external_review",
  "render",
  "complete",
] as const;
export type JobPhase = (typeof JOB_PHASES)[number];

export const PUBLIC_JOB_PHASES: readonly JobPhase[] = [
  "transcription",
  "draft_translation",
  "review_translation",
  "external_review",
];

export const JOB_OPERATIONS = [
  "extract",
  "transcribe",
  "translate",
  "full",
  "draft_translate",
  "review_translate",
  "external_review",
] as const;
export type JobOperation = (typeof JOB_OPERATIONS)[number];

export const PUBLIC_JOB_OPERATIONS: readonly JobOperation[] = [
  "transcribe",
  "draft_translate",
  "review_translate",
  "external_review",
];

export const JOB_REASONS = [
  "user_stop",
  "lm_unavailable",
  "draft_translation_unavailable",
  "review_translation_unavailable",
  "external_model_unavailable",
  "stt_unavailable",
  "service_restarted",
  "artifact_missing",
  "model_output_invalid",
  "invalid_input",
  "auth_required",
  "resource_exhausted",
  "transcription_processing_error",
  "internal_error",
] as const;
export type JobReason = (typeof JOB_REASONS)[number];

export const STATE_LABEL: Record<JobState, string> = {
  waiting: "대기",
  running: "진행 중",
  paused: "일시 정지",
  blocked: "중단",
  stopped: "정지",
  failed: "실패",
  done: "완료",
};

/** 상태색 CSS 변수. globals.css 의 검증된 팔레트를 가리킨다. */
export const STATE_COLOR_VAR: Record<JobState, string> = {
  waiting: "var(--color-st-waiting)",
  running: "var(--color-st-running)",
  paused: "var(--color-st-paused)",
  blocked: "var(--color-st-blocked)",
  stopped: "var(--color-st-stopped)",
  failed: "var(--color-st-failed)",
  done: "var(--color-st-done)",
};

/** 대시보드 요약과 필터에서 쓰는 표시 순서. 운영상 급한 것부터. */
export const STATE_ORDER: readonly JobState[] = [
  "running",
  "waiting",
  "blocked",
  "failed",
  "paused",
  "stopped",
  "done",
];

/** 사용자가 손을 대야 하는 상태. 대시보드에서 전면에 노출한다. */
export const ATTENTION_STATES: readonly JobState[] = [
  "blocked",
  "failed",
  "paused",
  "stopped",
];

export const PHASE_LABEL: Record<JobPhase, string> = {
  extraction: "추출",
  transcription: "전사",
  translation: "번역",
  draft_translation: "1차 번역",
  review_translation: "2차 번역",
  external_review: "외부 모델 검토",
  render: "자막 생성",
  complete: "작업 종료",
};

export const OPERATION_LABEL: Record<JobOperation, string> = {
  extract: "음원 추출",
  transcribe: "전사",
  translate: "번역·자막 생성",
  full: "전체 자막 생성",
  draft_translate: "1차 번역",
  review_translate: "2차 번역",
  external_review: "외부 모델 검토",
};

export const OPERATION_COMPLETION_LABEL: Record<JobOperation, string> = {
  extract: "음원 추출 완료",
  transcribe: "전사 완료",
  translate: "번역·자막 생성 완료",
  full: "자막 생성 완료",
  draft_translate: "1차 번역 완료",
  review_translate: "2차 번역 완료",
  external_review: "외부 모델 검토 완료",
};

export const TRANSCRIPTION_STAGE_LABEL: Record<string, string> = {
  model_loading: "모델 준비",
  scene_detection: "장면 분석",
  primary_transcription: "1차 전사",
  secondary_transcription: "2차 전사",
  forced_alignment: "강제 정렬",
  speaker_diarization: "화자 분리",
  quality_analysis: "문제 구간 분석",
  rescue_transcription: "문제 구간 재전사",
  transcription_merge: "전사 결과 병합",
  subtitle_normalization: "자막 구간 구성",
};

export const REASON_LABEL: Record<JobReason, string> = {
  user_stop: "사용자 정지",
  lm_unavailable: "번역 서버 연결 불가",
  draft_translation_unavailable: "1차 번역 서버 연결 불가",
  review_translation_unavailable: "2차 번역 서버 연결 불가",
  external_model_unavailable: "외부 검토 모델 연결 불가",
  stt_unavailable: "전사 서버 연결 불가",
  service_restarted: "서비스 재시작으로 중단",
  artifact_missing: "산출물 누락",
  model_output_invalid: "모델 출력 형식 오류",
  invalid_input: "입력 오류",
  auth_required: "인증 필요",
  resource_exhausted: "자원 부족",
  transcription_processing_error: "전사 처리 오류",
  internal_error: "내부 오류",
};

export const RUNTIME_STATUSES = [
  "ready",
  "checking",
  "unknown",
  "unavailable",
  "disabled",
] as const;
export type RuntimeStatus = (typeof RUNTIME_STATUSES)[number];

export const RUNTIME_STATUS_LABEL: Record<RuntimeStatus, string> = {
  ready: "사용 가능",
  checking: "확인 중",
  unknown: "확인 필요",
  unavailable: "연결 불가",
  disabled: "사용 안 함",
};

/** ready/unavailable 을 상태 팔레트에 대응시킨다. 색을 새로 만들지 않는다. */
export const RUNTIME_STATUS_TONE: Record<RuntimeStatus, JobState> = {
  ready: "done",
  checking: "running",
  unknown: "waiting",
  unavailable: "failed",
  disabled: "stopped",
};

/**
 * 백엔드가 모르는 값을 보내도 화면이 깨지지 않게 좁혀 준다.
 * 조용히 넘기지 않고 알 수 없는 값임을 호출부가 알 수 있도록 null 을 준다.
 */
export function asJobState(value: string): JobState | null {
  return (JOB_STATES as readonly string[]).includes(value)
    ? (value as JobState)
    : null;
}

export function asJobPhase(value: string): JobPhase | null {
  return (JOB_PHASES as readonly string[]).includes(value)
    ? (value as JobPhase)
    : null;
}

export function asRuntimeStatus(value: string): RuntimeStatus | null {
  return (RUNTIME_STATUSES as readonly string[]).includes(value)
    ? (value as RuntimeStatus)
    : null;
}

export function stateLabel(value: string): string {
  const state = asJobState(value);
  return state ? STATE_LABEL[state] : value;
}

export function phaseLabel(value: string): string {
  const phase = asJobPhase(value);
  return phase ? PHASE_LABEL[phase] : value;
}

export function operationLabel(value: string): string {
  return (JOB_OPERATIONS as readonly string[]).includes(value)
    ? OPERATION_LABEL[value as JobOperation]
    : value;
}

export function operationCompletionLabel(value: string): string {
  return (JOB_OPERATIONS as readonly string[]).includes(value)
    ? OPERATION_COMPLETION_LABEL[value as JobOperation]
    : "작업 완료";
}

export function transcriptionStageLabel(value: string | null): string | null {
  if (!value) return null;
  return TRANSCRIPTION_STAGE_LABEL[value] ?? value;
}

export function reasonLabel(value: string | null): string | null {
  if (!value) return null;
  return (JOB_REASONS as readonly string[]).includes(value)
    ? REASON_LABEL[value as JobReason]
    : value;
}

export function canRetryJob(state: JobState | null): boolean {
  return state === "blocked" || state === "failed" || state === "stopped";
}

export function canStopJob(state: JobState | null): boolean {
  return state === "waiting" || state === "running";
}

export function canPauseTranslation(state: JobState | null, phase: string): boolean {
  return state === "running" && [
    "translation",
    "draft_translation",
    "review_translation",
    "external_review",
  ].includes(phase);
}

export function canResumeTranslation(state: JobState | null): boolean {
  return state === "paused";
}
