"use client";

import { useCallback, useState } from "react";
import { Badge, NeutralBadge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Column, DataTable } from "@/components/ui/DataTable";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { api, type RuntimeEndpoint } from "@/lib/api";
import {
  RUNTIME_STATUS_LABEL,
  RUNTIME_STATUS_TONE,
  asRuntimeStatus,
} from "@/lib/domain";
import { useLiveQuery } from "@/lib/useLiveQuery";

const RUNTIME_INTERVAL_MS = 10000;

interface FormState {
  id: string | null;
  name: string;
  base_url: string;
  token: string;
  capacity: number;
}

const EMPTY: FormState = { id: null, name: "", base_url: "", token: "", capacity: 1 };

export default function RuntimesPage() {
  const fetcher = useCallback(() => api.runtimes(), []);
  const { data, status, error, updatedAt, refresh } = useLiveQuery(fetcher, RUNTIME_INTERVAL_MS);
  const [form, setForm] = useState<FormState>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const items = data?.items ?? [];

  const guard = async (task: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await task();
      await refresh();
      return true;
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const existing = items.find((item) => item.id === form.id);
    const ok = await guard(() =>
      existing
        ? api.updateRuntime(existing.id, {
            name: form.name,
            base_url: form.base_url,
            token: form.token || null,
            enabled: existing.enabled,
            capacity: form.capacity,
          })
        : api.createRuntime({
            name: form.name,
            base_url: form.base_url,
            token: form.token,
            enabled: true,
            capacity: form.capacity,
          }),
    );
    if (ok) setForm(EMPTY);
  };

  const columns: readonly Column<RuntimeEndpoint>[] = [
    {
      key: "name",
      header: "Runtime",
      width: "minmax(0,1fr)",
      cell: (runtime) => (
        <div className="grid min-w-0 gap-0.5">
          <span className="truncate text-sm font-bold">
            {runtime.name}
            {runtime.builtin ? (
              <span className="ml-1.5 text-xs font-semibold text-[var(--color-muted)]">기본</span>
            ) : null}
          </span>
          <span className="truncate text-xs text-[var(--color-muted)]" title={runtime.base_url}>
            {runtime.base_url}
          </span>
        </div>
      ),
    },
    {
      key: "status",
      header: "상태",
      width: "128px",
      cell: (runtime) => {
        const parsed = asRuntimeStatus(runtime.status);
        return parsed ? (
          <Badge tone={RUNTIME_STATUS_TONE[parsed]} title={runtime.message ?? undefined}>
            {RUNTIME_STATUS_LABEL[parsed]}
          </Badge>
        ) : (
          <NeutralBadge>{runtime.status}</NeutralBadge>
        );
      },
    },
    {
      key: "slots",
      header: "작업",
      width: "96px",
      align: "end",
      cell: (runtime) => (
        <span className="text-xs tnum">
          {runtime.running_jobs} / {runtime.capacity}
        </span>
      ),
    },
    {
      key: "actions",
      header: "관리",
      width: "minmax(0,240px)",
      align: "end",
      cell: (runtime) => (
        <div className="flex flex-wrap justify-start gap-1.5 md:justify-end">
          <Button disabled={busy} onClick={() => void guard(() => api.probeRuntime(runtime.id))}>
            확인
          </Button>
          {runtime.builtin ? null : (
            <>
              <Button
                disabled={busy}
                onClick={() =>
                  void guard(() =>
                    api.updateRuntime(runtime.id, {
                      name: runtime.name,
                      base_url: runtime.base_url,
                      enabled: !runtime.enabled,
                      capacity: runtime.capacity,
                    }),
                  )
                }
              >
                {runtime.enabled ? "사용 중지" : "사용"}
              </Button>
              <Button
                disabled={busy}
                onClick={() =>
                  setForm({
                    id: runtime.id,
                    name: runtime.name,
                    base_url: runtime.base_url,
                    token: "",
                    capacity: runtime.capacity,
                  })
                }
              >
                수정
              </Button>
              <Button
                variant="danger"
                disabled={busy}
                onClick={() => {
                  if (!window.confirm(`${runtime.name} Runtime을 삭제할까요?`)) return;
                  void guard(() => api.deleteRuntime(runtime.id));
                }}
              >
                삭제
              </Button>
            </>
          )}
        </div>
      ),
    },
  ];

  const field =
    "w-full rounded-[var(--radius-control)] border border-[var(--color-line)] bg-[var(--color-surface)] px-2.5 py-1.5 text-sm text-[var(--color-text)]";

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="m-0 text-lg font-bold">전사 Runtime</h1>
        <FreshnessBadge status={status} updatedAt={updatedAt} error={error} />
      </div>

      <Card title={form.id ? "Runtime 수정" : "Runtime 추가"}>
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <label className="grid gap-1 text-xs font-semibold">
            이름
            <input
              required
              maxLength={80}
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
              placeholder="GPU Runtime 02"
              className={field}
            />
          </label>
          <label className="grid gap-1 text-xs font-semibold">
            API 주소
            <input
              required
              type="url"
              value={form.base_url}
              onChange={(event) => setForm({ ...form, base_url: event.target.value })}
              placeholder="http://runtime-host:8100"
              className={field}
            />
          </label>
          <label className="grid gap-1 text-xs font-semibold">
            API 토큰
            <input
              type="password"
              autoComplete="new-password"
              value={form.token}
              onChange={(event) => setForm({ ...form, token: event.target.value })}
              placeholder={form.id ? "변경하지 않으려면 비워 두세요" : ""}
              className={field}
            />
          </label>
          <label className="grid gap-1 text-xs font-semibold">
            할당 슬롯
            <input
              type="number"
              min={1}
              max={8}
              value={form.capacity}
              onChange={(event) => setForm({ ...form, capacity: Number(event.target.value) })}
              className={field}
            />
          </label>
          <div className="flex items-center gap-2 sm:col-span-2 lg:col-span-4">
            <button
              type="submit"
              disabled={busy}
              className="inline-flex items-center rounded-[var(--radius-control)] border border-[var(--color-accent)] bg-[var(--color-accent)] px-3 py-1.5 text-xs font-semibold text-[var(--color-accent-ink)] disabled:opacity-50"
            >
              {form.id ? "변경 저장" : "Runtime 추가"}
            </button>
            {form.id ? <Button onClick={() => setForm(EMPTY)}>취소</Button> : null}
          </div>
        </form>
        {actionError ? (
          <p role="alert" className="mt-3 mb-0 text-sm" style={{ color: "var(--color-st-failed)" }}>
            {actionError}
          </p>
        ) : null}
      </Card>

      <Card title="등록된 Runtime" meta={`${items.length}개`} bodyClassName="p-0">
        <DataTable
          caption="전사 Runtime"
          columns={columns}
          rows={items}
          rowKey={(runtime) => runtime.id}
          emptyText={status === "loading" ? "불러오는 중" : "등록된 Runtime 없음"}
        />
      </Card>
    </>
  );
}
