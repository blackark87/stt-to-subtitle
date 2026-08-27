import type { JobEvent, PipelineJob } from "@/lib/api";
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

export function jobStateLabel(job: PipelineJob): string {
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

function genericMessage(message: string): string {
  const noise = message.match(/noise filter removed (\d+) non-speech diarization span/);
  if (noise) return `잡음 필터가 비음성 화자 구간 ${noise[1]}개를 제거했습니다.`;
  if (message === "translation review failed; using initial translation") {
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
  if (message === "transcript JSON edited") return "전사 JSON을 수정했습니다.";
  if (message === "translation JSON edited") return "번역 JSON을 수정했습니다.";
  if (/[가-힣]/.test(message)) return message;
  return "작업 상태가 갱신되었습니다.";
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
    case "recovery.checkpoint_resumed":
      return `서비스 재시작 후 저장된 지점에서 ${phase} 작업을 재개했습니다.`;
    case "recovery.stop_confirmation":
      return "서비스 재시작 후 요청된 전사 정지 상태를 확인하고 있습니다.";
    case "subtitle.published":
      return "선택한 자막 결과를 게시했습니다.";
    default:
      return genericMessage(event.message ?? "");
  }
}
