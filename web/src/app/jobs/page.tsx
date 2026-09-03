"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { LoadingOverlay } from "@/components/LoadingOverlay";
import { Pagination } from "@/components/Pagination";
import { api, type ExternalModelProvider } from "@/lib/api";
import {
  JOB_PHASES,
  JOB_OPERATIONS,
  PUBLIC_JOB_PHASES,
  PUBLIC_JOB_OPERATIONS,
  OPERATION_LABEL,
  PHASE_LABEL,
  STATE_LABEL,
  STATE_ORDER,
  asJobState,
  canPauseTranslation,
  canRetryJob,
  canStopJob,
  reasonLabel,
  type JobPhase,
  type JobOperation,
  type JobState,
} from "@/lib/domain";
import { clock, fileName, parentPath } from "@/lib/format";
import { jobStageLabel, jobTranslationMode } from "@/lib/jobPresentation";
import { useLiveQuery } from "@/lib/useLiveQuery";

const JOBS_INTERVAL_MS = 5000;
const PAGE_SIZE = 20;

const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

const ROW_CLASS: Record<JobState, string> = {
  running: "on-run",
  waiting: "on-wait",
  paused: "on-hold",
  blocked: "on-hold",
  stopped: "on-hold",
  failed: "on-bad",
  done: "on-ok",
};

