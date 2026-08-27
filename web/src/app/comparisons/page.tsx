"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api } from "@/lib/api";
import { clock, fileName } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

const INTERVAL_MS = 10000;

export default function ComparisonsPage() {
  const fetcher = useCallback(() => api.comparisons(), []);
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, INTERVAL_MS);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const items = data?.items ?? [];

  const retry = async (id: string) => {
    setBusyId(id);
    setActionError(null);
    try {
      await api.retryComparison(id);
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <header className="topbar">
        <div className="page-title">
          <h1>전사 비교</h1>
          <p>동일한 원본을 엔진별로 대조하고 결과 차이를 확인합니다.</p>
        </div>
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        <button type="button" className="btn sec" disabled={refreshing} onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />새로고침
        </button>
      </header>

      <div className="content">
        {actionError ? <p className="notice error" role="alert">{actionError}</p> : null}
        <section className="card">
          <div className="card-head">
            <div><h2>비교 기록</h2><span className="sub m" title={`${data?.total ?? 0}건`}>{data?.total ?? 0}건</span></div>
            <Link className="btn" href="/media?operation=compare"><Icon name="compare" size={14} />새 비교</Link>
          </div>
          <div className="card-body flush">
            <div className="tbl" role="table" aria-label="전사 비교 기록">
              <div className="tr head comparison-grid" role="row">
                <span role="columnheader">대상</span><span role="columnheader" className="r">동작</span>
              </div>
              {items.length === 0 ? (
                <div className="empty-state">
                  <Icon name="compare" size={24} />
                  <strong>{status === "loading" ? "비교 기록을 불러오는 중입니다" : "아직 비교 기록이 없습니다"}</strong>
                  <span>미디어 화면에서 전사 비교 작업을 시작할 수 있습니다.</span>
                </div>
              ) : items.map((item) => {
                const retryable = item.jobs.some((job) => ["blocked", "failed", "stopped"].includes(job.state));
                return (
                  <div key={item.id} className="tr tall comparison-grid" role="row">
                    <div className="t-name" role="cell" data-label="대상" title={(item.source_rels ?? []).join(", ")}>
                      <b><Link href={`/comparisons/${encodeURIComponent(item.id)}`}>{(item.source_rels ?? []).map(fileName).join(", ") || "—"}</Link></b>
                      <span>{item.jobs.length}개 엔진 · 최근 갱신 {clock(item.updated_at)}</span>
                    </div>
                    <span className="btns r" role="cell" data-label="동작">
                      {retryable ? <button type="button" className="btn sec sm" disabled={busyId === item.id} onClick={() => void retry(item.id)}><Icon name="refresh" size={13} />재시도</button> : null}
                      <Link className="btn sec sm" href={`/comparisons/${encodeURIComponent(item.id)}`}>결과 비교<Icon name="chevron_right" size={13} /></Link>
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
