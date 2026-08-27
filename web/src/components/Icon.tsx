// design/templates/_icons.html (38dbb0d) 에서 그대로 옮긴 아이콘 세트.
// 임의로 만들거나 다른 라이브러리로 대체하지 않는다.
import type { JSX } from "react";

const PATHS: Record<string, string> = {
  activity: "<polyline points=\"3 12 8 12 10 6 14 18 16 12 21 12\"/>",
  alert_triangle: "<path d=\"M12 3 2 20h20L12 3Z\"/><line x1=\"12\" y1=\"9\" x2=\"12\" y2=\"14\"/><circle cx=\"12\" cy=\"17.3\" r=\"0.6\" fill=\"currentColor\" stroke=\"none\"/>",
  braces: "<path d=\"M8 3a2 2 0 0 0-2 2v3a2 2 0 0 1-2 2 2 2 0 0 1 2 2v3a2 2 0 0 0 2 2\"/><path d=\"M16 3a2 2 0 0 1 2 2v3a2 2 0 0 0 2 2 2 2 0 0 0-2 2v3a2 2 0 0 1-2 2\"/>",
  captions: "<rect x=\"3\" y=\"5\" width=\"18\" height=\"14\" rx=\"2\"/><line x1=\"7\" y1=\"10\" x2=\"10\" y2=\"10\"/><line x1=\"7\" y1=\"14\" x2=\"13\" y2=\"14\"/><line x1=\"14\" y1=\"10\" x2=\"17\" y2=\"10\"/>",
  check: "<polyline points=\"20 6 9 17 4 12\"/>",
  chevron_left: "<polyline points=\"15 18 9 12 15 6\"/>",
  chevron_right: "<polyline points=\"9 18 15 12 9 6\"/>",
  clock: "<circle cx=\"12\" cy=\"12\" r=\"9\"/><polyline points=\"12 7 12 12 16 14\"/>",
  compare: "<rect x=\"3\" y=\"4\" width=\"5\" height=\"16\" rx=\"1.5\"/><rect x=\"10\" y=\"4\" width=\"4\" height=\"16\" rx=\"1.5\"/><rect x=\"16\" y=\"4\" width=\"5\" height=\"16\" rx=\"1.5\"/>",
  dashboard: "<rect x=\"3\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"14\" y=\"3\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"3\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/><rect x=\"14\" y=\"14\" width=\"7\" height=\"7\" rx=\"1.5\"/>",
  download: "<path d=\"M12 3v12\"/><polyline points=\"7 10 12 15 17 10\"/><path d=\"M5 19h14\"/>",
  folder: "<path d=\"M3 7a2 2 0 0 1 2-2h4.4l1.8 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z\"/>",
  log_out: "<path d=\"M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4\"/><polyline points=\"16 17 21 12 16 7\"/><line x1=\"21\" y1=\"12\" x2=\"9\" y2=\"12\"/>",
  pause: "<rect x=\"6\" y=\"4\" width=\"4\" height=\"16\" rx=\"1\" fill=\"currentColor\" stroke=\"none\"/><rect x=\"14\" y=\"4\" width=\"4\" height=\"16\" rx=\"1\" fill=\"currentColor\" stroke=\"none\"/>",
  pencil: "<path d=\"M12 20h9\"/><path d=\"M16.5 3.5 20.5 7.5 8 20H4v-4Z\"/>",
  play: "<polygon points=\"6 3 20 12 6 21 6 3\" fill=\"currentColor\" stroke=\"none\"/>",
  refresh: "<path d=\"M4 12a8 8 0 0 1 13.86-5.66\"/><polyline points=\"18 2 18 7 13 7\"/><path d=\"M20 12a8 8 0 0 1-13.86 5.66\"/><polyline points=\"6 22 6 17 11 17\"/>",
  settings: "<line x1=\"4\" y1=\"6\" x2=\"20\" y2=\"6\"/><circle cx=\"9\" cy=\"6\" r=\"2\"/><line x1=\"4\" y1=\"12\" x2=\"20\" y2=\"12\"/><circle cx=\"15\" cy=\"12\" r=\"2\"/><line x1=\"4\" y1=\"18\" x2=\"20\" y2=\"18\"/><circle cx=\"9\" cy=\"18\" r=\"2\"/>",
  trash: "<path d=\"M3 6h18\"/><path d=\"M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2\"/><path d=\"M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6\"/><line x1=\"10\" y1=\"11\" x2=\"10\" y2=\"17\"/><line x1=\"14\" y1=\"11\" x2=\"14\" y2=\"17\"/>",
};

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 16 }: { name: IconName; size?: number }): JSX.Element {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      dangerouslySetInnerHTML={{ __html: PATHS[name] ?? "" }}
    />
  );
}
