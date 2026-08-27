import type { Metadata, Viewport } from "next";
import { AppShell } from "@/components/AppShell";
import "@fontsource-variable/noto-sans-kr";
import "./globals.css";

export const metadata: Metadata = {
  title: "STT to Subtitle",
  description: "전사·번역·자막 파이프라인 관제",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

/**
 * 요청 시점 렌더로 고정한다. 정적 프리렌더된 HTML 에는 요청마다 바뀌는
 * CSP nonce 를 넣을 수 없어 Next 의 인라인 스크립트가 차단된다.
 */
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
