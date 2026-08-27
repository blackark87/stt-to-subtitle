"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState, type CSSProperties } from "react";
import { Freshness } from "@/components/Freshness";
import { Icon } from "@/components/Icon";
import { api, type PipelineJob } from "@/lib/api";
import { STATE_LABEL, asJobState, canRetryJob } from "@/lib/domain";
import { fileName } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

const INTERVAL_MS = 10000;

function itemsOf(payload: unknown): Record<string, unknown>[] {
  if (Array.isArray(payload)) return payload as Record<string, unknown>[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["segments", "items"]) {
      if (Array.isArray(record[key])) return record[key] as Record<string, unknown>[];
    }
  }
  return [];
}

function engine(job: PipelineJob): string {
  return String(job.options?.backend ?? "기본 엔진");
}

function segmentTime(item: Record<string, unknown> | undefined): string {
  const seconds = Number(item?.start);
  if (!Number.isFinite(seconds)) return "시간 미확인";
  const minutes = Math.floor(Math.max(0, seconds) / 60);
  const rest = Math.floor(Math.max(0, seconds) % 60);
  return `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
}

export default function ComparisonDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const fetcher = useCallback(() => api.comparison(id), [id]);
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, INTERVAL_MS);
  const [source, setSource] = useState("");
  const [transcripts, setTranscripts] = useState<Record<string, Record<string, unknown>[]>>({});
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const sources = useMemo(() => [...new Set((data?.jobs ?? []).map((job) => job.source_rel))], [data]);
  const activeSource = source && sources.includes(source) ? source : (sources[0] ?? "");
  const jobs = useMemo(() => (data?.jobs ?? []).filter((job) => job.source_rel === activeSource), [activeSource, data]);
  const retryable = (data?.jobs ?? []).some((job) => canRetryJob(asJobState(job.state)));

  useEffect(() => {
    if (!data?.jobs.length) return;
    let cancelled = false;
    void Promise.all(data.jobs.map(async (job) => [job.id, itemsOf(await api.artifact(job.id, "transcript"))] as const))
      .then((entries) => { if (!cancelled) { setTranscripts(Object.fromEntries(entries)); setArtifactError(null); } })
      .catch((reason) => { if (!cancelled) setArtifactError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { cancelled = true; };
  }, [data]);

  const rowCount = Math.max(0, ...jobs.map((job) => transcripts[job.id]?.length ?? 0));
  const rows = Array.from({ length: rowCount }, (_, index) => index);

  const retry = async () => {
    setBusy(true);
    setActionError(null);
    try {
      await api.retryComparison(id);
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
        <Link className="btn sec" href="/comparisons"><Icon name="chevron_left" size={14} />비교 목록</Link>
        <div className="page-title">
          <h1>전사 결과 비교</h1>
          <p className="code">{id.slice(0, 12)}</p>
        </div>
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
      </header>

      <div className="content">
        <section className="card">
          <div className="card-head comparison-controls">
            <label className="compact-field">
              <span>비교 원본</span>
              <select className="ctl" value={activeSource} onChange={(event) => setSource(event.target.value)}>
                {sources.map((value) => <option key={value} value={value}>{fileName(value)}</option>)}
              </select>
            </label>
            <div className="btns">
              <button type="button" className="btn sec" disabled={busy || !retryable} onClick={() => void retry()}><Icon name="refresh" size={14} />실패 작업 재시도</button>
            </div>
          </div>
          <div className="comparison-summary">
            {jobs.map((job) => {
              const jobState = asJobState(job.state);
              return (
                <Link href={`/jobs/${encodeURIComponent(job.id)}`} className="comparison-engine" key={job.id}>
                  <span className="eyebrow">{engine(job)}</span>
                  <strong>{jobState ? STATE_LABEL[jobState] : job.state}</strong>
                  <span>{transcripts[job.id]?.length ?? 0}개 구간</span>
                </Link>
              );
            })}
          </div>
        </section>

        {actionError ? <p className="notice error" role="alert">{actionError}</p> : null}
        {artifactError ? <p className="notice error" role="alert">{artifactError}</p> : null}
        <section className="card comparison-result" aria-labelledby="comparison-result-title">
          <div className="card-head"><div><h2 id="comparison-result-title">순번별 전사</h2><span className="sub" title="엔진마다 구간 분할이 다를 수 있어 각 시작 시각을 함께 표시합니다.">엔진마다 구간 분할이 다를 수 있어 각 시작 시각을 함께 표시합니다.</span></div><span className="sub" title={`최대 ${rowCount}개 구간`}>최대 {rowCount}개 구간</span></div>
          {jobs.length === 0 || rowCount === 0 ? (
            <div className="empty-state"><strong>{status === "loading" ? "비교 결과를 불러오는 중입니다" : "비교할 전사 결과가 없습니다"}</strong></div>
          ) : (
            <div className="comparison-table" style={{ "--engine-count": jobs.length } as CSSProperties}>
              <div className="comparison-row comparison-head">
                <span>구간</span>
                {jobs.map((job) => <strong key={job.id}>{engine(job)}</strong>)}
              </div>
              {rows.map((index) => (
                <div className="comparison-row" key={index}>
                  <span className="code">{index + 1}</span>
                  {jobs.map((job) => {
                    const item = transcripts[job.id]?.[index];
                    return <p key={job.id}><span className="comparison-time code">{segmentTime(item)}</span><span lang="ja">{String(item?.text ?? "—")}</span></p>;
                  })}
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </>
  );
}
