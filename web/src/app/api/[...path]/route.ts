import { NextResponse } from "next/server";

/**
 * /api/v1 은 next.config.ts 의 beforeFiles rewrite 가 backend 로 넘긴다.
 * 그 밖의 /api/* 는 여기로 떨어진다. nginx 가 주던 JSON 404 계약을 유지한다
 * (HTML 404 를 받으면 API 클라이언트가 파싱에 실패한다).
 */
export function GET() {
  return NextResponse.json({ detail: "API version not found" }, { status: 404 });
}

export const POST = GET;
export const PUT = GET;
export const PATCH = GET;
export const DELETE = GET;
