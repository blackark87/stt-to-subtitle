"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { Freshness } from "@/components/Freshness";
import { Icon } from "@/components/Icon";
import { api, type GpuDevice, type LibraryProgressSegment, type PipelineJob } from "@/lib/api";
import {
  JOB_PHASES,
  PHASE_LABEL,
  STATE_LABEL,
  asJobState,
  canResumeTranslation,
  canRetryJob,
  reasonLabel,
  type JobPhase,
  type JobState,
} from "@/lib/domain";
import { clock, elapsed, fileName, parentPath, percent } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

const DASHBOARD_INTERVAL_MS = 5000;
const ATTENTION: JobState[] = ["blocked", "failed", "paused", "stopped"];
const PIPELINE_PHASES = JOB_PHASES.filter((phase) => phase !== "complete");

const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

const LIBRARY_SEGMENT_CLASS: Record<LibraryProgressSegment["state"], string> = {
  done: "s-ok",
  running: "s-run",
  waiting: "s-wait",
  attention: "s-bad",
  unprocessed: "s-idle",
};

const LIBRARY_SEGMENT_LABEL: Record<LibraryProgressSegment["state"], string> = {
  done: "완료",
  running: "진행 중",
  waiting: "대기",
  attention: "확인 필요",
  unprocessed: "미처리",
};

function jobPercent(job: PipelineJob): number | null {
  if (job.phase === "transcription") {
    return percent(job.chunks_completed, Math.max(job.chunks_created, job.chunks_total_estimate));
  }
  if (job.phase === "translation") {
    return percent(job.translation_chunks_completed, job.translation_chunks_total);
  }
  return null;
}

function gpuErrorLabel(code?: string): string {
  const labels: Record<string, string> = {
    connection_error: "Prometheus 연결 실패",
    timeout: "Prometheus 응답 시간 초과",
    http_error: "Prometheus HTTP 오류",
    invalid_response: "Prometheus 응답 형식 오류",
    no_dcgm_metrics: "DCGM 메트릭 없음",
  };
  return labels[code ?? ""] ?? "GPU 메트릭 수집 실패";
}

function epochTime(value?: number | null): string {
  if (!value) return "기록 없음";
  return new Date(value * 1000).toLocaleTimeString("ko-KR", { hour12: false });
}

function dependencyLabel(value: string): string {
  const labels: Record<string, string> = {
    ready: "정상",
    closed: "정상",
    available: "정상",
    ok: "정상",
    checking: "확인 중",
    unknown: "확인 필요",
    unavailable: "연결 불가",
    open: "요청 차단",
    half_open: "복구 확인 중",
    disabled: "사용 안 함",
  };
  return labels[value] ?? value;
}

function gpuMemory(device: GpuDevice): {
  percent: number | null;
  percentLabel: string;
  detail: string;
} {
  const usedGib = device.memory_used_gib
    ?? (device.memory_used_mib == null ? null : device.memory_used_mib / 1024);
  const totalGib = device.memory_total_gib
    ?? (device.memory_total_mib == null ? null : device.memory_total_mib / 1024);
  const calculatedPercent = usedGib != null && totalGib != null && totalGib > 0
    ? Math.max(0, Math.min(100, (usedGib / totalGib) * 100))
    : null;
  const memoryPercent = device.memory_percent ?? calculatedPercent;
  const percentLabel = memoryPercent == null
    ? "—"
    : memoryPercent > 0 && memoryPercent < 1
      ? "<1"
      : memoryPercent.toFixed(0);
  if (usedGib == null || totalGib == null) {
    return { percent: memoryPercent, percentLabel, detail: "사용량 확인 불가" };
  }
  const used = usedGib < 0.1
    ? `${Math.round(usedGib * 1024)} MiB`
    : `${usedGib.toFixed(1)} GiB`;
  return {
    percent: memoryPercent,
    percentLabel,
    detail: `${used} / ${totalGib.toFixed(1)} GiB`,
  };
}

