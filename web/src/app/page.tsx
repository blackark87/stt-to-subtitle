"use client";

import { useCallback } from "react";
import { Badge, NeutralBadge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Column, DataTable } from "@/components/ui/DataTable";
import { Progress } from "@/components/ui/Progress";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { api, type PipelineJob } from "@/lib/api";
import {
  ATTENTION_STATES,
  STATE_COLOR_VAR,
  STATE_LABEL,
  STATE_ORDER,
  asJobState,
  phaseLabel,
  reasonLabel,
  stateLabel,
} from "@/lib/domain";
import { clock, fileName, parentPath, percent } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

const DASHBOARD_INTERVAL_MS = 5000;

function jobProgress(job: PipelineJob): number | null {
  if (job.phase === "transcription") {
    const total = Math.max(job.chunks_created, job.chunks_total_estimate);
    return percent(job.chunks_completed, total);
  }
  if (job.phase === "translation") {
    return percent(job.translation_chunks_completed, job.translation_chunks_total);
  }
  return null;
}

const columns: readonly Column<PipelineJob>[] = [
  {
    key: "source",
    header: "파일",
    width: "minmax(0,1fr)",
    cell: (job) => (
      <div className="grid min-w-0 gap-0.5">
        <span className="truncate text-sm font-bold" title={job.source_rel}>
          {fileName(job.source_rel)}
        </span>
        <span className="truncate text-xs text-[var(--color-muted)]">
          {parentPath(job.source_rel) || "—"}
        </span>
      </div>
    ),
  },
  {
    key: "phase",
    header: "단계",
    width: "120px",
    cell: (job) => <NeutralBadge>{phaseLabel(job.phase)}</NeutralBadge>,
  },
  {
    key: "state",
    header: "상태",
    width: "128px",
    cell: (job) => {
      const state = asJobState(job.state);
      const reason = reasonLabel(job.reason_code);
      return state ? (
        <Badge tone={state} title={reason ?? undefined}>
          {STATE_LABEL[state]}
        </Badge>
      ) : (
        <NeutralBadge>{job.state}</NeutralBadge>
      );
    },
  },
  {
    key: "progress",
    header: "진행",
    width: "140px",
    hideOnNarrow: true,
    cell: (job) => {
      const value = jobProgress(job);
      const state = asJobState(job.state);
      if (value == null) return <span className="text-xs text-[var(--color-muted)]">—</span>;
      return (
        <div className="flex items-center gap-2">
          <Progress
            value={value}
            label={`${fileName(job.source_rel)} 진행률`}
            color={state ? STATE_COLOR_VAR[state] : "var(--color-accent)"}
          />
          <span className="w-9 shrink-0 text-right text-xs font-semibold tnum">{value}%</span>
        </div>
      );
    },
  },
  {
    key: "updated",
    header: "갱신",
    width: "92px",
    align: "end",
    cell: (job) => <span className="text-xs text-[var(--color-muted)] tnum">{clock(job.updated_at)}</span>,
  },
];

export default function DashboardPage() {
  const fetcher = useCallback(() => api.dashboard(), []);
  const { data, status, error, updatedAt } = useLiveQuery(fetcher, DASHBOARD_INTERVAL_MS);

  const counts = data?.state_counts ?? {};
  const jobs = data?.recent_jobs ?? [];
  const gpu = data?.gpu ?? null;
  const attention = jobs.filter((job) => {
    const state = asJobState(job.state);
    return state != null && ATTENTION_STATES.includes(state);
  });

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="m-0 text-lg font-bold">대시보드</h1>
        <FreshnessBadge status={status} updatedAt={updatedAt} error={error} />
      </div>

      {/* 상태 요약 — 7종 전부. 색은 검증된 팔레트라 서로 구분된다(H2). */}
      <section aria-label="상태 요약" className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        {STATE_ORDER.map((state) => (
          <div
            key={state}
            className="grid gap-1 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)] p-3"
            style={{ boxShadow: `inset 3px 0 0 ${STATE_COLOR_VAR[state]}` }}
          >
            <span className="text-xs font-semibold text-[var(--color-muted)]">
              {STATE_LABEL[state]}
            </span>
            <strong className="text-2xl leading-none tnum" style={{ color: STATE_COLOR_VAR[state] }}>
              {counts[state] ?? 0}
            </strong>
          </div>
        ))}
      </section>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="grid gap-4">
          {attention.length > 0 ? (
            /* 카드 프레임은 중립. 상태색은 행에만 쓴다(H4). */
            <Card title="확인 필요" meta={`${attention.length}건`} bodyClassName="p-0">
              <DataTable
                caption="확인이 필요한 작업"
                columns={columns}
                rows={attention}
                rowKey={(job) => job.id}
                rowTone={(job) => asJobState(job.state)}
              />
            </Card>
          ) : null}

          <Card title="최근 작업" meta={`${jobs.length}건`} bodyClassName="p-0">
            <DataTable
              caption="최근 작업"
              columns={columns}
              rows={jobs}
              rowKey={(job) => job.id}
              rowTone={(job) => asJobState(job.state)}
              emptyText={status === "loading" ? "불러오는 중" : "등록된 작업 없음"}
            />
          </Card>
        </div>

        <div className="grid content-start gap-4">
          <Card title="의존성">
            <dl className="grid gap-2 text-sm">
              {(
                [
                  ["전사", data?.dependencies.transcription],
                  ["번역", data?.dependencies.translation],
                ] as const
              ).map(([label, dependency]) => {
                const raw = typeof dependency?.state === "string" ? dependency.state : "";
                const tone = asJobState(raw);
                return (
                  <div key={label} className="flex items-center justify-between gap-3">
                    <dt className="text-[var(--color-muted)]">{label}</dt>
                    <dd className="m-0">
                      {tone ? (
                        <Badge tone={tone}>{stateLabel(raw)}</Badge>
                      ) : (
                        <NeutralBadge>{raw || "확인 중"}</NeutralBadge>
                      )}
                    </dd>
                  </div>
                );
              })}
            </dl>
          </Card>

          <Card title="GPU" meta={gpu?.available ? undefined : "수집 안 됨"}>
            {gpu?.available && gpu.devices.length ? (
              <div className="grid gap-3">
                {gpu.devices.map((device, index) => (
                  <div key={device.index ?? index} className="grid gap-2">
                    <div className="flex items-baseline justify-between gap-2">
                      <strong className="text-xs">GPU {device.index ?? index}</strong>
                      <span className="truncate text-xs text-[var(--color-muted)]">
                        {device.display_name ?? ""}
                      </span>
                    </div>
                    <div className="grid gap-1">
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-[var(--color-muted)]">사용률</span>
                        <span className="tnum">
                          {device.utilization_percent?.toFixed(0) ?? "—"}%
                        </span>
                      </div>
                      <Progress
                        value={device.utilization_percent}
                        color="var(--color-accent)"
                        label="GPU 사용률"
                      />
                    </div>
                    <div className="grid gap-1">
                      <div className="flex items-center justify-between text-xs">
                        <span className="text-[var(--color-muted)]">메모리</span>
                        <span className="tnum">
                          {device.memory_used_gib?.toFixed(1) ?? "—"} /{" "}
                          {device.memory_total_gib?.toFixed(1) ?? "—"} GiB
                        </span>
                      </div>
                      <Progress
                        value={device.memory_percent}
                        color="var(--color-st-blocked)"
                        label="GPU 메모리"
                      />
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="m-0 text-sm text-[var(--color-muted)]">
                GPU 메트릭이 설정되지 않았습니다.
              </p>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}
