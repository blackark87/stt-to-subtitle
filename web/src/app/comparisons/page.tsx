"use client";

import { useCallback } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api } from "@/lib/api";
import { STATE_LABEL, asJobState, type JobState } from "@/lib/domain";
import { fileName } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

const INTERVAL_MS = 10000;

const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

export default function ComparisonsPage() {
  const fetcher = useCallback(() => api.comparisons(), []);
  const { data, status, error, updatedAt, refresh } = useLiveQuery(fetcher, INTERVAL_MS);
  const items = data?.items ?? [];

  return (
    <>
      <header className="topbar">
        <h1>전사 비교</h1>
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
            <h2>비교 기록</h2>
            <span className="sub m">{data?.total ?? 0}건</span>
          </div>
          <div className="card-body flush">
            <div className="tbl">
              <div className="tr head" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 320px)" }}>
                <span>대상</span>
                <span>엔진별 상태</span>
              </div>
              {items.length === 0 ? (
                <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                  {status === "loading" ? "불러오는 중" : "비교 기록 없음"}
                </div>
              ) : (
                items.map((item) => (
                  <div key={item.id} className="tr tall" style={{ gridTemplateColumns: "minmax(0, 1fr) minmax(0, 320px)" }}>
                    <div className="t-name">
                      <b>{(item.source_rels ?? []).map(fileName).join(", ") || "—"}</b>
                      <span className="code" style={{ fontSize: ".7rem" }}>{String(item.id ?? "").slice(0, 8)}</span>
                    </div>
                    <span className="btns">
                      {(item.jobs ?? []).map((job) => {
                        const state = asJobState(job.state);
                        const backend = String(job.options?.backend ?? "");
                        return (
                          <span key={job.id} className={state ? BADGE_CLASS[state] : "b"}>
                            {backend ? `${backend} · ` : ""}
                            {state ? STATE_LABEL[state] : job.state}
                          </span>
                        );
                      })}
                    </span>
                  </div>
                ))
              )}
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
