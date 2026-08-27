import { NextResponse, type NextRequest } from "next/server";

/**
 * backend 로 넘기는 리버스 프록시.
 *
 * next.config.ts 의 rewrites 를 쓰지 않는 이유: 그 값은 빌드 시점에 구워져서
 * 컨테이너 런타임의 BACKEND_ORIGIN 이 무시된다. 여기서는 요청마다 읽는다.
 *
 * 응답 본문을 스트림 그대로 넘긴다. 영상 byte-range(/media/file) 같이 큰 응답을
 * 메모리에 모으지 않기 위해서다.
 */

export const dynamic = "force-dynamic";

function backendOrigin(): string {
  return process.env.BACKEND_ORIGIN ?? "http://backend:8080";
}

/** 홉 단위 헤더는 전달하지 않는다. host 는 대상 기준으로 다시 잡힌다. */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "host",
  "content-length",
]);

function forwardHeaders(source: Headers): Headers {
  const headers = new Headers();
  source.forEach((value, key) => {
    if (!HOP_BY_HOP.has(key.toLowerCase())) headers.set(key, value);
  });
  return headers;
}

async function proxy(request: NextRequest): Promise<Response> {
  const url = new URL(request.url);
  const target = `${backendOrigin()}${url.pathname}${url.search}`;
  const hasBody = request.method !== "GET" && request.method !== "HEAD";

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers: forwardHeaders(request.headers),
      body: hasBody ? request.body : undefined,
      // 스트리밍 요청 본문에는 duplex 가 필요하다.
      ...(hasBody ? { duplex: "half" } : {}),
      redirect: "manual",
      cache: "no-store",
    } as RequestInit);

    const headers = new Headers();
    upstream.headers.forEach((value, key) => {
      if (!HOP_BY_HOP.has(key.toLowerCase())) headers.set(key, value);
    });

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers,
    });
  } catch (reason) {
    // 백엔드가 죽었을 때 Next 의 HTML 500 대신 JSON 을 준다.
    // 화면의 api 계층이 detail 을 읽어 "연결 실패" 로 표시할 수 있다.
    const detail =
      reason instanceof Error ? `Backend 연결 실패: ${reason.message}` : "Backend 연결 실패";
    return NextResponse.json({ detail }, { status: 502 });
  }
}

export const GET = proxy;
export const HEAD = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
