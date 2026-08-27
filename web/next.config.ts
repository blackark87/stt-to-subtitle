import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // 상위 홈 디렉터리의 unrelated package-lock.json을 workspace 기준으로 오인하지 않는다.
  turbopack: { root: process.cwd() },
  // 이미지 최적화는 런타임 캐시 쓰기를 유발한다. read_only 컨테이너 유지를 위해 끈다.
  images: { unoptimized: true },
  // /api/v1 프록시는 rewrites 로 하지 않는다. next.config 의 값은 빌드 시점에
  // 구워지므로 컨테이너 런타임의 BACKEND_ORIGIN 이 무시된다.
  // src/app/api/v1/[...path]/route.ts 가 요청 시점에 환경변수를 읽어 넘긴다.
};

export default nextConfig;
