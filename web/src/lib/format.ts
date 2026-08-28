export function fileName(path: string): string {
  const parts = String(path ?? "").split("/");
  return parts[parts.length - 1] || "-";
}

export function parentPath(path: string): string {
  const parts = String(path ?? "").split("/");
  parts.pop();
  return parts.join("/");
}

/** 작업 관련 시각을 날짜가 포함된 고정 KST 형식으로 표시한다. */
export function clock(value: string | number | null | undefined): string {
  if (!value) return "-";
  const normalized = typeof value === "number" && value < 1_000_000_000_000
    ? value * 1000
    : value;
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) return String(value);
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(parsed).map((part) => [part.type, part.value]),
  );
  return `${parts.year}${parts.month}${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
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

/** 두 ISO 시각 사이의 소요 시간. 시안의 "21분 04초" / "1:12:33" 형식. */
export function elapsed(from: string | null | undefined, to: string | null | undefined): string {
  if (!from || !to) return "—";
  const a = new Date(from).getTime();
  const b = new Date(to).getTime();
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return "—";
  const total = Math.round((b - a) / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}분 ${String(s).padStart(2, "0")}초`;
}
