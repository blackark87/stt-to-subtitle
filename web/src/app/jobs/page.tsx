"use client";

import { useCallback, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api } from "@/lib/api";
import {
  PHASE_LABEL,
  STATE_LABEL,
  STATE_ORDER,
  asJobState,
  reasonLabel,
  type JobPhase,
  type JobState,
} from "@/lib/domain";
import { clock, fileName, parentPath } from "@/lib/format";
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

const GRID = "36px 92px minmax(0, 1fr) 110px minmax(0, 220px) 100px";

export default function JobsPage() {
  const [filter, setFilter] = useState<readonly JobState[]>([]);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetcher = useCallback(
    () => api.jobs({ limit: 200, state: filter.length ? filter : undefined }),
    [filter],
  );
  const { data, status, error, updatedAt, refresh } = useLiveQuery(fetcher, JOBS_INTERVAL_MS);
  const jobs = data?.items ?? [];

  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const run = async (action: (ids: string[]) => Promise<unknown>) => {
    const ids = [...selected];
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
        <span style={{ marginLeft: "auto" }}>
          <Freshness status={status} updatedAt={updatedAt} error={error} />
        </span>
        <button type="button" className="btn sec sm" onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />
          새로고침
        </button>
      </header>

      <div className="content">
        <section className="card">
          <div className="card-head">
            <h2>필터</h2>
            {filter.length ? (
              <button type="button" className="btn sec sm" onClick={() => setFilter([])}>
                전체 해제
              </button>
            ) : null}
          </div>
          <div className="card-body">
            <div className="rail">
              {STATE_ORDER.map((state) => {
                const on = filter.includes(state);
                return (
                  <button
                    key={state}
                    type="button"
                    aria-pressed={on}
                    className={on ? "chip on" : "chip"}
                    style={{ paddingLeft: 9 }}
                    onClick={() =>
                      setFilter((current) =>
                        current.includes(state)
                          ? current.filter((value) => value !== state)
                          : [...current, state],
                      )
                    }
                  >
                    {STATE_LABEL[state]}
                  </button>
                );
              })}
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
              <button type="button" className="btn sec sm" disabled={!selected.size || busy} onClick={() => void run(api.retryJobs)}>
                <Icon name="refresh" size={13} />
                재시도
              </button>
              <button type="button" className="btn sec sm" disabled={!selected.size || busy} onClick={() => void run(api.pauseTranslations)}>
                <Icon name="pause" size={13} />
                번역 일시정지
              </button>
              <button type="button" className="btn dgr sm" disabled={!selected.size || busy} onClick={() => void run(api.stopJobs)}>
                <Icon name="alert_triangle" size={13} />
                정지
              </button>
            </span>
          </div>
          <div className="card-body flush">
            {actionError ? (
              <p role="alert" style={{ margin: 0, padding: "8px 12px", color: "var(--bad)", fontSize: ".82rem" }}>
                {actionError}
              </p>
            ) : null}
            <div className="tbl">
              <div className="tr head" style={{ gridTemplateColumns: GRID }}>
                <span />
                <span>상태</span>
                <span>작업</span>
                <span>단계</span>
                <span>사유</span>
                <span className="r">갱신</span>
              </div>
              {jobs.length === 0 ? (
                <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                  {status === "loading" ? "불러오는 중" : "조건에 맞는 작업 없음"}
                </div>
              ) : (
                jobs.map((job) => {
                  const state = asJobState(job.state);
                  const reason = reasonLabel(job.reason_code) ?? job.error ?? "—";
                  const on = selected.has(job.id);
                  return (
                    <div
                      key={job.id}
                      className={`tr ${state ? ROW_CLASS[state] : ""}`}
                      aria-selected={on}
                      style={{ gridTemplateColumns: GRID }}
                    >
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={() => toggle(job.id)}
                        aria-label={`${fileName(job.source_rel)} 선택`}
                      />
                      <span className={state ? BADGE_CLASS[state] : "b"}>
                        {state ? STATE_LABEL[state] : job.state}
                      </span>
                      <div className="t-name">
                        <b>{fileName(job.source_rel)}</b>
                        <span>{parentPath(job.source_rel)}</span>
                      </div>
                      <span className="b line">{PHASE_LABEL[job.phase as JobPhase] ?? job.phase}</span>
                      <span
                        className="m"
                        title={reason}
                        style={{ fontSize: ".76rem", color: "var(--muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                      >
                        {reason}
                      </span>
                      <span className="r m" style={{ fontSize: ".78rem", color: "var(--muted)" }}>
                        {clock(job.updated_at)}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
