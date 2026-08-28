import type { JobEvent, PipelineJob, TranslationMode } from "@/lib/api";
import {
  PHASE_LABEL,
  STATE_LABEL,
  asJobPhase,
  asJobState,
  operationCompletionLabel,
  operationLabel,
  reasonLabel,
  transcriptionStageLabel,
} from "@/lib/domain";

export const EVENT_LEVEL_LABEL: Record<string, string> = {
  info: "정보",
  warning: "주의",
  error: "오류",
};

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function phaseName(value: string | null | undefined): string {
  const phase = asJobPhase(value ?? "");
  return phase ? PHASE_LABEL[phase] : "작업";
}

function stateName(value: unknown): string {
  const raw = text(value);
  const state = asJobState(raw);
  if (state) return STATE_LABEL[state];
  const legacy: Record<string, string> = {
    queued: "대기",
    audio_ready: "전사 대기",
    transcribed: "번역 대기",
    translated: "자막 생성 대기",
    transcription_completed: "전사 완료",
    audio_completed: "음원 추출 완료",
    completed: "자막 완료",
  };
  return legacy[raw] ?? "저장된 중간 지점";
}

export function jobTranslationMode(job: PipelineJob): TranslationMode {
  const rawSnapshot = job.options.translation_prompt;
  const snapshot = rawSnapshot && typeof rawSnapshot === "object"
    ? rawSnapshot as Record<string, unknown>
    : {};
  const explicit = text(snapshot.translation_mode);
  if (explicit === "draft_only" || explicit === "review_existing" || explicit === "draft_and_review") {
    return explicit;
  }
  if (text(snapshot.target_stage) === "draft" || number(snapshot.review_rounds) === 0) {
    return "draft_only";
  }
  return "draft_and_review";
}

export function jobStateLabel(job: PipelineJob): string {
  if (
    job.state === "done"
    && (job.operation === "translate" || job.operation === "full")
    && jobTranslationMode(job) === "draft_only"
  ) {
    return "1차 자막 완료";
  }
  if (job.state === "done") return operationCompletionLabel(job.operation);
  const state = asJobState(job.state);
  return state ? STATE_LABEL[state] : job.state;
}

function runtimeName(
  runtimeId: unknown,
  runtimeNames: ReadonlyMap<string, string>,
): string {
  const id = text(runtimeId);
  return runtimeNames.get(id) ?? (id || "미확인 서버");
}

function segmentSuffix(payload: Record<string, unknown>): string {
  const count = number(payload.segment_count);
  return count == null ? "" : ` · ${count}개 구간`;
}

function measuredPassDetails(payload: Record<string, unknown>): string {
  const details: string[] = [];
  const requestCount = number(payload.request_count);
  if (requestCount != null) details.push(`모델 요청 ${requestCount}회`);
  const activeSeconds = number(payload.active_seconds);
  if (activeSeconds != null) {
    const total = Math.max(0, Math.round(activeSeconds));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const seconds = total % 60;
    const duration = hours > 0
      ? `${hours}시간 ${minutes}분 ${seconds}초`
      : `${minutes}분 ${seconds}초`;
    details.push(`처리 시간 ${duration}`);
  }
  return details.length > 0 ? ` · ${details.join(" · ")}` : "";
}

