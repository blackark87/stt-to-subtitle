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

  // fetcher 는 렌더마다 새 함수일 수 있으므로 ref 에 담아 effect 재실행을 막는다.
  // 렌더 중에 ref 를 쓰면 안 되므로 커밋 이후에 갱신한다.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  const inFlight = useRef(false);
  const mounted = useRef(true);

  const run = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);
    try {
      const next = await fetcherRef.current();
      if (!mounted.current) return;
      setData(next);
      setUpdatedAt(new Date());
      setError(null);
      setStatus("ok");
    } catch (reason) {
      if (!mounted.current) return;
      // 이전 data 는 그대로 둔다. 다만 status 로 신선하지 않음을 알린다.
      setError(reason instanceof Error ? reason.message : String(reason));
      setStatus("error");
    } finally {
      inFlight.current = false;
      if (mounted.current) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const tick = () => {
      // 탭이 숨겨져 있으면 요청하지 않는다. 보이면 즉시 한 번 따라잡는다.
      if (document.visibilityState === "visible") void run();
    };

    void run();
    const timer = window.setInterval(tick, intervalMs);

    const onVisibility = () => {
      if (document.visibilityState === "visible") void run();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      mounted.current = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [run, intervalMs]);

  return { data, status, error, updatedAt, refreshing, refresh: run };
}
