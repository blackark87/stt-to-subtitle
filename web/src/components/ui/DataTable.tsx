import type { ReactNode } from "react";
import { STATE_COLOR_VAR, type JobState } from "@/lib/domain";

/**
 * 컬럼 정의를 한 곳에만 둔다.
 *
 * 구 UI 는 머리글과 데이터 행이 각자 인라인 grid-template-columns 를 갖고 있어서,
 * 반응형 규칙이 데이터 행에만 적용되면 머리글이 어긋났다(L1). 또 좁은 폭에서
 * 동작 열만 재배치를 빠뜨려 버튼이 78px 열에 갇혔다(L2).
 * 여기서는 머리글·본문이 같은 배열을 쓰므로 두 결함이 구조적으로 불가능하다.
 * 좁은 화면에서는 표를 버리고 카드형으로 전환한다.
 */

export interface Column<T> {
  key: string;
  header: ReactNode;
  /** CSS grid track. 예: "minmax(0,1fr)", "120px" */
  width: string;
  align?: "start" | "end";
  cell: (row: T) => ReactNode;
  /** 카드형(좁은 화면)에서 숨길 열 */
  hideOnNarrow?: boolean;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  rowTone,
  selectedKeys,
  emptyText = "표시할 항목이 없습니다",
  caption,
}: {
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  /** 행 왼쪽 강조선 색. 상태색은 행 단위에만 쓴다. */
  rowTone?: (row: T) => JobState | null;
  selectedKeys?: ReadonlySet<string>;
  emptyText?: string;
  caption: string;
}) {
  const template = columns.map((column) => column.width).join(" ");

  if (!rows.length) {
    return (
      <div className="grid place-items-center px-4 py-10 text-sm text-[var(--color-muted)]">
        {emptyText}
      </div>
    );
  }

  return (
    <div role="table" aria-label={caption} className="grid">
      <div
        role="row"
        className="hidden min-h-8 items-center gap-3 border-b border-[var(--color-line)] bg-[var(--color-band)] px-3 py-1.5 text-[0.68rem] font-bold tracking-wide text-[var(--color-muted)] md:grid"
        style={{ gridTemplateColumns: template }}
      >
        {columns.map((column) => (
          <span
            key={column.key}
            role="columnheader"
            className={column.align === "end" ? "text-right" : undefined}
          >
            {column.header}
          </span>
        ))}
      </div>

      {rows.map((row) => {
        const key = rowKey(row);
        const tone = rowTone?.(row) ?? null;
        const selected = selectedKeys?.has(key) ?? false;
        return (
          <div
            key={key}
            role="row"
            aria-selected={selectedKeys ? selected : undefined}
            className={[
              "grid min-h-11 items-center gap-3 border-b border-[var(--color-line)] px-3 py-2 last:border-b-0",
              "grid-cols-1 md:grid-cols-[var(--cols)]",
              selected ? "bg-[color-mix(in_srgb,var(--color-accent)_10%,var(--color-surface))]" : "",
            ].join(" ")}
            style={
              {
                "--cols": template,
                boxShadow: tone ? `inset 3px 0 0 ${STATE_COLOR_VAR[tone]}` : undefined,
              } as React.CSSProperties
            }
          >
            {columns.map((column) => (
              <div
                key={column.key}
                role="cell"
                className={[
                  "min-w-0",
                  column.align === "end" ? "md:text-right" : "",
                  column.hideOnNarrow ? "hidden md:block" : "",
                ].join(" ")}
              >
                {/* 좁은 화면에서는 머리글이 숨겨지므로 셀마다 라벨을 붙인다. */}
                <span className="mb-0.5 block text-[0.65rem] font-bold text-[var(--color-muted)] md:hidden">
                  {column.header}
                </span>
                {column.cell(row)}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
