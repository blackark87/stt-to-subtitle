export function fileName(path: string): string {
  const parts = String(path ?? "").split("/");
  return parts[parts.length - 1] || "-";
}

export function parentPath(path: string): string {
  const parts = String(path ?? "").split("/");
  parts.pop();
  return parts.join("/");
}

/** 백엔드는 KST ISO 문자열을 준다. 표시에는 시:분:초만 쓴다. */
export function clock(value: string | null | undefined): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleTimeString("ko-KR", { hour12: false });
}

export function relativeFromNow(value: Date | null): string {
  if (!value) return "갱신 전";
  const seconds = Math.max(0, Math.round((Date.now() - value.getTime()) / 1000));
  if (seconds < 5) return "방금";
  if (seconds < 60) return `${seconds}초 전`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}분 전`;
  return `${Math.floor(minutes / 60)}시간 전`;
}

export function percent(done: number, total: number): number | null {
  if (!total || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((done / total) * 100)));
}