function genericMessage(message: string, eventCode: string | undefined): string {
  if (message === "job queued") return "작업을 대기열에 등록했습니다.";
  const created = message.match(/^job created in (\S+)$/);
  if (created) return `작업을 등록했습니다. 시작 지점: ${stateName(created[1])}`;

  if (message === "audio extraction started") return "음원 추출을 시작했습니다.";
  const extraction = message.match(
    /^audio extraction completed \(\d+ bytes(?:; [\d.]+s; approximately (\d+) transcription chunk\(s\))?\)$/,
  );
  if (extraction) {
    return extraction[1]
      ? `음원 추출을 완료했습니다. 예상 전사 구간: ${extraction[1]}개`
      : "음원 추출을 완료했습니다.";
  }

  const stageStarted = message.match(/^(transcription|translation|render) started$/);
  if (stageStarted) {
    const stage = stageStarted[1] ?? "";
    const labels: Record<string, string> = {
      transcription: "전사",
      translation: "번역",
      render: "자막 생성",
    };
    return `${labels[stage] ?? "작업"} 작업을 시작했습니다.`;
  }
  const stageCompleted = message.match(/^(transcription|translation) completed \((\d+) segments\)$/);
  if (stageCompleted) {
    const label = stageCompleted[1] === "transcription" ? "전사" : "번역";
    return `${label}를 완료했습니다. ${stageCompleted[2]}개 구간`;
  }
  const remoteAccepted = message.match(/^remote transcription job accepted: (\S+)$/);
  if (remoteAccepted) {
    return `전사 서버에 작업이 접수되었습니다. 원격 작업 ID: ${remoteAccepted[1]}`;
  }
  if (message === "job stopped by user request") {
    return "사용자 요청으로 작업을 정지했습니다.";
  }
  if (message.startsWith("subtitles written:")) {
    return "SRT·ASS 자막 파일을 저장했습니다.";
  }
  if (message.startsWith("legacy translation job(s) merged:")) {
    return "이전 번역 작업 기록을 현재 작업에 통합했습니다.";
  }

  const noise = message.match(/noise filter removed (\d+) non-speech diarization span/);
  if (noise) return `잡음 필터가 비음성 화자 구간 ${noise[1]}개를 제거했습니다.`;
  if (message.startsWith("translation review failed; using initial translation")) {
    return "번역 검토에 실패해 최초 번역 결과를 사용했습니다.";
  }
  if (message.includes("transcription requested; reusing extracted audio")) {
    return "추출된 음원을 재사용해 전사를 요청했습니다.";
  }
  if (message.includes("transcription requested; audio will be extracted again")) {
    return "음원을 다시 추출한 뒤 전사하도록 요청했습니다.";
  }
  if (message.includes("translation requested; reusing validated transcript")) {
    return "검증된 전사 결과를 재사용해 번역을 요청했습니다.";
  }
  if (message.includes("translation requested; continuing completed transcription")) {
    return "완료된 전사 결과에 이어 같은 작업에서 번역을 요청했습니다.";
  }
  if (message.includes("selected completed transcription continued in translation queue")) {
    return "선택한 전사 완료 작업을 번역 대기열로 보냈습니다.";
  }
  if (message.includes("selected completed first-pass translation continued in review queue")) {
    return "완료된 1차 자막을 2차 보정 대기열로 보냈습니다.";
  }
  if (message.includes("selected translation requested; reusing completed transcript")) {
    return "완료된 전사 결과를 재사용해 선택 번역을 요청했습니다.";
  }
  if (message.startsWith("translation requested from comparison transcript")) {
    return "비교 전사 결과로 번역을 요청했습니다.";
  }
  if (message.startsWith("comparison rerun requested; reusing extracted audio")) {
    return "추출된 음원을 재사용해 비교 전사를 다시 요청했습니다.";
  }
  if (message === "legacy WhisperX chunk length reduced to 30 seconds for retry") {
    return "이전 WhisperX 작업의 재시도를 위해 구간 길이를 30초로 조정했습니다.";
  }
  if (message.startsWith("remote transcription cancellation is pending:")) {
    return "전사 서버의 작업 취소 확인을 기다리고 있습니다.";
  }
  const ignoredCheckpoints = message.match(/^ignored (\d+) stale translation checkpoint id/);
  if (ignoredCheckpoints) {
    return `사용할 수 없는 이전 번역 저장 지점 ${ignoredCheckpoints[1]}개를 제외했습니다.`;
  }
  const repairedTimestamps = message.match(
    /^repaired (\d+) legacy or abnormal subtitle timestamp/,
  );
  if (repairedTimestamps) {
    return `이전 형식이거나 비정상적인 자막 시간 ${repairedTimestamps[1]}개를 보정했습니다.`;
  }
  if (message === "transcript JSON edited; subtitle regenerated") {
    return "전사 JSON을 수정하고 자막을 다시 생성했습니다.";
  }
  if (message === "translation JSON edited; subtitle regenerated") {
    return "번역 JSON을 수정하고 자막을 다시 생성했습니다.";
  }
  if (message === "transcript JSON edited") return "전사 JSON을 수정했습니다.";
  if (message === "translation JSON edited") return "번역 JSON을 수정했습니다.";
  if (/[가-힣]/.test(message)) return message;
  return eventCode && eventCode !== "job.message"
    ? `지원되지 않는 작업 기록입니다. 기록 코드: ${eventCode}`
    : "세부 정보가 없는 이전 형식의 작업 기록입니다.";
}

function stateTransition(event: JobEvent): string {
  // Pass measurements describe one sub-pass while the parent translation
  // stage is still running. Showing that transient state on a completed pass
  // makes historical records look contradictory after the job completes.
  if (event.event_code === "translation.pass.measured") return "";
  const fromState = text(event.from_state);
  const toState = text(event.to_state);
  if (!toState || fromState === toState) return "";
  if (!fromState) return `작업 상태: ${stateName(toState)}`;
  return `작업 상태: ${stateName(fromState)} → ${stateName(toState)}`;
}

export function jobProgressLabel(job: PipelineJob): string {
  if (job.state === "done") return jobStateLabel(job);
  const phase = phaseName(job.phase);
  if (job.phase !== "transcription") return phase;
  const stage = transcriptionStageLabel(job.transcription_stage);
  if (!stage) return phase;
  const position = job.transcription_stage_total > 0
    ? ` (${job.transcription_stage_index}/${job.transcription_stage_total})`
    : "";
  return `${phase} · ${stage}${position}`;
}

export function eventText(
  event: JobEvent,
  runtimeNames: ReadonlyMap<string, string>,
): string {
  const description = eventDescription(event, runtimeNames);
  const transition = stateTransition(event);
  return transition ? `${description} · ${transition}` : description;
}

