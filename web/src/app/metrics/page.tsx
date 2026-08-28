"use client";

import { useCallback, useMemo, useState } from "react";
import { Freshness } from "@/components/Freshness";
import { Icon } from "@/components/Icon";
import {
  api,
  type MediaDurationMetricGroup,
  type TranslationPassMetric,
} from "@/lib/api";
import { useLiveQuery } from "@/lib/useLiveQuery";

const INTERVAL_MS = 60_000;
const WINDOW_OPTIONS = [30, 90, 365] as const;

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

function bucketLabel(minutes: number): string {
  return `약 ${minutes}분`;
}

function timingCells(metric: {
  sample_count: number;
  media_average_seconds: number;
  average: number;
  p50: number;
  p95: number;
  minimum: number;
  maximum: number;
}) {
  return (
    <>
      <span className="metrics-number" role="cell" data-label="표본">{metric.sample_count}건</span>
      <span className="metrics-number" role="cell" data-label="영상 평균">{mediaDuration(metric.media_average_seconds)}</span>
      <span className="metrics-number metrics-primary" role="cell" data-label="평균">{duration(metric.average)}</span>
      <span className="metrics-number" role="cell" data-label="P50">{duration(metric.p50)}</span>
      <span className="metrics-number" role="cell" data-label="P95">{duration(metric.p95)}</span>
      <span className="metrics-number metrics-range" role="cell" data-label="최소–최대">
        <span>{duration(metric.minimum)} – {duration(metric.maximum)}</span>
      </span>
    </>
  );
}

