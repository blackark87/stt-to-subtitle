/**
 * 진행률 막대.
 *
 * 구 UI 는 채움색을 currentColor 로 두어서, 색을 지정하지 않은 부모 안에서는
 * 본문 글자색으로 칠해졌다(H1). 다크에서 거의 흰색 막대가 되어 화면에서
 * 가장 강한 강조가 의미 없는 요소에 붙었다.
 * 여기서는 채움색을 반드시 인자로 받는다. 상속하지 않는다.
 */
export function Progress({
  value,
  color,
  label,
  height = 6,
}: {
  value: number | null;
  color: string;
  label: string;
  height?: number;
}) {
  const clamped = value == null ? 0 : Math.min(100, Math.max(0, value));
  return (
    <span
      role="progressbar"
      aria-valuenow={value ?? undefined}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={label}
      className="block w-full overflow-hidden rounded-[var(--radius-pill)]"
      style={{
        height,
        background: `color-mix(in srgb, var(--color-line) 70%, transparent)`,
      }}
    >
      <span
        className="block h-full rounded-[var(--radius-pill)] transition-[width] duration-500 ease-out"
        style={{ width: `${clamped}%`, background: color }}
      />
    </span>
  );
}
