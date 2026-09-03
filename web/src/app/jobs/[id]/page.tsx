"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { LoadingOverlay } from "@/components/LoadingOverlay";
import { ResultPlayer } from "@/components/ResultPlayer";
import {
  SubtitleTimelineEditor,
  type SubtitleTimelineCue,
} from "@/components/SubtitleTimelineEditor";
import { api, type ExternalModelProvider, type JobDetailPayload } from "@/lib/api";
import {
  asJobState,
  canPauseTranslation,
  canResumeTranslation,
  canRetryJob,
  canStopJob,
  operationLabel,
  reasonLabel,
  type JobState,
} from "@/lib/domain";
import { clock, fileName, parentPath, percent } from "@/lib/format";
import { EVENT_LEVEL_LABEL, eventText, jobProgressLabel, jobStateLabel, jobTranslationMode } from "@/lib/jobPresentation";
import { useLiveQuery } from "@/lib/useLiveQuery";

const INTERVAL_MS = 5000;
const SUBTITLE_HISTORY_PREVIEW_LIMIT = 5;

const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

interface Segment {
  id: string;
  start: number;
  end: number;
  speaker: string;
  ja: string;
  ko: string;
}

interface ArtifactResult {
  version: string;
  segments: Segment[];
  error: string | null;
}

type TranslationPhaseKey = "draft" | "review" | "external";

interface TranslationGenerationRef {
  phase: TranslationPhaseKey;
  label: string;
  jobId: string;
  generationId: string;
}

interface TranslationPhaseResult extends TranslationGenerationRef {
  model: string;
  texts: Record<string, string>;
  subtitleGenerationId: string;
  isPublished: boolean;
  publishedSubtitleGenerationId: string;
}

interface TranslationLineageResult {
  version: string;
  phases: TranslationPhaseResult[];
  error: string | null;
}

type DiffPieceKind = "unchanged" | "added" | "removed";

interface DiffPiece {
  kind: DiffPieceKind;
  text: string;
}

interface TextDiff {
  before: DiffPiece[];
  after: DiffPiece[];
  changed: boolean;
}

function diffTokens(text: string): string[] {
  return text.match(/\s+|[0-9A-Za-z가-힣]+|[^\s0-9A-Za-z가-힣]/g) ?? [];
}

function appendDiffPiece(
  pieces: DiffPiece[],
  kind: DiffPieceKind,
  text: string,
): void {
  const previous = pieces[pieces.length - 1];
  if (previous?.kind === kind) previous.text += text;
  else pieces.push({ kind, text });
}

function compareText(beforeText: string, afterText: string): TextDiff {
  if (beforeText === afterText) {
    const same = beforeText ? [{ kind: "unchanged" as const, text: beforeText }] : [];
    return { before: same, after: same, changed: false };
  }
  const beforeTokens = diffTokens(beforeText);
  const afterTokens = diffTokens(afterText);
  if (beforeTokens.length * afterTokens.length > 40_000) {
    return {
      before: beforeText ? [{ kind: "removed", text: beforeText }] : [],
      after: afterText ? [{ kind: "added", text: afterText }] : [],
      changed: true,
    };
  }
  const lengths = Array.from(
    { length: beforeTokens.length + 1 },
    () => Array<number>(afterTokens.length + 1).fill(0),
  );
  for (let beforeIndex = beforeTokens.length - 1; beforeIndex >= 0; beforeIndex -= 1) {
    for (let afterIndex = afterTokens.length - 1; afterIndex >= 0; afterIndex -= 1) {
      lengths[beforeIndex]![afterIndex] = beforeTokens[beforeIndex] === afterTokens[afterIndex]
        ? 1 + lengths[beforeIndex + 1]![afterIndex + 1]!
        : Math.max(
          lengths[beforeIndex + 1]![afterIndex]!,
          lengths[beforeIndex]![afterIndex + 1]!,
        );
    }
  }
  const before: DiffPiece[] = [];
  const after: DiffPiece[] = [];
  let beforeIndex = 0;
  let afterIndex = 0;
  while (beforeIndex < beforeTokens.length && afterIndex < afterTokens.length) {
    if (beforeTokens[beforeIndex] === afterTokens[afterIndex]) {
      const token = beforeTokens[beforeIndex]!;
      appendDiffPiece(before, "unchanged", token);
      appendDiffPiece(after, "unchanged", token);
      beforeIndex += 1;
      afterIndex += 1;
    } else if (
      lengths[beforeIndex + 1]![afterIndex]!
      >= lengths[beforeIndex]![afterIndex + 1]!
    ) {
      appendDiffPiece(before, "removed", beforeTokens[beforeIndex]!);
      beforeIndex += 1;
    } else {
      appendDiffPiece(after, "added", afterTokens[afterIndex]!);
      afterIndex += 1;
    }
  }
  while (beforeIndex < beforeTokens.length) {
    appendDiffPiece(before, "removed", beforeTokens[beforeIndex]!);
    beforeIndex += 1;
  }
  while (afterIndex < afterTokens.length) {
    appendDiffPiece(after, "added", afterTokens[afterIndex]!);
    afterIndex += 1;
  }
  return { before, after, changed: true };
}

function DiffText({ pieces }: { pieces: DiffPiece[] }) {
  return pieces.map((piece, index) => {
    const key = `${piece.kind}-${index}`;
    if (piece.kind === "removed") return <del key={key}>{piece.text}</del>;
    if (piece.kind === "added") return <ins key={key}>{piece.text}</ins>;
    return <span key={key}>{piece.text}</span>;
  });
}

function timecode(seconds: number): string {
  const safe = Math.max(0, seconds);
  const total = Math.floor(safe);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ms = Math.floor((safe - total) * 1000);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

function itemsOf(payload: unknown): Record<string, unknown>[] {
  if (Array.isArray(payload)) return payload as Record<string, unknown>[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["segments", "translations", "items"]) {
      const value = record[key];
      if (Array.isArray(value)) return value as Record<string, unknown>[];
    }
  }
  return [];
}

function textOption(options: Record<string, unknown>, key: string): string {
  const value = options[key];
  return typeof value === "string" ? value.trim() : "";
}

function latestGenerationId(generations: Record<string, unknown>[]): string {
  for (let index = generations.length - 1; index >= 0; index -= 1) {
    const id = generations[index]?.id;
    if (typeof id === "string" && id) return id;
  }
  return "";
}

