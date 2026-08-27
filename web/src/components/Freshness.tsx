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
}: {
  status: LiveStatus;
  updatedAt: Date | null;
  error: string | null;
}) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((v) => v + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);

  if (status === "loading") return <span className="fresh">불러오는 중</span>;

  if (status === "error") {
    return (
      <span className="fresh bad" role="status" title={error ?? undefined}>
        <i aria-hidden />
        연결 실패 · 마지막 갱신 {relativeFromNow(updatedAt)}
      </span>
    );
  }

  return (
    <span className="fresh" role="status">
      <i aria-hidden />
      {relativeFromNow(updatedAt)} 갱신
    </span>
  );
}
