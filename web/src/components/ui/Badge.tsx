import type { ReactNode } from "react";
import { STATE_COLOR_VAR, type JobState } from "@/lib/domain";

/**
 * 상태 배지. 색은 검증된 팔레트(STATE_COLOR_VAR)에서만 온다.
 * 배경은 상태색의 틴트라 라이트/다크 어디서도 대비가 유지된다.
 * tone 이 JobState 로 제한되므로 임의의 색이 섞여 들어올 수 없다.
 */
export function Badge({
  tone,
  children,
  title,
}: {
  tone: JobState;
  children: ReactNode;
  title?: string;
}) {
  const color = STATE_COLOR_VAR[tone];
  return (
    <span
      title={title}
      className="inline-flex items-center gap-1.5 rounded-[var(--radius-pill)] border px-2.5 py-1 text-xs font-bold whitespace-nowrap tnum"
      style={{
        color,
        borderColor: `color-mix(in srgb, ${color} 38%, var(--color-line))`,
        background: `color-mix(in srgb, ${color} var(--state-tint), var(--color-surface))`,
      }}
    >
      <span
        aria-hidden
        className="size-1.5 shrink-0 rounded-full"
        style={{ background: color }}
      />
      {children}
    </span>
  );
}

/** 상태가 아닌 중립 표지(단계 이름 등). 상태 팔레트를 쓰지 않는다. */
export function NeutralBadge({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-[var(--radius-pill)] border border-[var(--color-line)] px-2.5 py-1 text-xs font-semibold text-[var(--color-muted)] whitespace-nowrap">
      {children}
    </span>
  );
}
