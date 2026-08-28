"use client";

import { useCallback, useMemo, useState } from "react";
import { Freshness } from "@/components/Freshness";
import { Icon } from "@/components/Icon";
import { api } from "@/lib/api";
import { useLiveQuery } from "@/lib/useLiveQuery";

const INTERVAL_MS = 60_000;
const WINDOW_OPTIONS = [30, 90, 365] as const;

interface MetricView {
  key: string;
  bucket: number;
  mediaAverage: number;
  contextLabel: string;
  context: string;
  sampleCount: number;
  average: number;
  p50: number;
  p95: number;
  minimum: number;
  maximum: number;
}

function duration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours > 0) return `${hours}시간 ${minutes}분`;
  if (minutes > 0) return `${minutes}분 ${String(rest).padStart(2, "0")}초`;
  return `${rest}초`;
}

function mediaDuration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "—";
  return `${Math.round(seconds / 60)}분`;
}

function MetricRecord({ row }: { row: MetricView }) {
  const values = [
    ["표본", `${row.sampleCount}건`],
    ["평균", duration(row.average)],
    ["P50", duration(row.p50)],
    ["P95", duration(row.p95)],
    ["최소", duration(row.minimum)],
    ["최대", duration(row.maximum)],
  ];
  return (
    <article className="metrics-record" role="listitem">
      <div className="metrics-scope">
        <span className="eyebrow">영상 구간</span>
        <strong>약 {row.bucket}분</strong>
        <small>실제 평균 {mediaDuration(row.mediaAverage)}</small>
      </div>
      <div className="metrics-context">
        <span className="eyebrow">{row.contextLabel}</span>
        <strong title={row.context}>{row.context}</strong>
      </div>
      <div className="metrics-values">
        {values.map(([label, value]) => (
          <div className={label === "평균" ? "metrics-value primary" : "metrics-value"} key={label}>
            <span>{label}</span>
            <strong title={value}>{value}</strong>
          </div>
        ))}
      </div>
    </article>
  );
}

function MetricsSection({
  title,
  subtitle,
  rows,
  emptyTitle,
  emptyText,
}: {
  title: string;
  subtitle: string;
  rows: MetricView[];
  emptyTitle: string;
  emptyText: string;
}) {
  const samples = rows.reduce((sum, row) => sum + row.sampleCount, 0);
  return (
    <section className="card metrics-section">
      <div className="card-head metrics-section-head">
        <div><h2>{title}</h2><span className="sub" title={subtitle}>{subtitle}</span></div>
        <span className="b line">{samples}건</span>
      </div>
      {rows.length === 0 ? (
        <div className="empty-state compact metrics-empty"><strong>{emptyTitle}</strong><span>{emptyText}</span></div>
      ) : (
        <div className="metrics-records" role="list">
          {rows.map((row) => <MetricRecord row={row} key={row.key} />)}
        </div>
      )}
    </section>
  );
}