export default function JobDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const fetcher = useCallback(() => api.job(id), [id]);
  const pollWhileActive = useCallback(
    (payload: JobDetailPayload | null) => payload?.job.state !== "done",
    [],
  );
  const { data: detail, status, error, updatedAt, refreshing, refresh } = useLiveQuery(
    fetcher,
    INTERVAL_MS,
    pollWhileActive,
  );
  const detailRef = useRef<JobDetailPayload | null>(detail);
  detailRef.current = detail;
  const transcribers = useLiveQuery(useCallback(() => api.transcribers(), []), 60000);
  const prompts = useLiveQuery(useCallback(() => api.promptCategories(), []), 60000);
  const settings = useLiveQuery(useCallback(() => api.settings(), []), 60000);
  const job = detail?.job ?? null;
  const state = job ? asJobState(job.state) : null;
  const runtimeNames = useMemo(
    () => new Map((transcribers.data?.items ?? []).map((transcriber) => [transcriber.id, transcriber.name])),
    [transcribers.data?.items],
  );
  const assignedRuntime = job?.transcriber_id
    ? runtimeNames.get(job.transcriber_id) ?? job.transcriber_id
    : null;
  const stateText = job ? jobStateLabel(job) : null;

  const transcriptRevisionId = String(
    detail?.transcript_revisions.at(-1)?.id ?? "base",
  );
  const ownTranslationGenerationId = latestGenerationId(
    detail?.translation_generations ?? [],
  );
  const artifactVersion = job
    ? `${id}:${transcriptRevisionId}:${ownTranslationGenerationId || "legacy"}:${job.phase === "transcription" ? job.updated_at : "stable"}`
    : null;
  const [artifactResult, setArtifactResult] = useState<ArtifactResult | null>(null);
  // 배경 갱신 때는 직전 산출물을 유지하고, 최초 로드만 화면 로딩으로 표시한다.
  const currentArtifact = artifactResult;
  const artifactLoading = artifactResult == null;
  const artifactError = currentArtifact?.error ?? null;
  const segments = useMemo(
    () => currentArtifact?.segments ?? [],
    [currentArtifact],
  );
  const [current, setCurrent] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [promptId, setPromptId] = useState("");
  const [externalModelKey, setExternalModelKey] = useState("");
  const [translationPhase, setTranslationPhase] = useState<TranslationPhaseKey | null>(null);
  const [expandedDiffId, setExpandedDiffId] = useState<string | null>(null);
  const [editingSegmentId, setEditingSegmentId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [lineageRefreshToken, setLineageRefreshToken] = useState(0);
  const [subtitleHistoryExpanded, setSubtitleHistoryExpanded] = useState(false);
  const [timelineEditorOpen, setTimelineEditorOpen] = useState(false);
  const [playheadSeconds, setPlayheadSeconds] = useState(0);
  const [mediaDuration, setMediaDuration] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const detailLayoutRef = useRef<HTMLDivElement | null>(null);
  const previewCardRef = useRef<HTMLElement | null>(null);
  const cueScrollRef = useRef<HTMLDivElement | null>(null);

  const translationLineageSpec = useMemo(() => {
    if (!job || !detail) return "[]";
    const options = job.options;
    const ownGenerationId = latestGenerationId(detail.translation_generations);
    const parentJobId = textOption(options, "pipeline_parent_job_id");
    const inputGenerationId = textOption(
      options,
      "input_translation_generation_id",
    );
    const comparisonJobId = textOption(
      options,
      "comparison_translation_job_id",
    );
    const comparisonGenerationId = textOption(
      options,
      "comparison_translation_generation_id",
    );
    const refs: TranslationGenerationRef[] = [];

    if (
      job.operation === "external_review"
      && comparisonJobId
      && comparisonGenerationId
    ) {
      refs.push({
        phase: "draft",
        label: "1차",
        jobId: comparisonJobId,
        generationId: comparisonGenerationId,
      });
    }
    if (
      ["review_translate", "external_review"].includes(job.operation)
      && parentJobId
      && inputGenerationId
    ) {
      refs.push({
        phase: job.operation === "review_translate" ? "draft" : "review",
        label: job.operation === "review_translate" ? "1차" : "2차",
        jobId: parentJobId,
        generationId: inputGenerationId,
      });
    }
    if (ownGenerationId) {
      const phase: TranslationPhaseKey = job.operation === "external_review"
        ? "external"
        : job.operation === "review_translate"
          ? "review"
          : "draft";
      refs.push({
        phase,
        label: phase === "external" ? "3차" : phase === "review" ? "2차" : "1차",
        jobId: job.id,
        generationId: ownGenerationId,
      });
    }
    return JSON.stringify(refs);
  }, [detail, job]);
  const subtitleGenerationVersion = JSON.stringify(
    (detail?.subtitle_generations ?? []).map((generation) => [
      generation.id,
      generation.is_published,
    ]),
  );
  const translationLineageVersion = [
    translationLineageSpec,
    job?.translation_chunks_completed ?? 0,
    job?.state ?? "pending",
    subtitleGenerationVersion,
    lineageRefreshToken,
  ].join(":");
  const [translationLineage, setTranslationLineage] = useState<TranslationLineageResult | null>(null);

  useEffect(() => {
    if (!artifactVersion) return;
    let cancelled = false;
    void (async () => {
      try {
        const [transcript, translation] = await Promise.all([
          api.artifact(id, "transcript", { compact: true }),
          ownTranslationGenerationId
            ? Promise.resolve(null)
            : api.artifact(id, "translation"),
        ]);
        if (cancelled) return;
        const korean = new Map<string, string>();
        for (const item of itemsOf(translation)) {
          const key = String(item.id ?? "");
          if (key) korean.set(key, String(item.text ?? ""));
        }
        const nextSegments = itemsOf(transcript).map((item, index) => {
          const key = String(item.id ?? index);
          return {
            id: key,
            start: Number(item.start ?? 0),
            end: Number(item.end ?? 0),
            speaker: String(item.speaker ?? "UNKNOWN"),
            ja: String(item.text ?? ""),
            ko: korean.get(key) ?? "",
          };
        });
        setArtifactResult({ version: artifactVersion, segments: nextSegments, error: null });
      } catch (reason) {
        if (!cancelled) {
          setArtifactResult({
            version: artifactVersion,
            segments: [],
            error: reason instanceof Error ? reason.message : String(reason),
          });
        }
      }
    })();
    return () => { cancelled = true; };
  }, [artifactVersion, id, ownTranslationGenerationId]);

  useEffect(() => {
    let cancelled = false;
    const refs = JSON.parse(translationLineageSpec) as TranslationGenerationRef[];
    if (refs.length === 0) {
      void Promise.resolve().then(() => {
        if (!cancelled) {
          setTranslationLineage({
            version: translationLineageVersion,
            phases: [],
            error: null,
          });
        }
      });
      return () => { cancelled = true; };
    }
    void (async () => {
      try {
        const results = await Promise.all(refs.map(async (ref) => {
          const currentDetail = detailRef.current;
          const phaseDetail = ref.jobId === id && currentDetail
            ? currentDetail
            : await api.job(ref.jobId);
          const generationId = latestGenerationId(
            phaseDetail.translation_generations,
          ) || ref.generationId;
          const payload = await api.translationGenerationItems(
            ref.jobId,
            generationId,
          );
          return {
            ref: { ...ref, generationId },
            payload,
            phaseDetail,
          };
        }));
        if (cancelled) return;
        const phases = results.map((result) => {
          const { ref, payload, phaseDetail } = result;
          const texts: Record<string, string> = {};
          for (const item of payload.items) {
            if (item.id) texts[item.id] = item.text;
          }
          const model = payload.generation.model;
          const subtitleGeneration = [...phaseDetail.subtitle_generations]
            .reverse()
            .find((item) => String(item.translation_generation_id ?? "") === ref.generationId);
          const publishedSubtitleGeneration = [...phaseDetail.subtitle_generations]
            .reverse()
            .find((item) => item.is_published === true);
          const subtitleGenerationId = subtitleGeneration?.id;
          return {
            ...ref,
            model: typeof model === "string" ? model : "",
            texts,
            subtitleGenerationId: typeof subtitleGenerationId === "string"
              ? subtitleGenerationId
              : "",
            isPublished: subtitleGeneration?.is_published === true,
            publishedSubtitleGenerationId: typeof publishedSubtitleGeneration?.id === "string"
              ? publishedSubtitleGeneration.id
              : "",
          };
        });
        setTranslationLineage({
          version: translationLineageVersion,
          phases,
          error: null,
        });
      } catch (reason) {
        if (!cancelled) {
          setTranslationLineage({
            version: translationLineageVersion,
            phases: [],
            error: reason instanceof Error ? reason.message : String(reason),
          });
        }
      }
    })();
    return () => { cancelled = true; };
  }, [id, translationLineageSpec, translationLineageVersion]);

  const translated = useMemo(() => segments.filter((segment) => segment.ko).length, [segments]);
  const translationPhases = translationLineage?.phases ?? [];
  const lastTranslationPhase = translationPhases[translationPhases.length - 1];
  const effectiveTranslationPhase = (
    translationPhase
    && translationPhases.some((phase) => phase.phase === translationPhase)
  )
    ? translationPhase
    : lastTranslationPhase?.phase ?? "review";
  const selectedTranslationPhase = translationPhases.find(
    (phase) => phase.phase === effectiveTranslationPhase,
  );
  const canEditTimeline = Boolean(
    selectedTranslationPhase
    && selectedTranslationPhase.phase === lastTranslationPhase?.phase,
  );
  const publishedSubtitlePhase = translationPhases.find(
    (phase) => phase.publishedSubtitleGenerationId,
  );
  const hasSubtitle = Boolean(publishedSubtitlePhase?.publishedSubtitleGenerationId);
  const timelineCues = useMemo<SubtitleTimelineCue[]>(() => segments.map((segment) => ({
    id: segment.id,
    start: segment.start,
    end: segment.end,
    speaker: segment.speaker,
    sourceText: segment.ja,
    text: selectedTranslationPhase?.texts[segment.id] ?? segment.ko,
  })), [segments, selectedTranslationPhase]);
  const cueIndexById = useMemo(
    () => new Map(segments.map((segment, index) => [segment.id, index])),
    [segments],
  );
  const cueKey = useCallback(
    (index: number) => segments[index]?.id ?? index,
    [segments],
  );
  // TanStack Virtual exposes non-memoizable methods by design. Skipping React
  // Compiler memoization for this hook is the supported integration path.
  // eslint-disable-next-line react-hooks/incompatible-library
  const cueVirtualizer = useVirtualizer({
    count: segments.length,
    getScrollElement: () => cueScrollRef.current,
    estimateSize: () => 92,
    getItemKey: cueKey,
    overscan: 8,
  });

  useEffect(() => {
    if (!current) return;
    const index = cueIndexById.get(current);
    if (index != null) cueVirtualizer.scrollToIndex(index, { align: "auto" });
  }, [cueIndexById, cueVirtualizer, current]);

  useEffect(() => {
    const layout = detailLayoutRef.current;
    const preview = previewCardRef.current;
    if (!layout || !preview) return;
    const syncHeight = () => {
      layout.style.setProperty(
        "--preview-card-height",
        `${Math.ceil(preview.getBoundingClientRect().height)}px`,
      );
    };
    syncHeight();
    const observer = new ResizeObserver(syncHeight);
    observer.observe(preview);
    return () => observer.disconnect();
  }, [job?.source_rel, publishedSubtitlePhase?.jobId]);

  const progress = useMemo(() => {
    if (!job) return null;
    if (job.phase === "transcription") {
      return percent(job.chunks_completed, Math.max(job.chunks_created, job.chunks_total_estimate));
    }
    if (["translation", "draft_translation", "review_translation", "external_review"].includes(job.phase)) {
      return percent(job.translation_chunks_completed, job.translation_chunks_total);
    }
    return state === "done" ? 100 : null;
  }, [job, state]);

  const seek = (segment: Segment) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = segment.start;
    void video.play().catch(() => undefined);
  };

  const act = async (task: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await task();
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const latestOwnTranslationGenerationId = latestGenerationId(
    detail?.translation_generations ?? [],
  );
  const existingDraftJob = detail?.child_jobs.find(
    (candidate) => candidate.operation === "draft_translate",
  );
  const existingReviewJob = detail?.child_jobs.find((candidate) => (
    candidate.operation === "review_translate"
    && (
      !latestOwnTranslationGenerationId
      || textOption(
        candidate.options,
        "input_translation_generation_id",
      ) === latestOwnTranslationGenerationId
    )
  ));
  const existingExternalReviewJob = detail?.child_jobs.find((candidate) => (
    candidate.operation === "external_review"
    && (
      !latestOwnTranslationGenerationId
      || textOption(
        candidate.options,
        "input_translation_generation_id",
      ) === latestOwnTranslationGenerationId
    )
  ));
  const isDraftSource = job?.operation === "transcribe"
    && job.status === "transcription_completed";
  const isReviewSource = job?.status === "completed" && (
    job.operation === "draft_translate"
    || (["translate", "full"].includes(job.operation) && jobTranslationMode(job) === "draft_only")
  );
  const canDraft = isDraftSource && !existingDraftJob;
  const canReview = isReviewSource && !existingReviewJob;
  const isExternalReviewSource = job?.operation === "review_translate"
    && job.status === "completed";
  const canExternalReview = isExternalReviewSource
    && !existingExternalReviewJob;
  const externalModels = (settings.data?.external_models ?? []).flatMap((profile) =>
    profile.configured && profile.selected_model
      ? [{
        key: `${profile.provider}\u0000${profile.selected_model}`,
        provider: profile.provider,
        model: profile.selected_model,
      }]
      : [],
  );
  const effectiveExternalModelKey = externalModels.some(
    (item) => item.key === externalModelKey,
  )
    ? externalModelKey
    : externalModels.length === 1
      ? externalModels[0]?.key ?? ""
      : "";
  const [externalProvider = "", externalModel = ""] =
    effectiveExternalModelKey.split("\u0000");
  const workflowJobs = detail?.workflow_jobs ?? (job ? [job] : []);
  const workflowHistoryJobs = detail?.workflow_history_jobs ?? [];
  const previousSubtitleWorkflows = detail?.previous_subtitle_workflows ?? [];
  const visibleSubtitleWorkflows = subtitleHistoryExpanded
    ? previousSubtitleWorkflows
    : previousSubtitleWorkflows.slice(0, SUBTITLE_HISTORY_PREVIEW_LIMIT);
  const jobExternalModel = job?.options.external_model;
  const jobExternalSelection = (
    jobExternalModel
    && typeof jobExternalModel === "object"
    && "provider" in jobExternalModel
    && "model" in jobExternalModel
  )
    ? {
      provider: String(jobExternalModel.provider),
      model: String(jobExternalModel.model),
    }
    : null;
  const playerContent = job ? (
    <ResultPlayer
      sourceRel={job.source_rel}
      subtitleJobId={publishedSubtitlePhase?.jobId}
      videoRef={videoRef}
      initialTime={playheadSeconds}
      onTimeUpdate={(time) => {
        setPlayheadSeconds(time);
        const hit = segments.find((segment) => time >= segment.start && time < segment.end);
        setCurrent(hit ? hit.id : null);
      }}
      onDurationChange={setMediaDuration}
    />
  ) : <div className="empty-state"><strong>표시할 작업 정보가 없습니다</strong></div>;
  return (
    <>
      <LoadingOverlay
        active={refreshing || transcribers.refreshing || prompts.refreshing || settings.refreshing || artifactLoading || busy}
        message={busy ? "작업 요청을 처리하는 중입니다" : artifactLoading ? "작업 산출물을 불러오는 중입니다" : "작업 정보를 불러오는 중입니다"}
      />
      <header className="topbar">
        <Link className="btn sec" href="/jobs">
          <Icon name="chevron_left" size={14} />
          작업 목록
        </Link>
        <div className="page-title job-title">
          <h1>{job ? fileName(job.source_rel) : "작업 상세"}</h1>
          <p>{job ? parentPath(job.source_rel) : "작업 정보를 불러오는 중입니다."}</p>
        </div>
        {state && stateText ? <span className={BADGE_CLASS[state]}>{stateText}</span> : null}
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
      </header>

      <div className="content">
        {actionNotice ? <p role="status" className="notice success">{actionNotice}</p> : null}
        {actionError ? <p role="alert" className="notice error">{actionError}</p> : null}

        <section className="card progress-overview" aria-labelledby="job-progress-title">
          <div className="card-head">
            <div>
              <h2 id="job-progress-title">작업 진행</h2>
              <span className="sub" title={job ? jobProgressLabel(job) : "불러오는 중"}>{job ? jobProgressLabel(job) : "불러오는 중"}</span>
            </div>
            <div className="btns">
              {canDraft || canReview ? (
                <select
                  className="ctl sm"
                  aria-label="번역 프롬프트"
                  value={promptId}
                  disabled={busy}
                  onChange={(event) => setPromptId(event.target.value)}
                >
                  <option value="">프롬프트 선택</option>
                  {(prompts.data?.items ?? []).filter((item) => !item.archived).map((item) => (
                    <option key={item.id} value={item.id}>{item.name}</option>
                  ))}
                </select>
              ) : null}
              {canDraft ? (
                <button type="button" className="btn" disabled={busy || !promptId} onClick={() => void act(async () => {
                  await api.draftTranslateJobs([id], promptId);
                  setActionNotice("독립된 1차 번역 작업을 생성했습니다.");
                })}>
                  <Icon name="play" size={14} />1차 번역
                </button>
              ) : null}
              {isDraftSource && existingDraftJob ? (
                <Link
                  className="btn sec"
                  href={`/jobs/${encodeURIComponent(existingDraftJob.id)}`}
                  title="이 전사 결과에서 생성된 기존 1차 번역 작업으로 이동합니다."
                >
                  <Icon name="chevron_right" size={14} />1차 번역 보기
                </Link>
              ) : null}
              {canReview ? (
                <button type="button" className="btn" disabled={busy || !promptId} onClick={() => void act(async () => {
                  await api.reviewTranslateJobs([id], promptId);
                  setActionNotice("독립된 2차 번역 작업을 생성했습니다.");
                })}>
                  <Icon name="play" size={14} />2차 번역
                </button>
              ) : null}
              {isReviewSource && existingReviewJob ? (
                <Link
                  className="btn sec"
                  href={`/jobs/${encodeURIComponent(existingReviewJob.id)}`}
                  title="현재 1차 번역 generation에서 생성된 기존 2차 번역 작업으로 이동합니다."
                >
                  <Icon name="chevron_right" size={14} />2차 번역 보기
                </Link>
              ) : null}
              {canExternalReview ? (
                <select
                  className="ctl sm"
                  aria-label="외부 검토 모델"
                  value={effectiveExternalModelKey}
                  disabled={busy}
                  onChange={(event) => setExternalModelKey(event.target.value)}
                >
                  <option value="">설정된 외부 모델 선택</option>
                  {externalModels.map((item) => (
                    <option key={item.key} value={item.key}>{item.provider} · {item.model}</option>
                  ))}
                </select>
              ) : null}
              {canExternalReview ? (
                <button type="button" className="btn" disabled={busy || !externalModel} onClick={() => void act(async () => {
                  await api.externalReviewJobs(
                    [id],
                    externalProvider as ExternalModelProvider,
                    externalModel,
                  );
                  setActionNotice("독립된 외부 모델 검토 작업을 생성했습니다.");
                })}>
                  <Icon name="play" size={14} />외부 모델 검토
                </button>
              ) : null}
              {isExternalReviewSource && existingExternalReviewJob ? (
                <Link
                  className="btn sec"
                  href={`/jobs/${encodeURIComponent(existingExternalReviewJob.id)}`}
                  title="현재 2차 번역 generation에서 생성된 기존 외부 검토 작업으로 이동합니다."
                >
                  <Icon name="chevron_right" size={14} />외부 검토 보기
                </Link>
              ) : null}
              {state && canRetryJob(state) ? (
                <button type="button" className="btn sec" disabled={busy} onClick={() => void act(() => api.retryJob(id))}>
                  <Icon name="refresh" size={14} />재시도
                </button>
              ) : null}
              {state && canResumeTranslation(state) ? (
                <button type="button" className="btn" disabled={busy} onClick={() => void act(() => api.resumeTranslation(id))}>
                  <Icon name="play" size={14} />번역 재개
                </button>
              ) : null}
              {job && canPauseTranslation(state, job.phase) ? (
                <button type="button" className="btn sec" disabled={busy} onClick={() => void act(() => api.pauseTranslation(id))}>
                  <Icon name="pause" size={14} />번역 일시정지
                </button>
              ) : null}
              {state && canStopJob(state) ? (
                <button type="button" className="btn dgr" disabled={busy} onClick={() => {
                  if (!window.confirm("이 작업을 정지할까요?")) return;
                  void act(() => api.stopJob(id));
                }}>
                  <Icon name="alert_triangle" size={14} />정지
                </button>
              ) : null}
            </div>
          </div>
          <div className="card-body progress-body">
            <div>
              <span className="eyebrow">현재 작업 진행</span>
              <strong>{job ? jobProgressLabel(job) : "확인 중"}</strong>
              {job ? <span className="muted">작업 종류: {operationLabel(job.operation)} · 상태: {stateText}</span> : null}
              {assignedRuntime ? <span className="muted">전사 서버: {assignedRuntime} <span className="code">({job?.transcriber_id})</span></span> : null}
              {jobExternalSelection ? (
                <span className="muted">
                  외부 검토 모델: {jobExternalSelection.provider} · <span className="code">{jobExternalSelection.model}</span>
                </span>
              ) : null}
              {job?.reason_code ? <span className="reason-text" title={reasonLabel(job.reason_code) ?? undefined}>{reasonLabel(job.reason_code)}</span> : null}
            </div>
            <div className="progress-measure">
              <span>{progress == null ? "진행률 계산 중" : `${progress}%`}</span>
              <progress max={100} value={progress ?? undefined} aria-label="작업 진행률" />
            </div>
          </div>
        </section>

        {workflowJobs.length > 1 ? (
          <section className="card" aria-labelledby="lineage-title">
            <div className="card-head">
              <div>
                <h2 id="lineage-title">같은 영상 작업 · 단계 실행 이력</h2>
                <span className="sub">하나의 영상 흐름 · {workflowJobs.length}개 단계 실행 기록</span>
              </div>
            </div>
            <div className="workflow-stage-list">
              {workflowJobs.map((workflowJob) => {
                const workflowState = asJobState(workflowJob.state);
                const isCurrentJob = workflowJob.id === id;
                return (
                  <Link
                    key={workflowJob.id}
                    href={`/jobs/${workflowJob.id}`}
                    className={isCurrentJob ? "workflow-stage-link is-current" : "workflow-stage-link"}
                    aria-current={isCurrentJob ? "page" : undefined}
                    title={`${operationLabel(workflowJob.operation)} · ${workflowJob.id}`}
                  >
                    <span className="workflow-stage-copy">
                      <strong>{operationLabel(workflowJob.operation)}</strong>
                      <span className="code">{workflowJob.id.slice(0, 8)}</span>
                    </span>
                    <span className={workflowState ? BADGE_CLASS[workflowState] : "b"}>
                      {jobStateLabel(workflowJob)}
                    </span>
                  </Link>
                );
              })}
            </div>
          </section>
        ) : null}

        {workflowHistoryJobs.length ? (
          <section className="card" aria-labelledby="lineage-history-title">
            <div className="card-head">
              <div>
                <h2 id="lineage-history-title">과거 분기 이력</h2>
                <span className="sub">
                  현재 계보와 이어지지 않아 단계 집계에서 제외된 실행 · {workflowHistoryJobs.length}건
                </span>
              </div>
            </div>
            <div className="workflow-stage-list">
              {workflowHistoryJobs.map((workflowJob) => {
                const workflowState = asJobState(workflowJob.state);
                const isCurrentJob = workflowJob.id === id;
                return (
                  <Link
                    key={workflowJob.id}
                    href={`/jobs/${workflowJob.id}`}
                    className={isCurrentJob ? "workflow-stage-link is-current" : "workflow-stage-link"}
                    aria-current={isCurrentJob ? "page" : undefined}
                    title={`${operationLabel(workflowJob.operation)} · ${workflowJob.id}`}
                  >
                    <span className="workflow-stage-copy">
                      <strong>{operationLabel(workflowJob.operation)}</strong>
                      <span className="code">{workflowJob.id.slice(0, 8)}</span>
                    </span>
                    <span className={workflowState ? BADGE_CLASS[workflowState] : "b"}>
                      {jobStateLabel(workflowJob)}
                    </span>
                  </Link>
                );
              })}
            </div>
          </section>
        ) : null}

        {previousSubtitleWorkflows.length ? (
          <section className="card" aria-labelledby="subtitle-history-title">
            <div className="card-head">
              <h2 id="subtitle-history-title">과거 자막 생성 이력</h2>
            </div>
            <div className="subtitle-history-columns" aria-hidden="true">
              <span>작업 일시</span>
              <span>전사 모델</span>
              <span>번역 프롬프트</span>
              <span>산출물</span>
            </div>
            <ol className="subtitle-history-list" id="subtitle-history-list">
              {visibleSubtitleWorkflows.map((history) => {
                const modelLabel = history.transcription_model_revision
                  ? `${history.transcription_backend} · ${history.transcription_model_revision}`
                  : history.transcription_backend;
                const promptLabel = history.translation_prompt_version == null
                  ? `${history.translation_prompt_name} · 버전 미기록`
                  : `${history.translation_prompt_name} · v${history.translation_prompt_version}`;
                return (
                  <li
                    key={history.workflow_root_job_id}
                    className="subtitle-history-row"
                  >
                    <div className="subtitle-history-run">
                      <time dateTime={new Date(history.latest_generation_created_at * 1000).toISOString()}>
                        {clock(history.latest_generation_created_at)}
                      </time>
                      <span className={history.is_test ? "b hold" : "b ok"}>
                        {history.is_test ? "테스트 실행" : "일반 실행"}
                      </span>
                    </div>
                    <div className="subtitle-history-value subtitle-history-model">
                      <span className="subtitle-history-mobile-label">전사 모델</span>
                      <span className="code">{modelLabel}</span>
                    </div>
                    <div className="subtitle-history-value subtitle-history-prompt">
                      <span className="subtitle-history-mobile-label">번역 프롬프트</span>
                      <span>{promptLabel}</span>
                    </div>
                    <div className="btns subtitle-history-actions">
                      {history.transcript_job_id ? (
                        <a
                          className="btn sec sm"
                          href={`/api/v1/jobs/${encodeURIComponent(history.transcript_job_id)}/artifacts/transcript?inline=true`}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <Icon name="captions" size={14} />전사 스크립트
                        </a>
                      ) : (
                        <span className="btn sec sm off" aria-disabled="true">
                          <Icon name="captions" size={14} />전사 없음
                        </span>
                      )}
                      {history.translation_job_id ? (
                        <a
                          className="btn sec sm"
                          href={`/api/v1/jobs/${encodeURIComponent(history.translation_job_id)}/artifacts/translation?inline=true`}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <Icon name="braces" size={14} />번역 결과
                        </a>
                      ) : (
                        <span className="btn sec sm off" aria-disabled="true">
                          <Icon name="braces" size={14} />번역 없음
                        </span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
            {previousSubtitleWorkflows.length > SUBTITLE_HISTORY_PREVIEW_LIMIT ? (
              <div className="subtitle-history-disclosure">
                <button
                  type="button"
                  className="btn sec sm"
                  aria-expanded={subtitleHistoryExpanded}
                  aria-controls="subtitle-history-list"
                  onClick={() => setSubtitleHistoryExpanded((expanded) => !expanded)}
                >
                  {subtitleHistoryExpanded
                    ? `최신 ${SUBTITLE_HISTORY_PREVIEW_LIMIT}건만 보기`
                    : `전체 ${previousSubtitleWorkflows.length}건 보기`}
                </button>
              </div>
            ) : null}
          </section>
        ) : null}

        <div className="detail-layout" ref={detailLayoutRef} hidden={timelineEditorOpen}>
          <section className="card preview-card" ref={previewCardRef}>
            <div className="card-head">
              <h2>결과 미리보기</h2>
              <span className="btns">
                {hasSubtitle ? (
                  <>
                  <a className="btn sec" href={`/api/v1/jobs/${encodeURIComponent(publishedSubtitlePhase?.jobId ?? id)}/subtitle.srt`}><Icon name="download" size={14} />SRT</a>
                  <a className="btn sec" href={`/api/v1/jobs/${encodeURIComponent(publishedSubtitlePhase?.jobId ?? id)}/subtitle.ass`}><Icon name="download" size={14} />ASS</a>
                  </>
                ) : null}
              </span>
            </div>
            <div className="card-body">
              {!timelineEditorOpen ? playerContent : null}
            </div>
          </section>

          <section className="card cue-card">
            <div className="card-head cue-card-head">
              <div>
                <h2>{job?.operation === "transcribe"
                  ? "전사 구간"
                  : `${selectedTranslationPhase?.label ?? "현재"} 자막 스크립트`}</h2>
                <span className="sub" title={job?.operation === "transcribe" ? `${segments.length}개 전사 구간` : `${translated} / ${segments.length}개 번역 · ${translationPhases.map((phase) => phase.label).join(" · ")}`}>
                  {job?.operation === "transcribe"
                    ? `${segments.length}개 전사 구간`
                    : `${translated} / ${segments.length}개 번역${translationPhases.length ? ` · ${translationPhases.map((phase) => phase.label).join(" · ")}` : ""}`}
                </span>
              </div>
              {translationPhases.length ? (
                <div className="cue-stage-actions">
                  <div className="cue-stage-switch" role="tablist" aria-label="표시할 자막 번역 차수">
                    {translationPhases.map((phase) => (
                      <button
                        type="button"
                        role="tab"
                        aria-selected={effectiveTranslationPhase === phase.phase}
                        disabled={timelineEditorOpen && effectiveTranslationPhase !== phase.phase}
                        className={effectiveTranslationPhase === phase.phase ? "is-active" : ""}
                        key={phase.phase}
                        onClick={() => {
                          setTranslationPhase(phase.phase);
                          setExpandedDiffId(null);
                          setEditingSegmentId(null);
                          setTimelineEditorOpen(false);
                        }}
                      >
                        {phase.label}
                      </button>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="btn sec sm"
                    disabled={busy || !canEditTimeline}
                    title={canEditTimeline
                      ? "최종 번역 차수의 시간·문장·세그먼트를 편집합니다."
                      : "타임라인 구조는 최종 번역 차수에서 편집할 수 있습니다."}
                    aria-expanded={timelineEditorOpen}
                    aria-controls="subtitle-timeline-editor"
                    onClick={() => setTimelineEditorOpen((open) => !open)}
                  >
                    <Icon name="captions" size={14} />
                    {timelineEditorOpen ? "타임라인 닫기" : "타임라인 편집"}
                  </button>
                  <button
                    type="button"
                    className="btn sec sm"
                    disabled={busy || !selectedTranslationPhase?.subtitleGenerationId}
                    title={selectedTranslationPhase?.subtitleGenerationId
                      ? `${selectedTranslationPhase.label} 결과로 미디어 자막 파일을 생성합니다.`
                      : "선택한 번역 차수가 완료된 뒤 생성할 수 있습니다."}
                    onClick={() => {
                      if (!selectedTranslationPhase?.subtitleGenerationId) return;
                      if (!window.confirm(`${selectedTranslationPhase.label} 결과로 미디어의 한국어 SRT/ASS 파일을 생성할까요? 기존 파일은 교체됩니다.`)) return;
                      void act(async () => {
                        await api.publishSubtitleGeneration(
                          selectedTranslationPhase.jobId,
                          selectedTranslationPhase.subtitleGenerationId,
                        );
                        setLineageRefreshToken((value) => value + 1);
                        setActionNotice(`${selectedTranslationPhase.label} 결과로 자막 파일을 생성했습니다.`);
                      });
                    }}
                  >
                    <Icon name="check" size={14} />자막 파일 생성
                  </button>
                </div>
              ) : null}
            </div>
            <div className="cue-scroll" ref={cueScrollRef}>
              {artifactError ? <p className="notice error" role="alert">{artifactError}</p> : null}
              {translationLineage?.error ? <p className="notice error" role="alert">번역 단계 결과를 불러오지 못했습니다: {translationLineage.error}</p> : null}
              {segments.length === 0 ? (
                <div className="empty-state">
                  <strong>아직 전사 결과가 없습니다</strong>
                  <span>작업이 진행 중이면 완료되는 대로 자동 갱신됩니다.</span>
                </div>
              ) : (
                <ol
                  className="cue-list cue-list-virtual"
                  aria-label="자막 대사 목록"
                  style={{ height: cueVirtualizer.getTotalSize() }}
                >
                  {cueVirtualizer.getVirtualItems().map((virtualRow) => {
                    const segmentIndex = virtualRow.index;
                    const segment = segments[segmentIndex]!;
                    const on = current === segment.id;
                    const canShowDiff = translationPhases.length > 1;
                    const canOpenCueTools = Boolean(selectedTranslationPhase);
                    const diffExpanded = canOpenCueTools && expandedDiffId === segment.id;
                    const editingThisSegment = editingSegmentId === segment.id;
                    const phaseComparisons = diffExpanded && !editingThisSegment
                      ? translationPhases.slice(1).map((phase, index) => {
                        const previous = translationPhases[index]!;
                        return {
                          previous,
                          phase,
                          diff: compareText(
                            previous.texts[segment.id] ?? "",
                            phase.texts[segment.id] ?? "",
                          ),
                        };
                      })
                      : [];
                    const diffPanelId = `cue-diff-${segmentIndex}`;
                    const cueCopy = (
                      <>
                        <span className="cue-version cue-source">
                          <span className="cue-version-label">원문</span>
                          <span lang="ja">{segment.ja}</span>
                        </span>
                        {selectedTranslationPhase ? (
                          <span className={`cue-version cue-version-${selectedTranslationPhase.phase}`} title={selectedTranslationPhase.model || undefined}>
                            <span className="cue-version-label">{selectedTranslationPhase.label}</span>
                            <strong>{selectedTranslationPhase.texts[segment.id] || "아직 결과 없음"}</strong>
                          </span>
                        ) : segment.ko ? (
                          <span className="cue-version cue-version-current">
                            <span className="cue-version-label">현재</span>
                            <strong>{segment.ko}</strong>
                          </span>
                        ) : job?.operation === "transcribe" ? null : <span>아직 번역되지 않음</span>}
                        {canOpenCueTools ? (
                          <span className="cue-diff-affordance" aria-hidden="true">
                            {diffExpanded
                              ? "비교·편집 닫기"
                              : canShowDiff ? "비교·편집" : "편집"}
                          </span>
                        ) : null}
                      </>
                    );
                    return (
                      <li
                        key={segment.id}
                        data-index={virtualRow.index}
                        ref={cueVirtualizer.measureElement}
                        aria-posinset={segmentIndex + 1}
                        aria-setsize={segments.length}
                        style={{ transform: `translateY(${virtualRow.start}px)` }}
                      >
                        <div className={on ? "cue-row is-current" : "cue-row"}>
                          <button
                            type="button"
                            className="cue-time-button"
                            aria-label={`${timecode(segment.start)}부터 재생`}
                            title={`${timecode(segment.start)}부터 재생`}
                            onClick={() => seek(segment)}
                          >
                            <span className="code cue-time">{timecode(segment.start)}</span>
                          </button>
                          <div className="cue-copy-wrap">
                            {canOpenCueTools ? (
                              <button
                                type="button"
                                className="cue-copy cue-diff-trigger"
                                aria-expanded={diffExpanded}
                                aria-controls={diffPanelId}
                                aria-label={`${timecode(segment.start)} 자막 비교와 편집 ${diffExpanded ? "닫기" : "열기"}`}
                                onClick={() => {
                                  setExpandedDiffId(diffExpanded ? null : segment.id);
                                  if (diffExpanded) setEditingSegmentId(null);
                                }}
                              >
                                {cueCopy}
                              </button>
                            ) : <div className="cue-copy">{cueCopy}</div>}
                            {diffExpanded ? (
                              <div
                                id={diffPanelId}
                                className="cue-diff-panel"
                                role="region"
                                aria-label={`${timecode(segment.start)} 자막 차수별 차이`}
                              >
                                <div className="cue-diff-panel-head">
                                  <span>
                                    <strong>자막 비교·편집</strong>
                                    <span>{selectedTranslationPhase?.label} 선택됨</span>
                                  </span>
                                  <button
                                    type="button"
                                    className="btn sec sm"
                                    disabled={busy || !selectedTranslationPhase}
                                    onClick={() => {
                                      if (editingThisSegment) {
                                        setEditingSegmentId(null);
                                        return;
                                      }
                                      setEditingSegmentId(segment.id);
                                      setEditingText(
                                        selectedTranslationPhase?.texts[segment.id] ?? "",
                                      );
                                    }}
                                  >
                                    {editingThisSegment ? "편집 취소" : "선택 차수 편집"}
                                  </button>
                                </div>
                                {editingThisSegment && selectedTranslationPhase ? (
                                  <form
                                    className="cue-edit-form"
                                    onSubmit={(event) => {
                                      event.preventDefault();
                                      const nextText = editingText.trim();
                                      if (!nextText) return;
                                      void act(async () => {
                                        await api.updateTranslationItem(
                                          selectedTranslationPhase.jobId,
                                          selectedTranslationPhase.generationId,
                                          segment.id,
                                          nextText,
                                        );
                                        setEditingSegmentId(null);
                                        setLineageRefreshToken((value) => value + 1);
                                        setActionNotice(
                                          `${selectedTranslationPhase.label} 자막 수정본을 저장했습니다. 미디어 자막 파일은 아직 변경하지 않았습니다.`,
                                        );
                                      });
                                    }}
                                  >
                                    <label htmlFor={`cue-edit-${segmentIndex}`}>
                                      {selectedTranslationPhase.label} 자막 문장
                                    </label>
                                    <textarea
                                      id={`cue-edit-${segmentIndex}`}
                                      className="ctl cue-edit-textarea"
                                      rows={3}
                                      maxLength={10_000}
                                      autoFocus
                                      value={editingText}
                                      onChange={(event) => setEditingText(event.target.value)}
                                      onKeyDown={(event) => {
                                        if (event.key === "Escape") setEditingSegmentId(null);
                                      }}
                                    />
                                    <span className="cue-edit-help">
                                      수정본은 새 generation으로 보존됩니다. 다음 차수는 다시 실행되지 않으며, 위의 자막 파일 생성 버튼을 누르기 전까지 미디어 파일은 바뀌지 않습니다.
                                    </span>
                                    <div className="btns">
                                      <button
                                        type="button"
                                        className="btn sec sm"
                                        disabled={busy}
                                        onClick={() => setEditingSegmentId(null)}
                                      >
                                        취소
                                      </button>
                                      <button
                                        type="submit"
                                        className="btn sm"
                                        disabled={
                                          busy
                                          || !editingText.trim()
                                          || editingText.trim()
                                            === (selectedTranslationPhase.texts[segment.id] ?? "").trim()
                                        }
                                      >
                                        수정본 저장
                                      </button>
                                    </div>
                                  </form>
                                ) : canShowDiff ? (
                                  <div className="cue-diff-comparisons">
                                    {phaseComparisons.map(({ previous, phase, diff }) => (
                                      <section className="cue-diff-pair" key={`${previous.phase}-${phase.phase}`}>
                                        <div className="cue-diff-pair-head">
                                          <strong>{previous.label} → {phase.label}</strong>
                                          {!diff.changed ? <span className="b line">변경 없음</span> : null}
                                        </div>
                                        {diff.changed ? (
                                          <>
                                            <p className="cue-diff-line is-before">
                                              <span>이전·삭제</span>
                                              <span><DiffText pieces={diff.before} /></span>
                                            </p>
                                            <p className="cue-diff-line is-after">
                                              <span>변경·추가</span>
                                              <span><DiffText pieces={diff.after} /></span>
                                            </p>
                                          </>
                                        ) : <p className="cue-diff-unchanged">두 차수의 문장이 같습니다.</p>}
                                      </section>
                                    ))}
                                  </div>
                                ) : (
                                  <p className="cue-diff-unchanged">비교할 다른 차수는 없지만 선택한 자막은 편집할 수 있습니다.</p>
                                )}
                              </div>
                            ) : null}
                          </div>
                        </div>
                      </li>
                    );
                  })}
                </ol>
              )}
            </div>
          </section>
        </div>

        {timelineEditorOpen && selectedTranslationPhase && canEditTimeline ? (
          <div id="subtitle-timeline-editor">
            <SubtitleTimelineEditor
              key={`${selectedTranslationPhase.jobId}:${selectedTranslationPhase.generationId}`}
              cues={timelineCues}
              currentTime={playheadSeconds}
              duration={mediaDuration}
              busy={busy}
              preview={(
                <div className="subtitle-editor-preview-content">
                  <div className="subtitle-editor-preview-head">
                    <strong>영상 미리보기</strong>
                    <span>재생 위치가 아래 타임라인의 빨간 재생 헤드와 동기화됩니다.</span>
                  </div>
                  {playerContent}
                </div>
              )}
              onSeek={(seconds) => {
                const video = videoRef.current;
                if (!video) return;
                video.currentTime = seconds;
                setPlayheadSeconds(seconds);
              }}
              onClose={() => setTimelineEditorOpen(false)}
              onSave={(cues) => act(async () => {
                await api.updateSubtitleTimeline(
                  selectedTranslationPhase.jobId,
                  selectedTranslationPhase.generationId,
                  cues,
                );
                setLineageRefreshToken((value) => value + 1);
                setActionNotice(
                  "자막 타임라인 수정본을 저장했습니다. 미디어 자막 파일은 아직 변경하지 않았습니다.",
                );
              })}
            />
          </div>
        ) : null}

        <section className="card" aria-labelledby="event-title">
          <div className="card-head">
            <h2 id="event-title">작업 기록</h2>
            <span className="sub" title={`${detail?.events.length ?? 0}건`}>{detail?.events.length ?? 0}건</span>
          </div>
          <div className="event-list">
            {(detail?.events ?? []).length ? detail?.events.slice(-100).reverse().map((event, index) => (
              <div className="event-row" key={`${event.created_at ?? "event"}-${index}`}>
                <time className="code event-time">{clock(event.created_at)}</time>
                <span className={`event-level ${event.level === "error" ? "b bad" : event.level === "warning" ? "b hold" : "b line"}`}>{EVENT_LEVEL_LABEL[event.level ?? ""] ?? "기록"}</span>
                <span className="event-message" title={event.message}>{eventText(event, runtimeNames)}</span>
              </div>
            )) : <div className="empty-state compact"><strong>기록이 없습니다</strong></div>}
          </div>
        </section>
      </div>
    </>
  );
}
