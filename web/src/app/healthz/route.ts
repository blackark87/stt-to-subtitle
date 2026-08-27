import { NextResponse } from "next/server";

/** nginx 가 제공하던 /healthz 를 유지한다. compose HEALTHCHECK 가 이 경로를 본다. */
export function GET() {
  return NextResponse.json({ service: "stt-web", status: "ok" });
}
