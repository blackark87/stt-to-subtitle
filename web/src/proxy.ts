import { NextResponse, type NextRequest } from "next/server";

/**
 * nginx 가 붙이던 보안 헤더를 여기로 옮긴다.
 *
 * nginx 는 style-src 'self' 였는데 Next 는 인라인 스타일·스크립트를 넣으므로
 * 그대로는 화면이 뜨지 않는다. 인라인을 통째로 허용하는 대신 요청마다 nonce 를
 * 발급해 그 nonce 만 허용한다.
 *
 * CSP 헤더를 응답뿐 아니라 요청에도 실어야 한다. Next 는 요청 헤더의 CSP 에서
 * nonce 를 읽어 자기 script 태그에 붙인다. 응답에만 실으면 Next 스크립트가
 * nonce 없이 나가고, 우리가 만든 CSP 에 우리 앱이 차단된다.
 */
export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");

  const csp = [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    `style-src 'self' 'nonce-${nonce}'`,
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
  ].join("; ");

  const headers = new Headers(request.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", csp);

  const response = NextResponse.next({ request: { headers } });
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Referrer-Policy", "no-referrer");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("Permissions-Policy", "xr-spatial-tracking=(self)");
  return response;
}

export const config = {
  matcher: [
    // 정적 자산과 API 프록시, 헬스체크는 제외한다.
    { source: "/((?!_next/static|_next/image|favicon.ico|healthz|vendor/|webgpu|api/).*)" },
  ],
};
