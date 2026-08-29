"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { LoadingOverlay } from "@/components/LoadingOverlay";
import { api } from "@/lib/api";
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
import { EVENT_LEVEL_LABEL, eventText, jobProgressLabel, jobStateLabel } from "@/lib/jobPresentation";
import { useLiveQuery } from "@/lib/useLiveQuery";

const INTERVAL_MS = 5000;

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
  ja: string;
  ko: string;
}

interface ArtifactResult {
  version: string;
  segments: Segment[];
  error: string | null;
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

export default function JobDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const fetcher = useCallback(() => api.job(id), [id]);
  const { data: detail, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, INTERVAL_MS);
  const runtimes = useLiveQuery(useCallback(() => api.runtimes(), []), 60000);
  const job = detail?.job ?? null;
  const state = job ? asJobState(job.state) : null;
  const runtimeNames = useMemo(
    () => new Map((runtimes.data?.items ?? []).map((runtime) => [runtime.id, runtime.name])),
    [runtimes.data?.items],
  );
  const assignedRuntime = job?.stt_runtime_id
    ? runtimeNames.get(job.stt_runtime_id) ?? job.stt_runtime_id
    : null;
  const stateText = job ? jobStateLabel(job) : null;

  const artifactVersion = `${id}:${job?.updated_at ?? "pending"}`;
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
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const cueRefs = useRef(new Map<string, HTMLLIElement>());

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [transcript, translation] = await Promise.all([
          api.artifact(id, "transcript"),
          api.artifact(id, "translation"),
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
  }, [artifactVersion, id]);

  useEffect(() => {
    if (current) cueRefs.current.get(current)?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const translated = useMemo(() => segments.filter((segment) => segment.ko).length, [segments]);
  const progress = useMemo(() => {
    if (!job) return null;
    if (job.phase === "transcription") {
      return percent(job.chunks_completed, Math.max(job.chunks_created, job.chunks_total_estimate));
    }
    if (job.phase === "translation") {
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

  const hasSubtitle = Boolean(detail?.subtitle_generations?.length);

  return (
    <>
      <LoadingOverlay
        active={refreshing || runtimes.refreshing || artifactLoading || busy}
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
        {actionError ? <p role="alert" className="notice error">{actionError}</p> : null}

        <section className="card progress-overview" aria-labelledby="job-progress-title">
          <div className="card-head">
            <div>
              <h2 id="job-progress-title">작업 진행</h2>
              <span className="sub" title={job ? jobProgressLabel(job) : "불러오는 중"}>{job ? jobProgressLabel(job) : "불러오는 중"}</span>
            </div>
            <div className="btns">
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
              {assignedRuntime ? <span className="muted">전사 서버: {assignedRuntime} <span className="code">({job?.stt_runtime_id})</span></span> : null}
              {job?.reason_code ? <span className="reason-text" title={reasonLabel(job.reason_code) ?? undefined}>{reasonLabel(job.reason_code)}</span> : null}
            </div>
            <div className="progress-measure">
              <span>{progress == null ? "진행률 계산 중" : `${progress}%`}</span>
              <progress max={100} value={progress ?? undefined} aria-label="작업 진행률" />
            </div>
          </div>
        </section>

        <div className="detail-layout">
          <section className="card">
            <div className="card-head">
              <h2>결과 미리보기</h2>
              {hasSubtitle ? (
                <span className="btns">
                  <a className="btn sec" href={`/api/v1/jobs/${encodeURIComponent(id)}/subtitle.srt`}><Icon name="download" size={14} />SRT</a>
                  <a className="btn sec" href={`/api/v1/jobs/${encodeURIComponent(id)}/subtitle.ass`}><Icon name="download" size={14} />ASS</a>
                </span>
              ) : <span className="sub" title="자막 생성 후 다운로드할 수 있습니다.">자막 생성 후 다운로드할 수 있습니다.</span>}
            </div>
            <div className="card-body">
              {job ? (
                <video
                  ref={videoRef}
                  controls
                  preload="metadata"
                  playsInline
                  crossOrigin="anonymous"
                  className="video-preview"
                  onTimeUpdate={(event) => {
                    const t = event.currentTarget.currentTime;
                    const hit = segments.find((segment) => t >= segment.start && t < segment.end);
                    setCurrent(hit ? hit.id : null);
                  }}
                >
                  <source src={api.mediaFileUrl(job.source_rel)} />
                  {hasSubtitle ? <track kind="subtitles" srcLang="ko" label="한국어" default src={api.subtitlesUrl(id)} /> : null}
                </video>
              ) : <div className="empty-state"><strong>표시할 작업 정보가 없습니다</strong></div>}
            </div>
          </section>

          <section className="card cue-card">
            <div className="card-head">
              <div>
                <h2>{job?.operation === "transcribe" ? "전사 구간" : "자막"}</h2>
                <span className="sub" title={job?.operation === "transcribe" ? `${segments.length}개 전사 구간` : `${translated} / ${segments.length}개 번역`}>
                  {job?.operation === "transcribe" ? `${segments.length}개 전사 구간` : `${translated} / ${segments.length}개 번역`}
                </span>
              </div>
            </div>
            <div className="cue-scroll">
              {artifactError ? <p className="notice error" role="alert">{artifactError}</p> : null}
              {segments.length === 0 ? (
                <div className="empty-state">
                  <strong>아직 전사 결과가 없습니다</strong>
                  <span>작업이 진행 중이면 완료되는 대로 자동 갱신됩니다.</span>
                </div>
              ) : (
                <ol className="cue-list" aria-label="자막 대사 목록">
                  {segments.map((segment) => {
                    const on = current === segment.id;
                    return (
                      <li key={segment.id} ref={(node) => { if (node) cueRefs.current.set(segment.id, node); else cueRefs.current.delete(segment.id); }}>
                        <button type="button" className={on ? "cue-button is-current" : "cue-button"} onClick={() => seek(segment)}>
                          <span className="code cue-time">{timecode(segment.start)}</span>
                          <span className="cue-copy">
                            {segment.ko ? <strong>{segment.ko}</strong> : job?.operation === "transcribe" ? null : <span>아직 번역되지 않음</span>}
                            <span lang="ja">{segment.ja}</span>
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ol>
              )}
            </div>
          </section>
        </div>

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
