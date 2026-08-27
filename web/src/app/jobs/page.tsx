"use client";

import { useCallback, useMemo, useState } from "react";
import { Badge, NeutralBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Column, DataTable } from "@/components/ui/DataTable";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { api, type PipelineJob } from "@/lib/api";
import {
  STATE_LABEL,
  STATE_ORDER,
  asJobState,
  phaseLabel,
  reasonLabel,
} from "@/lib/domain";
import { clock, fileName, parentPath } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

const JOBS_INTERVAL_MS = 5000;

export default function JobsPage() {
  const [filter, setFilter] = useState<readonly string[]>([]);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetcher = useCallback(
    () => api.jobs({ limit: 200, state: filter.length ? filter : undefined }),
    [filter],
  );
  const { data, status, error, updatedAt, refresh } = useLiveQuery(fetcher, JOBS_INTERVAL_MS);

  const jobs = useMemo(() => data?.items ?? [], [data]);

  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const runAction = async (action: (ids: string[]) => Promise<unknown>) => {
    const ids = [...selected];
    if (!ids.length) return;
    setBusy(true);
    setActionError(null);
    try {
      await action(ids);
      setSelected(new Set());
      // mutation 직후 폴링 주기를 기다리지 않고 바로 반영한다.
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const columns: readonly Column<PipelineJob>[] = [
    {
      key: "select",
      header: "선택",
      width: "44px",
      cell: (job) => (
        <input
          type="checkbox"
          checked={selected.has(job.id)}
          onChange={() => toggle(job.id)}
          aria-label={`${fileName(job.source_rel)} 선택`}
          className="size-4 accent-[var(--color-accent)]"
        />
      ),
    },
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
      width: "110px",
      cell: (job) => <NeutralBadge>{phaseLabel(job.phase)}</NeutralBadge>,
    },
    {
      key: "state",
      header: "상태",
      width: "124px",
      cell: (job) => {
        const state = asJobState(job.state);
        return state ? (
          <Badge tone={state}>{STATE_LABEL[state]}</Badge>
        ) : (
          <NeutralBadge>{job.state}</NeutralBadge>
        );
      },
    },
    {
      key: "reason",
      header: "사유",
      width: "minmax(0,200px)",
      hideOnNarrow: true,
      cell: (job) => {
        const reason = reasonLabel(job.reason_code) ?? job.error;
        return (
          <span className="block truncate text-xs text-[var(--color-muted)]" title={reason ?? ""}>
            {reason ?? "—"}
          </span>
        );
      },
    },
    {
      key: "updated",
      header: "갱신",
      width: "92px",
      align: "end",
      cell: (job) => (
        <span className="text-xs text-[var(--color-muted)] tnum">{clock(job.updated_at)}</span>
      ),
    },
  ];

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="m-0 text-lg font-bold">작업 목록</h1>
        <FreshnessBadge status={status} updatedAt={updatedAt} error={error} />
      </div>

      <Card
        title="필터"
        actions={
          filter.length ? (
            <Button onClick={() => setFilter([])}>전체 해제</Button>
          ) : undefined
        }
      >
        <div className="flex flex-wrap gap-2">
          {STATE_ORDER.map((state) => {
            const active = filter.includes(state);
            return (
              <button
                key={state}
                type="button"
                aria-pressed={active}
                onClick={() =>
                  setFilter((current) =>
                    current.includes(state)
                      ? current.filter((value) => value !== state)
                      : [...current, state],
                  )
                }
                className="rounded-[var(--radius-pill)] border border-[var(--color-line)] px-3 py-1.5 text-xs font-semibold aria-pressed:border-[var(--color-accent)] aria-pressed:bg-[color-mix(in_srgb,var(--color-accent)_14%,var(--color-surface))] aria-pressed:text-[var(--color-accent)]"
              >
                {STATE_LABEL[state]}
              </button>
            );
          })}
        </div>
      </Card>

      <Card
        title="작업"
        meta={`${jobs.length} / ${data?.total ?? 0}건${selected.size ? ` · ${selected.size}개 선택` : ""}`}
        actions={
          <>
            <Button disabled={!selected.size || busy} onClick={() => runAction(api.retryJobs)}>
              재시도
            </Button>
            <Button
              disabled={!selected.size || busy}
              onClick={() => runAction(api.pauseTranslations)}
            >
              번역 일시정지
            </Button>
            <Button
              variant="danger"
              disabled={!selected.size || busy}
              onClick={() => runAction(api.stopJobs)}
            >
              정지
            </Button>
          </>
        }
        bodyClassName="p-0"
      >
        {actionError ? (
          <p
            role="alert"
            className="m-0 border-b border-[var(--color-line)] px-4 py-2 text-sm"
            style={{ color: "var(--color-st-failed)" }}
          >
            {actionError}
          </p>
        ) : null}
        <DataTable
          caption="작업 목록"
          columns={columns}
          rows={jobs}
          rowKey={(job) => job.id}
          rowTone={(job) => asJobState(job.state)}
          selectedKeys={selected}
          emptyText={status === "loading" ? "불러오는 중" : "조건에 맞는 작업 없음"}
        />
      </Card>
    </>
  );
}
