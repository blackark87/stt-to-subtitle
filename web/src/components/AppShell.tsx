"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { Icon, type IconName } from "@/components/Icon";

/** design/templates/base.html (38dbb0d) 의 셸을 그대로 옮긴다. */
const NAV: { href: string; icon: IconName; label: string }[] = [
  { href: "/", icon: "dashboard", label: "대시보드" },
  { href: "/media", icon: "folder", label: "미디어" },
  { href: "/jobs", icon: "activity", label: "작업 목록" },
  { href: "/comparisons", icon: "compare", label: "전사 비교" },
  { href: "/settings", icon: "settings", label: "설정" },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Link className="brand" href="/">
          <span className="brand-mark" aria-hidden>
            <Icon name="captions" size={16} />
          </span>
          <span className="brand-copy">
            <strong>STT</strong>
            <small>to Subtitle</small>
          </span>
        </Link>

        <nav className="primary-nav" aria-label="주요 메뉴">
          {NAV.map((item) => {
            // 활성 표시는 aria-current 하나만 근거로 삼는다(N6).
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
              >
                <Icon name={item.icon} size={17} />
                <span>{item.label}</span>
              </Link>
            );
          })}
        </nav>
      </aside>

      <div className="frame">{children}</div>
    </div>
  );
}