function StageTable({
  title,
  subtitle,
  rows,
  secondColumn,
  secondValue,
  translation = false,
}: {
  title: string;
  subtitle: string;
  rows: MediaDurationMetricGroup[];
  secondColumn: string;
  secondValue: (row: MediaDurationMetricGroup) => string;
  translation?: boolean;
}) {
  return (
    <section className="card">
      <div className="card-head"><div><h2>{title}</h2><span className="sub">{subtitle}</span></div><span className="sub">{rows.reduce((sum, row) => sum + row.sample_count, 0)}건</span></div>
      <div className="card-body flush">
        <div className="tbl" role="table" aria-label={title}>
          <div className={`tr head metrics-grid${translation ? " translation" : ""}`} role="row">
            <span role="columnheader">영상 구간</span><span role="columnheader">{secondColumn}</span><span role="columnheader" className="r">표본</span><span role="columnheader" className="r">영상 평균</span><span role="columnheader" className="r">평균</span><span role="columnheader" className="r">P50</span><span role="columnheader" className="r">P95</span><span role="columnheader" className="r">최소–최대</span>
          </div>
          {rows.length === 0 ? (
            <div className="empty-state compact metrics-empty"><strong>완료된 표본이 없습니다</strong><span>선택한 기간에 측정된 작업이 쌓이면 여기에 표시됩니다.</span></div>
          ) : rows.map((row) => (
            <div className={`tr tall metrics-grid${translation ? " translation" : ""}`} role="row" key={`${row.media_duration_bucket_minutes}-${row.runtime_id ?? "translation"}`}>
              <strong role="cell" data-label="영상 구간">{bucketLabel(row.media_duration_bucket_minutes)}</strong>
              <span role="cell" data-label={secondColumn}>{secondValue(row)}</span>
              {timingCells({
                sample_count: row.sample_count,
                media_average_seconds: row.media_average_seconds,
                average: row.processing_average_seconds,
                p50: row.processing_p50_seconds,
                p95: row.processing_p95_seconds,
                minimum: row.processing_minimum_seconds,
                maximum: row.processing_maximum_seconds,
              })}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function TranslationPassTable({ rows }: { rows: TranslationPassMetric[] }) {
  const passLabel = (pass: TranslationPassMetric["pass"]) => pass === "draft" ? "초벌 번역" : "검증 번역";
  return (
    <section className="card">
      <div className="card-head"><div><h2>번역 단계별 활성 시간</h2><span className="sub">병렬 요청이 겹친 시간은 한 번만 계산합니다.</span></div><span className="sub">{rows.reduce((sum, row) => sum + row.sample_count, 0)}건</span></div>
      <div className="card-body flush">
        <div className="tbl" role="table" aria-label="번역 단계별 활성 시간">
          <div className="tr head metrics-grid translation" role="row">
            <span role="columnheader">영상 구간</span><span role="columnheader">번역 단계</span><span role="columnheader" className="r">표본</span><span role="columnheader" className="r">영상 평균</span><span role="columnheader" className="r">평균</span><span role="columnheader" className="r">P50</span><span role="columnheader" className="r">P95</span><span role="columnheader" className="r">최소–최대</span>
          </div>
          {rows.length === 0 ? (
            <div className="empty-state compact metrics-empty"><strong>완료된 번역 표본이 없습니다</strong><span>새 번역 작업부터 초벌·검증 시간이 기록됩니다.</span></div>
          ) : rows.map((row) => (
            <div className="tr tall metrics-grid translation" role="row" key={`${row.media_duration_bucket_minutes}-${row.pass}`}>
              <strong role="cell" data-label="영상 구간">{bucketLabel(row.media_duration_bucket_minutes)}</strong>
              <span role="cell" data-label="번역 단계">{passLabel(row.pass)}</span>
              {timingCells({
                sample_count: row.sample_count,
                media_average_seconds: row.media_average_seconds,
                average: row.active_average_seconds,
                p50: row.active_p50_seconds,
                p95: row.active_p95_seconds,
                minimum: row.active_minimum_seconds,
                maximum: row.active_maximum_seconds,
              })}
            </div>
          ))}
        </div>
      </div>
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

  return (
    <>
      <header className="topbar">
        <div className="page-title"><h1>처리 시간 통계</h1><p>영상 길이 구간별 전사·번역 소요 시간을 비교합니다.</p></div>
        <span className="topbar-spacer" />
        <label className="compact-field"><span>조회 기간</span><select className="ctl sm" value={windowDays} onChange={(event) => setWindowDays(Number(event.target.value))}>{WINDOW_OPTIONS.map((days) => <option value={days} key={days}>최근 {days}일</option>)}</select></label>
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        <button type="button" className="btn sec" disabled={refreshing} onClick={() => void refresh()}><Icon name="refresh" size={14} />새로고침</button>
      </header>

      <div className="content">
        <section className="card"><div className="card-body metrics-note"><p><strong>완료된 작업만</strong> 시간 비교 표에 포함합니다. 영상 길이는 가장 가까운 15분 구간으로 묶으며, 정확한 15·30·45분 영상만 뜻하지 않습니다.</p><p className="muted">전사는 Runtime별로 구분합니다. 번역은 단일 번역 서비스 기준이며 Runtime 구분이 없습니다.{excluded > 0 ? ` 실패·중지·차단 표본 ${excluded}건은 완료 통계에서 제외했습니다.` : ""}</p></div></section>

        {!data ? (
          <section className="card"><div className="empty-state"><strong>{status === "error" ? "통계를 불러오지 못했습니다" : "처리 시간 통계를 불러오는 중입니다"}</strong><span>{status === "error" ? (error ?? "Backend 연결을 확인하세요.") : "잠시만 기다려 주세요."}</span></div></section>
        ) : (
          <>
            <StageTable title="전사 소요 시간" subtitle="영상 구간 × 전사 Runtime" rows={transcription} secondColumn="전사 Runtime" secondValue={(row) => row.runtime_name ?? row.runtime_id ?? "Runtime 미확인"} />
            <StageTable title="번역 전체 소요 시간" subtitle="초벌부터 검증 완료까지" rows={translation} secondColumn="범위" secondValue={() => "전체 번역"} translation />
            <TranslationPassTable rows={translationPasses} />
          </>
        )}
      </div>
    </>
  );
}
