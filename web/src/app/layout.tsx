import type { Metadata, Viewport } from "next";
import { AppShell } from "@/components/AppShell";
import "./globals.css";

export const metadata: Metadata = {
  title: "STT to Subtitle",
  description: "전사·번역·자막 파이프라인 관제",
};

/**
 * 요청 시점 렌더로 고정한다.
 *
 * 정적 프리렌더된 HTML 에는 요청마다 바뀌는 CSP nonce 를 넣을 수 없다.
 * 그러면 Next 가 내보내는 인라인 스크립트(RSC 페이로드)가 우리 CSP 에 걸려
 * 앱이 통째로 죽는다. 이 앱은 모든 페이지가 클라이언트 렌더라 프리렌더로
 * 얻는 것이 거의 없으므로, nonce 를 살리는 쪽이 이득이다.
 */
export const dynamic = "force-dynamic";

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      {/* 조상에 overflow:hidden 을 두지 않는다. 두면 헤더의 sticky 가 죽는다(L3). */}
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
