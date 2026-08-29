"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 배경 갱신 훅.
 *
 * 구 UI 의 결함 두 가지를 여기서 막는다.
 *  - N3: 상단의 "갱신" 시각이 페이지 로드 시점에 고정돼 영원히 안 바뀌던 것.
 *        시각을 fetch 가 성공한 순간에서 파생한다.
 *  - N4: 연결이 끊겨도 마지막 화면을 계속 live 처럼 보여주던 것.
 *        실패를 상태로 노출하고, 마지막 성공 시각을 유지한다.
 *
 * 배경 갱신 중에는 data 를 비우지 않는다. 그래야 React 가 같은 key 의 행을
 * 재사용해서 바뀐 셀만 갱신하고, 포커스·텍스트 선택·스크롤·호버가 유지된다.
 * 로딩 표시는 최초 1회뿐이다.
 *
 * transport 는 이 훅 안에만 있다. 나중에 push 로 바꿔도 화면 코드는 그대로다.
 */

export type LiveStatus = "loading" | "ok" | "error";

export interface LiveQuery<T> {
  data: T | null;
  status: LiveStatus;
  error: string | null;
  /** 마지막으로 성공한 시각. 실패해도 유지된다. */
  updatedAt: Date | null;
  /** 지금 배경 요청이 떠 있는지. 스피너 용도가 아니라 미세 표시용. */
  refreshing: boolean;
  /** 즉시 다시 읽는다. mutation 직후 폴링 주기를 기다리지 않기 위해 쓴다. */
  refresh: () => Promise<void>;
}

export function useLiveQuery<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
): LiveQuery<T> {
  const [data, setData] = useState<T | null>(null);
  const [status, setStatus] = useState<LiveStatus>("loading");
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const mounted = useRef(true);
  const requestVersion = useRef(0);

  const run = useCallback(async (silent = false) => {
    const version = ++requestVersion.current;
    if (!silent) setRefreshing(true);
    try {
      const next = await fetcher();
      if (!mounted.current || version !== requestVersion.current) return;
      setData(next);
      setUpdatedAt(new Date());
      setError(null);
      setStatus("ok");
    } catch (reason) {
      if (!mounted.current || version !== requestVersion.current) return;
      // 이전 data 는 그대로 둔다. 다만 status 로 신선하지 않음을 알린다.
      setError(reason instanceof Error ? reason.message : String(reason));
      setStatus("error");
    } finally {
      if (mounted.current && version === requestVersion.current) {
        // 표시 요청이 더 최신의 silent 갱신으로 대체돼도 로딩 상태가
        // 고정되지 않도록, 마지막 요청이 끝날 때는 항상 해제한다.
        setRefreshing(false);
      }
    }
  }, [fetcher]);

  useEffect(() => {
    mounted.current = true;
    // 타이머·탭 복귀로 인한 자동 갱신은 silent 로 돈다: refreshing 을 켜지 않아
    // "새로고침" 버튼이 흐려지거나 Freshness 문구가 "갱신 중"으로 바뀌는 걸
    // 5초마다 반복해서 보여주지 않는다. 사용자가 직접 누른 새로고침만 시각
    // 피드백을 준다.
    const tick = () => {
      if (document.visibilityState === "visible") void run(true);
    };

    queueMicrotask(() => void run());
    const timer = window.setInterval(tick, intervalMs);

    const onVisibility = () => {
      if (document.visibilityState === "visible") void run(true);
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      mounted.current = false;
      requestVersion.current += 1;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [run, intervalMs]);

  return { data, status, error, updatedAt, refreshing, refresh: () => run() };
}