function eventDescription(
  event: JobEvent,
  runtimeNames: ReadonlyMap<string, string>,
): string {
  const payload = event.payload ?? {};
  const phase = phaseName(event.phase);
  const rawReason = text(payload.reason_code);
  const translatedReason = reasonLabel(rawReason);
  const reason = translatedReason && (translatedReason !== rawReason || /[가-힣]/.test(translatedReason))
    ? translatedReason
    : "원인 확인 필요";

  switch (event.event_code) {
    case "job.created":
      return `${operationLabel(text(payload.operation) || "full")} 작업을 등록했습니다.`;
    case "stage.started": {
      const server = event.phase === "transcription" && payload.runtime_id
        ? ` · 전사 서버: ${runtimeName(payload.runtime_id, runtimeNames)}`
        : "";
      return `${phase} 작업을 시작했습니다${server}.`;
    }
    case "transcription.stage_changed": {
      const stage = transcriptionStageLabel(text(payload.stage)) ?? "내부 처리";
      const index = number(payload.stage_index);
      const total = number(payload.stage_total);
      const position = index != null && total != null ? ` (${index}/${total})` : "";
      const server = payload.runtime_id
        ? ` · 전사 서버: ${runtimeName(payload.runtime_id, runtimeNames)}`
        : "";
      return `전사 세부 단계: ${stage}${position}${server}`;
    }
    case "transcription.remote_accepted":
      return `전사 서버 ${runtimeName(payload.runtime_id, runtimeNames)}에 작업이 접수되었습니다.`;
    case "transcription.reconnected":
      return `서비스 재시작 후 전사 서버 ${runtimeName(payload.runtime_id, runtimeNames)}의 작업에 다시 연결했습니다.`;
    case "transcription.runtime_failover":
      return `전사 서버 ${runtimeName(payload.failed_runtime_id, runtimeNames)}의 연결이 끊겨 다른 가용 서버에 자동 재배정합니다.`;
    case "stage.completed": {
      if (event.phase === "extraction") {
        return payload.reused ? "기존 음원을 재사용해 추출 단계를 완료했습니다." : "음원 추출을 완료했습니다.";
      }
      if (event.phase === "transcription") return `전사를 완료했습니다${segmentSuffix(payload)}.`;
      if (event.phase === "translation") return `번역을 완료했습니다${segmentSuffix(payload)}.`;
      if (event.phase === "render") return "SRT·ASS 자막 생성을 완료했습니다.";
      return `${phase} 단계를 완료했습니다.`;
    }
    case "stage.blocked":
      return `${phase} 작업이 중단되었습니다. 사유: ${reason}`;
    case "stage.failed":
      return `${phase} 작업이 실패했습니다. 사유: ${reason}`;
    case "stage.paused":
      return "번역 작업을 안전 지점에서 일시 정지했습니다.";
    case "job.stop_requested":
      return "사용자가 작업 정지를 요청했습니다. 안전한 중단 지점을 기다리고 있습니다.";
    case "job.stopped":
      return "사용자 요청으로 작업을 정지했습니다.";
    case "job.retry_requested": {
      const target = stateName(payload.target_status);
      return target ? `수동 재시도를 요청했습니다. 재개 지점: ${target}` : "수동 재시도를 요청했습니다.";
    }
    case "translation.pause_requested":
      return "번역 일시 정지를 요청했습니다.";
    case "translation.resume_requested":
      return "번역 재개를 요청했습니다.";
    case "translation.generation_created": {
      const generation = number(payload.generation_number);
      return generation == null
        ? "새 번역 결과 생성을 시작했습니다."
        : `새 번역 결과 ${generation}번 생성을 시작했습니다.`;
    }
    case "translation.route.selected": {
      const executionMode = text(payload.execution_mode) === "batch" ? "배치" : "실시간";
      const draft = payload.draft_pass === false ? "생략" : "사용";
      const review = payload.local_review_pass === true ? "사용" : "생략";
      return `번역 경로를 확정했습니다. 실행 방식: ${executionMode} · 1차 번역: ${draft} · 2차 검수: ${review}`;
    }
    case "translation.pass.measured": {
      const pass = text(payload.pass);
      const label = pass === "review"
        ? "2차 번역 검수를"
        : pass === "draft"
          ? "1차 초벌 번역을"
          : "번역 처리를";
      const outcome = text(payload.outcome);
      const action: Record<string, string> = {
        completed: "완료했습니다",
        paused: "일시 정지했습니다",
        blocked: "중단됐습니다",
        failed: "실패했습니다",
      };
      return `${label} ${action[outcome] ?? "측정했습니다"}${measuredPassDetails(payload)}`;
    }
    case "recovery.checkpoint_resumed":
      return `서비스 재시작 후 저장된 지점에서 ${phase} 작업을 재개했습니다.`;
    case "recovery.stop_confirmation":
      return "서비스 재시작 후 요청된 전사 정지 상태를 확인하고 있습니다.";
    case "subtitle.published":
      return "선택한 자막 결과를 게시했습니다.";
    default:
      return genericMessage(event.message ?? "", event.event_code);
  }
}