export default function DashboardPage() {
  const fetcher = useCallback(() => api.dashboard(), []);
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, DASHBOARD_INTERVAL_MS);
  const libraryProgress = useLiveQuery(useCallback(() => api.libraryProgress(), []), 60000);
  const [actingId, setActingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const active = data?.active_jobs ?? data?.recent_jobs ?? [];
  const running = active.filter((job) => job.state === "running");
  const waiting = active.filter((job) => job.state === "waiting");
  const attention = data?.attention_jobs
    ?? active.filter((job) => ATTENTION.includes(job.state as JobState));
  const completed = data?.recent_completed ?? (data?.recent_jobs ?? []).filter((job) => job.state === "done");
  const counts = data?.state_counts ?? {};
  const attentionTotal = ATTENTION.reduce((sum, state) => sum + (counts[state] ?? 0), 0);
  const visibleAttention = attention.slice(0, 10);
  const visibleCompleted = completed.slice(0, 10);
  const gpu = data?.gpu ?? null;

  const act = async (id: string, task: () => Promise<unknown>) => {
    setActingId(id);
    setActionError(null);
    try {
      await task();
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setActingId(null);
    }
  };

  return (
    <>
      <header className="topbar">
        <div className="page-title">
          <h1>대시보드</h1>
          <p>전사·번역·자막 생성 상태를 한눈에 확인합니다.</p>
        </div>
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        <button type="button" className="btn sec" disabled={refreshing} onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />새로고침
        </button>
        <Link className="btn" href="/media"><Icon name="play" size={14} />새 작업</Link>
      </header>

      <div className="content dashboard-content">
        {!data ? (
          <section className="card">
            <div className="empty-state">
              <strong>{status === "error" ? "대시보드에 연결할 수 없습니다" : "운영 현황을 불러오는 중입니다"}</strong>
              <span>{status === "error" ? (error ?? "Backend 연결을 확인하세요.") : "잠시만 기다려 주세요."}</span>
              {status === "error" ? <button type="button" className="btn sec" onClick={() => void refresh()}><Icon name="refresh" size={14} />다시 시도</button> : null}
            </div>
          </section>
        ) : null}

        <section className="summary-grid" aria-label="작업 상태 요약" hidden={!data}>
          <Link href="/jobs?state=running" className="summary-card running">
            <span>진행 중</span><strong>{counts.running ?? 0}</strong><small>현재 처리 중인 작업</small>
          </Link>
          <Link href="/jobs?state=waiting" className="summary-card waiting">
            <span>대기</span><strong>{counts.waiting ?? 0}</strong><small>처리를 기다리는 작업</small>
          </Link>
          <Link href="/jobs?state=blocked&state=failed&state=paused&state=stopped" className={attentionTotal ? "summary-card attention" : "summary-card"}>
            <span>확인 필요</span><strong>{attentionTotal}</strong><small>중단·실패·일시 정지</small>
          </Link>
          <Link href="/jobs?state=done" className="summary-card complete">
            <span>완료</span><strong>{counts.done ?? 0}</strong><small>누적 완료 작업</small>
          </Link>
        </section>

        {actionError ? <p className="notice error" role="alert">{actionError}</p> : null}

        <section className="card pipeline-card" aria-labelledby="pipeline-title" hidden={!data}>
          <div className="card-head">
            <div><h2 id="pipeline-title">현재 파이프라인</h2><span className="sub" title="실행 작업의 현재 단계를 기준으로 표시합니다.">실행 작업의 현재 단계를 기준으로 표시합니다.</span></div>
            <span className="b run">{running.length}건 실행 중</span>
          </div>
          <div className="card-body pipeline-grid">
            {PIPELINE_PHASES.map((phase) => {
              const jobs = running.filter((job) => job.phase === phase);
              const primary = jobs[0];
              const pct = primary ? jobPercent(primary) : null;
              return (
                <div className={primary ? "pipeline-stage is-active" : "pipeline-stage"} key={phase}>
                  <div className="pipeline-stage-head">
                    <span className="eyebrow">{PHASE_LABEL[phase]}</span>
                    <span className={primary ? "b run dot" : "b"}>{primary ? `${jobs.length}건 실행` : "유휴"}</span>
                  </div>
                  {primary ? (
                    <>
                      <Link className="pipeline-job" title={primary.source_rel} href={`/jobs/${encodeURIComponent(primary.id)}`}>{fileName(primary.source_rel)}</Link>
                      <span className="muted truncate" title={parentPath(primary.source_rel)}>{parentPath(primary.source_rel)}</span>
                      <div className="progress-line">
                        <progress max={100} value={pct ?? undefined} aria-label={`${PHASE_LABEL[phase]} 진행률`} />
                        <span>{pct == null ? "—" : `${pct}%`}</span>
                      </div>
                    </>
                  ) : <span className="pipeline-idle-copy">실행 중인 작업이 없습니다.</span>}
                </div>
              );
            })}
          </div>
        </section>

        <div className="dashboard-layout" hidden={!data}>
          <div className="dashboard-main">
            <section className="card" aria-labelledby="running-title">
              <div className="card-head"><h2 id="running-title">실행 중 작업</h2><Link href="/jobs?state=running" className="text-link">전체 보기<Icon name="chevron_right" size={13} /></Link></div>
              <div className="card-body flush">
                {running.length === 0 ? <div className="empty-state compact"><strong>실행 중인 작업이 없습니다</strong></div> : (
                  <div className="job-list">
                    {running.map((job) => {
                      const pct = jobPercent(job);
                      return (
                        <Link href={`/jobs/${encodeURIComponent(job.id)}`} className="job-row" key={job.id}>
                          <span className="job-state-line running" />
                          <span className="t-name" title={job.source_rel}><b>{fileName(job.source_rel)}</b><span>{parentPath(job.source_rel)}</span></span>
                          <span className="b line">{PHASE_LABEL[job.phase as JobPhase] ?? job.phase}</span>
                          <span className="job-progress"><progress max={100} value={pct ?? undefined} aria-label="작업 진행률" /><b>{pct == null ? "—" : `${pct}%`}</b></span>
                          <time className="m">{clock(job.updated_at)}</time>
                        </Link>
                      );
                    })}
                  </div>
                )}
              </div>
            </section>

            <section className="card attention-card" aria-labelledby="attention-title">
                <div className="card-head">
                  <div><h2 id="attention-title">확인 필요</h2><span className="sub" title={`최근 ${visibleAttention.length}건 · 전체 ${attentionTotal}건`}>최근 {visibleAttention.length}건 · 전체 {attentionTotal}건</span></div>
                  <Link href="/jobs?state=blocked&state=failed&state=paused&state=stopped" className="text-link">전체 보기<Icon name="chevron_right" size={13} /></Link>
                </div>
                <div className="card-body flush">
                  {visibleAttention.length === 0 ? (
                    <div className="empty-state compact"><strong>확인이 필요한 작업이 없습니다</strong></div>
                  ) : <div className="job-list">
                    {visibleAttention.map((job) => {
                      const jobState = asJobState(job.state);
                      const reason = reasonLabel(job.reason_code) ?? job.error ?? "원인 정보 없음";
                      return (
                        <div className="job-row attention-row" key={job.id}>
                          <span className={`job-state-line ${jobState === "failed" ? "failed" : "stopped"}`} />
                          <span className="t-name" title={job.source_rel}><b><Link href={`/jobs/${encodeURIComponent(job.id)}`}>{fileName(job.source_rel)}</Link></b><span>{PHASE_LABEL[job.phase as JobPhase] ?? job.phase} · {clock(job.updated_at)}</span></span>
                          <span className={jobState ? BADGE_CLASS[jobState] : "b"}>{jobState ? STATE_LABEL[jobState] : job.state}</span>
                          <span className="reason-text" title={reason}>{reason}</span>
                          <span className="btns">
                            {canResumeTranslation(jobState) ? <button type="button" className="btn sec sm" disabled={actingId != null} onClick={() => void act(job.id, () => api.resumeTranslation(job.id))}><Icon name="play" size={13} />재개</button> : null}
                            {canRetryJob(jobState) ? <button type="button" className="btn sec sm" disabled={actingId != null} onClick={() => void act(job.id, () => api.retryJob(job.id))}><Icon name="refresh" size={13} />재시도</button> : null}
                            {canRetryJob(jobState) ? <button type="button" className="btn dgr sm" disabled={actingId != null} onClick={() => {
                              if (!window.confirm(`${fileName(job.source_rel)} 기록을 삭제할까요?`)) return;
                              void act(job.id, () => api.deleteJob(job.id));
                            }}><Icon name="trash" size={13} />삭제</button> : null}
                          </span>
                        </div>
                      );
                    })}
                  </div>}
                </div>
              </section>

            <section className="card" aria-labelledby="completed-title">
              <div className="card-head"><div><h2 id="completed-title">최근 완료</h2><span className="sub" title={`최근 ${visibleCompleted.length}건 · 전체 ${counts.done ?? 0}건`}>최근 {visibleCompleted.length}건 · 전체 {counts.done ?? 0}건</span></div><Link href="/jobs?state=done" className="text-link">전체 보기<Icon name="chevron_right" size={13} /></Link></div>
              <div className="card-body flush">
                {completed.length === 0 ? <div className="empty-state compact"><strong>완료된 작업이 없습니다</strong></div> : (
                  <div className="job-list">
                    {visibleCompleted.map((job) => (
                      <Link href={`/jobs/${encodeURIComponent(job.id)}`} className="job-row completed-row" key={job.id}>
                        <span className="job-state-line complete" />
                        <span className="t-name" title={job.source_rel}><b>{fileName(job.source_rel)}</b><span>{parentPath(job.source_rel)}</span></span>
                        <span>{String(job.options?.backend ?? "기본")}</span>
                        <strong className="m">{elapsed(job.created_at, job.updated_at)}</strong>
                        <time className="m">{clock(job.updated_at)}</time>
                      </Link>
                    ))}
                  </div>
                )}
              </div>
            </section>
          </div>

          <aside className="dashboard-aside">
            <section className="card gpu-card" aria-labelledby="gpu-title">
              <div className="card-head">
                <div><h2 id="gpu-title">GPU</h2><span className="sub" title={gpu?.available ? "정상 수집" : gpu?.configured ? gpuErrorLabel(gpu.error_code) : "미설정"}>{gpu?.available ? "정상 수집" : gpu?.configured ? gpuErrorLabel(gpu.error_code) : "미설정"}</span></div>
                <span className={gpu?.available ? "b ok dot" : gpu?.stale ? "b wait dot" : "b bad dot"}>{gpu?.available ? "정상" : gpu?.stale ? "이전 값" : "수집 안 됨"}</span>
              </div>
              <div className="card-body">
                {!gpu?.configured ? (
                  <div className="empty-state compact"><strong>GPU 모니터링이 설정되지 않았습니다</strong><span>Backend에 Prometheus URL과 공유 네트워크를 설정하세요.</span></div>
                ) : gpu.devices.length ? gpu.devices.map((device, index) => {
                  const memory = gpuMemory(device);
                  return (
                    <div className="gpu-device" key={device.index ?? index}>
                      <div className="gpu-name"><strong>{device.display_name ?? `GPU ${device.index ?? index}`}</strong>{gpu.stale ? <span>마지막 정상값 {epochTime(gpu.last_success_at)}</span> : null}</div>
                      <div className="metric-grid">
                        <div className="metric primary"><span>사용률</span><strong>{device.utilization_percent?.toFixed(0) ?? "—"}<small>%</small></strong></div>
                        <div className="metric primary"><span>메모리</span><strong>{memory.percentLabel}<small>%</small></strong><span className="metric-detail">{memory.detail}</span><progress max={100} value={memory.percent ?? undefined} aria-label={`GPU 메모리 사용량 ${memory.detail}`} /></div>
                        <div className="metric"><span>온도</span><strong>{device.temperature_celsius?.toFixed(0) ?? "—"}<small>°C</small></strong></div>
                        <div className="metric"><span>전력</span><strong>{device.power_watts?.toFixed(0) ?? "—"}<small>W</small></strong></div>
                      </div>
                    </div>
                  );
                }) : (
                  <div className="empty-state compact">
                    <Icon name="alert_triangle" size={22} />
                    <strong>{gpuErrorLabel(gpu.error_code)}</strong>
                    <span>{gpu.error || "Prometheus와 DCGM Exporter 연결을 확인하세요."}</span>
                    <button type="button" className="btn sec sm" onClick={() => void refresh()}><Icon name="refresh" size={13} />다시 확인</button>
                  </div>
                )}
              </div>
            </section>

            <section className="card" aria-labelledby="dependency-title">
              <div className="card-head"><h2 id="dependency-title">서비스 상태</h2></div>
              <div className="card-body service-list">
                {Object.entries(data?.dependencies ?? {}).map(([name, dependency]) => {
                  const value = typeof dependency === "string"
                    ? dependency
                    : String(dependency.state ?? "unknown");
                  const detail = typeof dependency === "string"
                    ? ""
                    : String(dependency.detail ?? "");
                  const healthy = ["ready", "closed", "available", "ok"].includes(value);
                  const failed = ["unavailable", "open"].includes(value);
                  return <div className="service-row" key={name}><span>{name === "transcription" ? "전사 서비스" : "번역 서비스"}</span><span className={healthy ? "b ok dot" : failed ? "b bad dot" : "b wait dot"} title={detail}>{dependencyLabel(value)}</span></div>;
                })}
              </div>
            </section>

            <section className="card" aria-labelledby="library-progress-title">
              <div className="card-head">
                <div><h2 id="library-progress-title">배우별 작업 스택</h2><span className="sub" title="처리 중이거나 남은 작업이 많은 순 · 최대 5명">처리 중이거나 남은 작업이 많은 순 · 최대 5명</span></div>
                <Link href="/media" className="text-link">미디어 보기<Icon name="chevron_right" size={13} /></Link>
              </div>
              <div className="card-body flush">
                {(libraryProgress.data?.items ?? []).length === 0 ? (
                  <div className="empty-state compact">
                    <strong>{libraryProgress.status === "loading" ? "배우별 현황을 불러오는 중입니다" : "표시할 배우별 작업이 없습니다"}</strong>
                  </div>
                ) : (
                  <div className="library-progress-list">
                    {libraryProgress.data?.items.map((item) => (
                      <Link className="library-progress-row" href={`/media?folder=${encodeURIComponent(item.path)}`} key={item.path}>
                        <span className="av s36">
                          {item.image_path ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img src={api.actorImageUrl(item.image_path)} alt="" loading="lazy" />
                          ) : <span aria-hidden>{item.name.slice(0, 1)}</span>}
                        </span>
                        <span className="library-progress-copy">
                          <span><strong title={item.name}>{item.name}</strong><small>{item.done} / {item.total} 완료</small></span>
                          <span className="segbar" aria-label={`${item.name} 작업 진행 현황`}>
                            {item.segments.map((segment) => (
                              <i
                                className={LIBRARY_SEGMENT_CLASS[segment.state]}
                                key={segment.state}
                                style={{ width: `${segment.percent}%` }}
                                title={`${LIBRARY_SEGMENT_LABEL[segment.state]} ${segment.percent}%`}
                              />
                            ))}
                          </span>
                        </span>
                        <Icon name="chevron_right" size={14} />
                      </Link>
                    ))}
                  </div>
                )}
              </div>
            </section>

            <section className="card" aria-labelledby="queue-title">
              <div className="card-head"><h2 id="queue-title">최근 대기 작업</h2><span className="sub" title={`${counts.waiting ?? 0}건`}>{counts.waiting ?? 0}건</span></div>
              <div className="card-body flush">
                {waiting.length === 0 ? <div className="empty-state compact"><strong>대기 작업이 없습니다</strong></div> : (
                  <div className="queue-list">
                    {waiting.slice(0, 8).map((job) => (
                      <Link href={`/jobs/${encodeURIComponent(job.id)}`} key={job.id}><span className="t-name" title={job.source_rel}><b>{fileName(job.source_rel)}</b><span>{PHASE_LABEL[job.phase as JobPhase] ?? job.phase}</span></span><Icon name="chevron_right" size={14} /></Link>
                    ))}
                  </div>
                )}
              </div>
            </section>
          </aside>
        </div>
      </div>
    </>
  );
}
