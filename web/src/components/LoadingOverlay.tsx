interface LoadingOverlayProps {
  active: boolean;
  message?: string;
  detail?: string;
}

export function LoadingOverlay({
  active,
  message = "화면을 불러오는 중입니다",
  detail = "잠시만 기다려 주세요.",
}: LoadingOverlayProps) {
  if (!active) return null;

  return (
    <div
      className="loading-overlay"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      aria-busy="true"
    >
      <div className="loading-overlay-panel">
        <span className="loading-spinner" aria-hidden="true" />
        <span className="loading-overlay-copy">
          <strong>{message}</strong>
          <span>{detail}</span>
        </span>
      </div>
    </div>
  );
}
