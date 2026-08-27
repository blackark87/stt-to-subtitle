"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

const NAV = [
  { href: "/", label: "대시보드" },
  { href: "/jobs", label: "작업 목록" },
  { href: "/runtimes", label: "Runtime" },
] as const;

/**
 * 활성 표시는 aria-current 를 단일 근거로 삼는다.
 * 구 UI 는 aria-current 를 8곳에 붙여 두고 시각 규칙은 breadcrumbs 하나뿐이라
 * 대부분 보조기술에만 노출되고 눈에는 안 보였다(N6).
 * 여기서는 [aria-current] 셀렉터가 곧 스타일이라 둘이 어긋날 수 없다.
 */
export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-[var(--color-line)] bg-[color-mix(in_srgb,var(--color-surface)_92%,transparent)] backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-5 gap-y-2 px-5 py-2.5">
          <Link
            href="/"
            className="flex items-center gap-2 text-sm font-bold tracking-wide no-underline"
            style={{ color: "var(--color-text)" }}
          >
            <span
              aria-hidden
              className="grid size-7 place-items-center rounded-[var(--radius-control)] text-xs font-black"
              style={{ background: "var(--color-accent)", color: "var(--color-accent-ink)" }}
            >
              ST
            </span>
            STT to Subtitle
          </Link>

          <nav aria-label="주요 메뉴" className="flex items-center gap-1">
            {NAV.map((item) => {
              const active =
                item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className="rounded-[var(--radius-control)] px-2.5 py-1.5 text-sm font-semibold text-[var(--color-muted)] no-underline hover:bg-[var(--color-sunken)] aria-[current=page]:bg-[color-mix(in_srgb,var(--color-accent)_14%,var(--color-surface))] aria-[current=page]:text-[var(--color-accent)]"
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
        </div>
      </header>

      <main className="mx-auto grid max-w-[1400px] gap-4 px-5 py-5">{children}</main>
    </div>
  );
}