export default function JobsPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const stateFilter = useMemo(() => {
    const state = searchParams.getAll("state").find(
      (value): value is JobState => STATE_ORDER.includes(value as JobState),
    );
    return state ? [state] : [];
  }, [searchParams]);
  const phaseFilter = useMemo(() =>
    searchParams.getAll("phase").filter((value): value is JobPhase => JOB_PHASES.includes(value as JobPhase)),
    [searchParams],
  );
  const operationFilter = useMemo(() =>
    searchParams.getAll("operation").filter((value): value is JobOperation => JOB_OPERATIONS.includes(value as JobOperation)),
    [searchParams],
  );
  const pageValue = Number(searchParams.get("page") ?? "1");
  const page = Number.isSafeInteger(pageValue) && pageValue > 0 ? pageValue : 1;
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [translationPromptId, setTranslationPromptId] = useState("");
  const [externalModelKey, setExternalModelKey] = useState("");

  const fetcher = useCallback(
    () => api.jobs({
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      state: stateFilter.length ? stateFilter : undefined,
      phase: phaseFilter.length ? phaseFilter : undefined,
      operation: operationFilter.length ? operationFilter : undefined,
    }),
    [operationFilter, page, phaseFilter, stateFilter],
  );
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, JOBS_INTERVAL_MS);
  const transcribers = useLiveQuery(useCallback(() => api.transcribers(), []), 60000);
  const prompts = useLiveQuery(useCallback(() => api.promptCategories(), []), 60000);
  const settings = useLiveQuery(useCallback(() => api.settings(), []), 60000);
  const jobs = useMemo(() => data?.items ?? [], [data?.items]);
  const runtimeNames = useMemo(
    () => new Map((transcribers.data?.items ?? []).map((transcriber) => [transcriber.id, transcriber.name])),
    [transcribers.data?.items],
  );
  const activePrompts = useMemo(
    () => (prompts.data?.items ?? []).filter((item) => !item.archived),
    [prompts.data?.items],
  );
  const pageCount = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE));

  useEffect(() => {
    if (!data || page <= pageCount) return;
    const params = new URLSearchParams(searchParams.toString());
    if (pageCount <= 1) params.delete("page");
    else params.set("page", String(pageCount));
    const suffix = params.toString();
    router.replace(suffix ? `/jobs?${suffix}` : "/jobs", { scroll: false });
  }, [data, page, pageCount, router, searchParams]);

  const setFilter = (key: "state" | "phase" | "operation", next: readonly string[]) => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete(key);
    params.delete("page");
    next.forEach((value) => params.append(key, value));
    const suffix = params.toString();
    router.replace(suffix ? `/jobs?${suffix}` : "/jobs", { scroll: false });
  };

  const clearFilters = () => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("state");
    params.delete("phase");
    params.delete("operation");
    params.delete("reason_code");
    params.delete("page");
    const suffix = params.toString();
    router.replace(suffix ? `/jobs?${suffix}` : "/jobs", { scroll: false });
    setSelected(new Set());
  };

  const goToPage = (nextPage: number) => {
    const params = new URLSearchParams(searchParams.toString());
    if (nextPage <= 1) params.delete("page");
    else params.set("page", String(nextPage));
    const suffix = params.toString();
    router.push(suffix ? `/jobs?${suffix}` : "/jobs");
    setSelected(new Set());
    setActionNotice(null);
  };

  const activeFilterCount = stateFilter.length + phaseFilter.length + operationFilter.length;

  const selectedJobs = useMemo(
    () => jobs.filter((job) => selected.has(job.id)),
    [jobs, selected],
  );
  const pageSelected = jobs.length > 0 && selectedJobs.length === jobs.length;
  const retryIds = selectedJobs
    .filter((job) => canRetryJob(asJobState(job.state)))
    .map((job) => job.id);
  const pauseIds = selectedJobs
    .filter((job) => canPauseTranslation(asJobState(job.state), job.phase))
    .map((job) => job.id);
  const draftTranslationIds = selectedJobs
    .filter((job) => job.operation === "transcribe" && job.status === "transcription_completed")
    .map((job) => job.id);
  const reviewTranslationIds = selectedJobs
    .filter((job) => (
      job.status === "completed"
      && (
        job.operation === "draft_translate"
        || (["translate", "full"].includes(job.operation) && jobTranslationMode(job) === "draft_only")
      )
    ))
    .map((job) => job.id);
  const externalReviewIds = selectedJobs
    .filter((job) => job.status === "completed" && job.operation === "review_translate")
    .map((job) => job.id);
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
  const stopIds = selectedJobs
    .filter((job) => canStopJob(asJobState(job.state)))
    .map((job) => job.id);

  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const run = async (
    ids: string[],
    action: (ids: string[]) => Promise<unknown>,
    successMessage?: string,
  ) => {
    if (!ids.length) return;
    setBusy(true);
    setActionError(null);
    setActionNotice(null);
    try {
      await action(ids);
      setSelected(new Set());
      await refresh();
      if (successMessage) setActionNotice(successMessage);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <LoadingOverlay
        active={refreshing || transcribers.refreshing || prompts.refreshing || settings.refreshing || busy}
        message={busy ? "선택한 작업을 처리하는 중입니다" : "작업 목록을 불러오는 중입니다"}
      />
      <header className="topbar">
        <h1>작업 목록</h1>
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        <button type="button" className="btn sec sm" disabled={refreshing} onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />
          새로고침
        </button>
      </header>

      <div className="content">
        <section className="card">
          <div className="card-head">
            <h2>필터</h2>
            {activeFilterCount ? (
              <button type="button" className="btn sec sm" onClick={clearFilters}>
                전체 해제
              </button>
            ) : null}
          </div>
          <div className="card-body">
            <div className="job-filter-stack">
              <div className="job-filter-row">
                <strong>작업 종류</strong>
                <div className="rail">
                  {PUBLIC_JOB_OPERATIONS.map((operation) => {
                    const on = operationFilter.includes(operation);
                    return <button key={operation} type="button" aria-pressed={on} className={on ? "chip on" : "chip"} onClick={() => {
                      setSelected(new Set());
                      setFilter("operation", on ? operationFilter.filter((value) => value !== operation) : [...operationFilter, operation]);
                    }}>{OPERATION_LABEL[operation]}</button>;
                  })}
                </div>
              </div>
              <div className="job-filter-row">
                <strong>처리 단계</strong>
                <div className="rail">
                  {PUBLIC_JOB_PHASES.map((phase) => {
                    const on = phaseFilter.includes(phase);
                    return <button key={phase} type="button" aria-pressed={on} className={on ? "chip on" : "chip"} onClick={() => {
                      setSelected(new Set());
                      setFilter("phase", on ? phaseFilter.filter((value) => value !== phase) : [...phaseFilter, phase]);
                    }}>{PHASE_LABEL[phase]}</button>;
                  })}
                </div>
              </div>
              <div className="job-filter-row">
                <strong>작업 상태</strong>
                <div className="rail">
                  {STATE_ORDER.map((state) => {
                    const on = stateFilter.includes(state);
                    return <button key={state} type="button" aria-pressed={on} className={on ? "chip on" : "chip"} onClick={() => {
                      setSelected(new Set());
                      setFilter("state", on ? [] : [state]);
                    }}>{state === "done" ? "종료된 작업" : STATE_LABEL[state]}</button>;
                  })}
                </div>
              </div>
            </div>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>
              작업
              <span className="n job-count">
                {data?.total ?? 0}
              </span>
            </h2>
            <span className="btns">
              <button
                type="button"
                className="btn sec sm mobile-page-select"
                disabled={!jobs.length || busy}
                aria-pressed={pageSelected}
                onClick={() => setSelected(pageSelected ? new Set() : new Set(jobs.map((job) => job.id)))}
              >
                <Icon name="check" size={13} />
                {pageSelected ? "페이지 선택 해제" : `현재 페이지 ${jobs.length}건 선택`}
              </button>
              <span className="selection-summary" aria-live="polite">{selectedJobs.length ? `${selectedJobs.length}건 선택` : "작업을 선택하세요"}</span>
              <select
                className="ctl sm job-translation-prompt"
                aria-label="번역 프롬프트"
                title="번역 프롬프트"
                value={translationPromptId}
                disabled={busy || prompts.status === "loading"}
                onChange={(event) => setTranslationPromptId(event.target.value)}
              >
                <option value="">{prompts.status === "loading" ? "불러오는 중" : "프롬프트 선택"}</option>
                {activePrompts.map((prompt) => (
                  <option key={prompt.id} value={prompt.id}>{prompt.name}</option>
                ))}
              </select>
              <button
                type="button"
                className="btn sec sm"
                disabled={!draftTranslationIds.length || !translationPromptId || busy}
                title={!draftTranslationIds.length ? "전사 완료 작업을 선택하세요." : !translationPromptId ? "번역 프롬프트를 선택하세요." : "1차 초벌 번역까지만 실행합니다."}
                onClick={() => void run(
                  draftTranslationIds,
                  (ids) => api.draftTranslateJobs(ids, translationPromptId),
                  `${draftTranslationIds.length}건의 1차 번역을 시작했습니다.`,
                )}
              >
                <Icon name="play" size={13} />
                1차 번역 {draftTranslationIds.length || ""}
              </button>
              <button
                type="button"
                className="btn sec sm"
                disabled={!reviewTranslationIds.length || !translationPromptId || busy}
                title={!reviewTranslationIds.length ? "1차 자막 완료 작업을 선택하세요." : !translationPromptId ? "번역 프롬프트를 선택하세요." : "기존 1차 번역을 다시 번역하지 않고 검수·교정합니다."}
                onClick={() => void run(
                  reviewTranslationIds,
                  (ids) => api.reviewTranslateJobs(ids, translationPromptId),
                  `${reviewTranslationIds.length}건의 2차 보정을 시작했습니다.`,
                )}
              >
                <Icon name="play" size={13} />
                2차 보정 {reviewTranslationIds.length || ""}
              </button>
              <select
                className="ctl sm job-translation-prompt"
                aria-label="외부 검토 모델"
                value={effectiveExternalModelKey}
                disabled={busy || settings.status === "loading"}
                onChange={(event) => setExternalModelKey(event.target.value)}
              >
                <option value="">설정된 외부 모델 선택</option>
                {externalModels.map((item) => (
                  <option key={item.key} value={item.key}>{item.provider} · {item.model}</option>
                ))}
              </select>
              <button
                type="button"
                className="btn sm"
                disabled={!externalReviewIds.length || !externalProvider || !externalModel || busy}
                title={!externalReviewIds.length ? "2차 번역 완료 작업을 선택하세요." : !externalModel ? "외부 모델을 선택하세요." : "외부 모델 검토 결과는 수동 게시 전까지 공개되지 않습니다."}
                onClick={() => void run(
                  externalReviewIds,
                  (ids) => api.externalReviewJobs(
                    ids,
                    externalProvider as ExternalModelProvider,
                    externalModel,
                  ),
                  `${externalReviewIds.length}건의 외부 모델 검토를 시작했습니다.`,
                )}
              >
                <Icon name="play" size={13} />
                외부 모델 검토 {externalReviewIds.length || ""}
              </button>
              <button type="button" className="btn sec sm" disabled={!retryIds.length || busy} onClick={() => void run(retryIds, api.retryJobs)}>
                <Icon name="refresh" size={13} />
                재시도 {retryIds.length || ""}
              </button>
              <button type="button" className="btn sec sm" disabled={!pauseIds.length || busy} onClick={() => void run(pauseIds, api.pauseTranslations)}>
                <Icon name="pause" size={13} />
                번역 일시정지 {pauseIds.length || ""}
              </button>
              <button type="button" className="btn dgr sm" disabled={!stopIds.length || busy} onClick={() => {
                if (!window.confirm(`선택한 ${stopIds.length}건의 작업을 정지할까요?`)) return;
                void run(stopIds, api.stopJobs);
              }}>
                <Icon name="alert_triangle" size={13} />
                정지 {stopIds.length || ""}
              </button>
            </span>
          </div>
          <div className="card-body flush">
            {actionNotice ? (
              <p className="notice success job-action-notice" role="status">{actionNotice}</p>
            ) : null}
            {actionError ? (
              <p role="alert" className="job-action-error">
                {actionError}
              </p>
            ) : null}
            <div className="tbl jobs-table" role="table" aria-label="작업 목록">
              <div className="tr head job-grid-row" role="row">
                <span role="columnheader">
                  <input
                    type="checkbox"
                    checked={pageSelected}
                    onChange={(event) => setSelected(event.target.checked ? new Set(jobs.map((job) => job.id)) : new Set())}
                    aria-label="현재 표시 작업 전체 선택"
                  />
                </span>
                <span role="columnheader">단계</span>
                <span role="columnheader">상태</span>
                <span role="columnheader">테스트</span>
                <span role="columnheader">미디어</span>
                <span role="columnheader">사유</span>
                <span role="columnheader" className="r">최근 변경</span>
              </div>
              {jobs.length === 0 ? (
                <div className="tr empty" role="row">
                  <span role="cell">조건에 맞는 작업 없음</span>
                </div>
              ) : (
                jobs.map((job) => {
                  const state = asJobState(job.state);
                  const reason = reasonLabel(job.reason_code) ?? job.error ?? "—";
                  const on = selected.has(job.id);
                  const selectionId = `job-select-${job.id}`;
                  const runtime = job.transcriber_id
                    ? runtimeNames.get(job.transcriber_id) ?? job.transcriber_id
                    : null;
                  const phase = jobStageLabel(job);
                  const stateText = state ? STATE_LABEL[state] : job.state;
                  const filename = fileName(job.source_rel);
                  const nfoTitle = job.nfo_title?.trim() || null;
                  const displayTitle = nfoTitle ?? filename;
                  const location = parentPath(job.source_rel);
                  const identityDetail = [
                    nfoTitle ? filename : location,
                    runtime ? `전사 서버 · ${runtime}` : null,
                  ].filter((value): value is string => Boolean(value)).join(" · ");
                  return (
                    <div
                      key={job.id}
                      className={`tr job-grid-row job-table-row ${state ? ROW_CLASS[state] : ""}`}
                      role="row"
                      aria-selected={on}
                    >
                      <label role="cell" className="job-select-cell" htmlFor={selectionId}><input
                          id={selectionId}
                          type="checkbox"
                          checked={on}
                          onChange={() => toggle(job.id)}
                          aria-label={`${fileName(job.source_rel)} 선택`}
                        /></label>
                      <div role="cell" className="job-meta-cell job-phase-cell" data-label="단계">
                        <span className="b line" title={phase}>{phase}</span>
                      </div>
                      <label role="cell" htmlFor={selectionId} className="job-meta-cell job-state-cell" data-label="상태">
                        <span className={state ? BADGE_CLASS[state] : "b"} title={stateText}>{stateText}</span>
                      </label>
                      <div role="cell" className="job-meta-cell job-test-cell" data-label="테스트">
                        {job.is_test ? (
                          <span className="b line" title="임시 검증 작업">테스트</span>
                        ) : (
                          <span className="m" aria-label="일반 작업">—</span>
                        )}
                      </div>
                      <div role="cell" className="job-name-cell" data-label="미디어">
                        <div className={job.poster_path ? "job-identity has-poster" : "job-identity"}>
                          {job.poster_path ? (
                            <Link
                              href={`/jobs/${encodeURIComponent(job.id)}`}
                              className="job-poster"
                              aria-label={`${displayTitle} 작업 상세`}
                            >
                              {/* eslint-disable-next-line @next/next/no-img-element */}
                              <img src={api.posterUrl(job.poster_path)} alt="" loading="lazy" />
                            </Link>
                          ) : null}
                          <div className="t-name">
                            <b title={displayTitle}>
                              <Link href={`/jobs/${encodeURIComponent(job.id)}`}>{displayTitle}</Link>
                            </b>
                            {identityDetail ? <span title={`${job.source_rel}${runtime ? ` · 전사 서버: ${runtime}` : ""}`}>{identityDetail}</span> : null}
                          </div>
                        </div>
                      </div>
                      <div role="cell" className="job-meta-cell job-reason-cell" data-label="사유">
                        <span className="m job-reason-value" title={reason}>{reason}</span>
                      </div>
                      <div role="cell" className="job-meta-cell job-updated-cell" data-label="최근 변경">
                        <time className="m" dateTime={job.updated_at}>{clock(job.updated_at)}</time>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
            <Pagination
              currentPage={page}
              pageSize={PAGE_SIZE}
              totalItems={data?.total ?? 0}
              onPageChange={goToPage}
              ariaLabel="작업 목록 페이지 이동"
            />
          </div>
        </section>
      </div>
    </>
  );
}
