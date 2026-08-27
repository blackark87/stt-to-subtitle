import type { ReactNode } from "react";

/**
 * 카드 프레임은 언제나 중립이다.
 * 구 UI 는 "멈춤" 카드 전체를 빨강으로 틀지어서, 사용자가 직접 누른
 * 일시 정지·정지까지 오류로 보이게 했다(H4). 상태색은 행 단위에만 쓴다.
 */
export function Card({
  title,
  meta,
  actions,
  children,
  bodyClassName = "p-4",
}: {
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  bodyClassName?: string;
}) {
  return (
    <section className="min-w-0 rounded-[var(--radius-card)] border border-[var(--color-line)] bg-[var(--color-surface)]">
      <header className="flex min-h-12 flex-wrap items-center justify-between gap-3 border-b border-[var(--color-line)] px-4 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="m-0 text-sm font-bold">{title}</h2>
          {meta ? (
            <span className="text-xs font-semibold text-[var(--color-muted)] tnum">{meta}</span>
          ) : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </header>
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}
