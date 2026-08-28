"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api } from "@/lib/api";
import {
  JOB_PHASES,
  JOB_OPERATIONS,
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
import { jobStateLabel } from "@/lib/jobPresentation";
import { useLiveQuery } from "@/lib/useLiveQuery";

const JOBS_INTERVAL_MS = 5000;

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

const GRID = "36px 92px minmax(0, 1fr) 110px minmax(0, 220px) 132px";

export default function JobsPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const stateFilter = useMemo(() =>
    searchParams.getAll("state").filter((value): value is JobState => STATE_ORDER.includes(value as JobState)),
    [searchParams],
  );
  const phaseFilter = useMemo(() =>
    searchParams.getAll("phase").filter((value): value is JobPhase => JOB_PHASES.includes(value as JobPhase)),
    [searchParams],
  );
  const operationFilter = useMemo(() =>
    searchParams.getAll("operation").filter((value): value is JobOperation => JOB_OPERATIONS.includes(value as JobOperation)),
    [searchParams],
  );
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [limit, setLimit] = useState(20);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetcher = useCallback(
    () => api.jobs({
      limit,
      state: stateFilter.length ? stateFilter : undefined,
      phase: phaseFilter.length ? phaseFilter : undefined,
      operation: operationFilter.length ? operationFilter : undefined,
    }),
    [limit, operationFilter, phaseFilter, stateFilter],
  );
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, JOBS_INTERVAL_MS);
  const runtimes = useLiveQuery(useCallback(() => api.runtimes(), []), 60000);
  const jobs = useMemo(() => data?.items ?? [], [data?.items]);
  const runtimeNames = useMemo(
    () => new Map((runtimes.data?.items ?? []).map((runtime) => [runtime.id, runtime.name])),
    [runtimes.data?.items],
  );

  const setFilter = (key: "state" | "phase" | "operation", next: readonly string[]) => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete(key);
    next.forEach((value) => params.append(key, value));
    const suffix = params.toString();
    router.replace(suffix ? `/jobs?${suffix}` : "/jobs", { scroll: false });
    setLimit(20);
  };

  const clearFilters = () => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("state");
    params.delete("phase");
    params.delete("operation");
    params.delete("reason_code");
    const suffix = params.toString();
    router.replace(suffix ? `/jobs?${suffix}` : "/jobs", { scroll: false });
    setSelected(new Set());
    setLimit(20);
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

  const run = async (ids: string[], action: (ids: string[]) => Promise<unknown>) => {
    if (!ids.length) return;
    setBusy(true);
    setActionError(null);
    try {
      await action(ids);
      setSelected(new Set());
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
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
                  {JOB_OPERATIONS.map((operation) => {
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
                  {JOB_PHASES.map((phase) => {
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
                      setFilter("state", on ? stateFilter.filter((value) => value !== state) : [...stateFilter, state]);
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
              <span className="n" style={{ marginLeft: 7 }}>
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
            {actionError ? (
              <p role="alert" style={{ margin: 0, padding: "8px 12px", color: "var(--bad)", fontSize: ".82rem" }}>
                {actionError}
              </p>
            ) : null}
            <div className="tbl jobs-table" role="table" aria-label="작업 목록">
              <div className="tr head" role="row" style={{ gridTemplateColumns: GRID }}>
                <span role="columnheader">
                  <input
                    type="checkbox"
                    checked={pageSelected}
                    onChange={(event) => setSelected(event.target.checked ? new Set(jobs.map((job) => job.id)) : new Set())}
                    aria-label="현재 표시 작업 전체 선택"
                  />
                </span>
                <span role="columnheader">상태</span>
                <span role="columnheader">작업</span>
                <span role="columnheader">단계</span>
                <span role="columnheader">사유</span>
                <span role="columnheader" className="r">최근 변경</span>
              </div>
              {jobs.length === 0 ? (
                <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                  <span role="cell">{status === "loading" ? "불러오는 중" : "조건에 맞는 작업 없음"}</span>
                </div>
              ) : (
                jobs.map((job) => {
                  const state = asJobState(job.state);
                  const reason = reasonLabel(job.reason_code) ?? job.error ?? "—";
                  const on = selected.has(job.id);
                  const runtime = job.stt_runtime_id
                    ? runtimeNames.get(job.stt_runtime_id) ?? job.stt_runtime_id
                    : null;
                  const phase = PHASE_LABEL[job.phase as JobPhase] ?? job.phase;
                  const stateText = jobStateLabel(job);
                  return (
                    <div
                      key={job.id}
                      className={`tr job-table-row ${state ? ROW_CLASS[state] : ""}`}
                      role="row"
                      aria-selected={on}
                      style={{ gridTemplateColumns: GRID }}
                    >
                      <span role="cell" className="job-select-cell"><input
                          type="checkbox"
                          checked={on}
                          onChange={() => toggle(job.id)}
                          aria-label={`${fileName(job.source_rel)} 선택`}
                        /></span>
                      <div role="cell" className="job-meta-cell job-state-cell" data-label="상태">
                        <span className={state ? BADGE_CLASS[state] : "b"} title={stateText}>{stateText}</span>
                      </div>
                      <div role="cell" className="job-name-cell" data-label="작업">
                        <div className="t-name" title={job.source_rel}>
                          <b><Link href={`/jobs/${encodeURIComponent(job.id)}`}>{fileName(job.source_rel)}</Link></b>
                          <span>{parentPath(job.source_rel)}</span>
                          {runtime ? <span title={`전사 서버: ${runtime}`}>전사 서버 · {runtime}</span> : null}
                        </div>
                      </div>
                      <div role="cell" className="job-meta-cell job-phase-cell" data-label="단계">
                        <span className="b line" title={phase}>{phase}</span>
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
            {(data?.total ?? 0) > jobs.length ? (
              <div className="load-more">
                <button type="button" className="btn sec" onClick={() => setLimit((value) => value + 20)}>
                  더 보기 · {jobs.length} / {data?.total ?? 0}
                </button>
              </div>
            ) : null}
          </div>
        </section>
      </div>
    </>
  );
}
