"use client";

import { useEffect, useState } from "react";
import { relativeFromNow } from "@/lib/format";
import type { LiveStatus } from "@/lib/useLiveQuery";

/**
 * N3 — "갱신" 시각이 페이지 로드 시점에 고정돼 영원히 안 바뀌던 결함.
 * N4 — 연결이 끊겨도 옛 데이터를 live 처럼 보여주던 결함.
 */
export function Freshness({
  status,
  updatedAt,
  error,
  refreshing = false,
}: {
  status: LiveStatus;
  updatedAt: Date | null;
  error: string | null;
  refreshing?: boolean;
}) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((v) => v + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);

  if (status === "loading") return <span className="fresh">불러오는 중</span>;

  if (status === "error") {
    return (
      <span className="fresh bad" title={error ?? undefined}>
        <i aria-hidden />
        <span role="alert" aria-label={error ? `연결 실패: ${error}` : "연결 실패"}>연결 실패</span>
        <span aria-hidden> · 마지막 갱신 {relativeFromNow(updatedAt)}</span>
      </span>
    );
  }

  return (
    <span className="fresh">
      <i aria-hidden />
      {refreshing ? "갱신 중" : `${relativeFromNow(updatedAt)} 갱신`}
    </span>
  );
}
