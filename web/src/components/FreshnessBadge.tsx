"use client";

import { useEffect, useState } from "react";
import { relativeFromNow } from "@/lib/format";
import type { LiveStatus } from "@/lib/useLiveQuery";

/**
 * 데이터 신선도 표시.
 *
 * 구 UI 는 "09:42 갱신" 을 페이지 로드 시점에 박아 두고 영원히 갱신하지 않았고(N3),
 * 연결이 끊겨도 아무 표시 없이 옛 데이터를 계속 보여줬다(N4).
 * 여기서는 마지막 성공 시각에서 파생하고, 실패 시 명시적으로 알린다.
 */
export function FreshnessBadge({
  status,
  updatedAt,
  error,
}: {
  status: LiveStatus;
  updatedAt: Date | null;
  error: string | null;
}) {
  // 상대 시각은 데이터가 안 와도 스스로 늙어야 한다.
  const [, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), 5000);
    return () => window.clearInterval(timer);
  }, []);

  if (status === "loading") {
    return <span className="text-xs text-[var(--color-muted)]">불러오는 중</span>;
  }

  if (status === "error") {
    return (
      <span
        role="status"
        title={error ?? undefined}
        className="inline-flex items-center gap-1.5 text-xs font-semibold"
        style={{ color: "var(--color-st-failed)" }}
      >
        <span aria-hidden className="size-1.5 rounded-full bg-current" />
        연결 실패 · 마지막 갱신 {relativeFromNow(updatedAt)}
      </span>
    );
  }

  return (
    <span
      role="status"
      className="inline-flex items-center gap-1.5 text-xs text-[var(--color-muted)] tnum"
    >
      <span
        aria-hidden
        className="size-1.5 rounded-full"
        style={{ background: "var(--color-st-done)" }}
      />
      {relativeFromNow(updatedAt)} 갱신
    </span>
  );
}