export default function MetricsPage() {
  const [windowDays, setWindowDays] = useState<number>(30);
  const fetcher = useCallback(() => api.mediaDurationMetrics(windowDays), [windowDays]);
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, INTERVAL_MS);
  const completed = useMemo(() => (data?.groups ?? []).filter((row) => row.outcome === "completed"), [data]);
  const transcription = completed.filter((row) => row.phase === "transcription");
  const translation = completed.filter((row) => row.phase === "translation");
  const translationPasses = (data?.translation_passes ?? []).filter((row) => row.outcome === "completed");
  const excluded = (data?.groups ?? []).filter((row) => row.outcome !== "completed").reduce((sum, row) => sum + row.sample_count, 0);

  const transcriptionRows: MetricView[] = transcription.map((row) => ({
    key: `${row.media_duration_bucket_minutes}-${row.runtime_id ?? "unknown"}`,
    bucket: row.media_duration_bucket_minutes,
    mediaAverage: row.media_average_seconds,
    contextLabel: "전사 Runtime",
    context: row.runtime_name ?? row.runtime_id ?? "Runtime 미확인",
    sampleCount: row.sample_count,
    average: row.processing_average_seconds,
    p50: row.processing_p50_seconds,
    p95: row.processing_p95_seconds,
    minimum: row.processing_minimum_seconds,
    maximum: row.processing_maximum_seconds,
  }));
  const translationRows: MetricView[] = translation.map((row) => ({
    key: `${row.media_duration_bucket_minutes}-translation`,
    bucket: row.media_duration_bucket_minutes,
    mediaAverage: row.media_average_seconds,
    contextLabel: "처리 범위",
    context: "전체 번역",
    sampleCount: row.sample_count,
    average: row.processing_average_seconds,
    p50: row.processing_p50_seconds,
    p95: row.processing_p95_seconds,
    minimum: row.processing_minimum_seconds,
    maximum: row.processing_maximum_seconds,
  }));
  const passRows: MetricView[] = translationPasses.map((row) => ({
    key: `${row.media_duration_bucket_minutes}-${row.pass}`,
    bucket: row.media_duration_bucket_minutes,
    mediaAverage: row.media_average_seconds,
    contextLabel: "번역 단계",
    context: row.pass === "draft" ? "초벌 번역" : "검증 번역",
    sampleCount: row.sample_count,
    average: row.active_average_seconds,
    p50: row.active_p50_seconds,
    p95: row.active_p95_seconds,
    minimum: row.active_minimum_seconds,
    maximum: row.active_maximum_seconds,
  }));
  const transcriptionSamples = transcription.reduce((sum, row) => sum + row.sample_count, 0);
  const translationSamples = translation.reduce((sum, row) => sum + row.sample_count, 0);
  const passSamples = translationPasses.reduce((sum, row) => sum + row.sample_count, 0);

  return (
    <>
      <header className="topbar">
        <div className="page-title"><h1>처리 시간 통계</h1><p>영상 길이 구간별 전사·번역 소요 시간을 비교합니다.</p></div>
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        <button type="button" className="btn sec" disabled={refreshing} onClick={() => void refresh()}><Icon name="refresh" size={14} />새로고침</button>
      </header>

      <div className="content metrics-content">
        <section className="card">
          <div className="card-body metrics-toolbar">
            <div className="metrics-note">
              <strong>완료된 작업의 실제 처리 시간</strong>
              <p>영상 길이는 가장 가까운 15분 구간으로 묶습니다. 전사는 Runtime별로, 번역은 전체·초벌·검증 단계별로 구분합니다.</p>
              {excluded > 0 ? <small>실패·중지·차단 표본 {excluded}건은 완료 통계에서 제외했습니다.</small> : null}
            </div>
            <label className="compact-field metrics-window">
              <span>조회 기간</span>
              <select className="ctl" value={windowDays} onChange={(event) => setWindowDays(Number(event.target.value))}>
                {WINDOW_OPTIONS.map((days) => <option value={days} key={days}>최근 {days}일</option>)}
              </select>
            </label>
          </div>
        </section>

        {!data ? (
          <section className="card"><div className="empty-state"><strong>{status === "error" ? "통계를 불러오지 못했습니다" : "처리 시간 통계를 불러오는 중입니다"}</strong><span>{status === "error" ? (error ?? "Backend 연결을 확인하세요.") : "잠시만 기다려 주세요."}</span></div></section>
        ) : (
          <>
            <section className="summary-grid metrics-summary" aria-label="처리 시간 표본 요약">
              <article className="summary-card"><span>전사 완료 표본</span><strong>{transcriptionSamples}</strong><small>Runtime별 집계</small></article>
              <article className="summary-card"><span>번역 완료 표본</span><strong>{translationSamples}</strong><small>전체 번역 시간</small></article>
              <article className="summary-card"><span>단계별 번역 표본</span><strong>{passSamples}</strong><small>초벌·검증 활성 시간</small></article>
              <article className={excluded > 0 ? "summary-card attention" : "summary-card"}><span>완료 제외 표본</span><strong>{excluded}</strong><small>실패·중지·차단</small></article>
            </section>
            <MetricsSection title="전사 소요 시간" subtitle="영상 구간 × 전사 Runtime" rows={transcriptionRows} emptyTitle="완료된 전사 표본이 없습니다" emptyText="선택한 기간에 측정된 전사 작업이 쌓이면 여기에 표시됩니다." />
            <MetricsSection title="번역 전체 소요 시간" subtitle="초벌 시작부터 검증 완료까지" rows={translationRows} emptyTitle="완료된 번역 표본이 없습니다" emptyText="선택한 기간에 완료된 번역 작업이 쌓이면 여기에 표시됩니다." />
            <MetricsSection title="번역 단계별 활성 시간" subtitle="병렬 요청이 겹친 시간은 한 번만 계산" rows={passRows} emptyTitle="단계별 번역 표본이 없습니다" emptyText="새 번역 작업부터 초벌·검증 시간이 기록됩니다." />
          </>
        )}
      </div>
    </>
  );
}
