import { Icon } from "@/components/Icon";

interface PaginationProps {
  currentPage: number;
  pageSize: number;
  totalItems: number;
  onPageChange: (page: number) => void;
  ariaLabel?: string;
}

type PageItem = number | "start-gap" | "end-gap";

function pageItems(currentPage: number, pageCount: number): PageItem[] {
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, index) => index + 1);
  }
  if (currentPage <= 4) return [1, 2, 3, 4, 5, "end-gap", pageCount];
  if (currentPage >= pageCount - 3) {
    return [
      1,
      "start-gap",
      pageCount - 4,
      pageCount - 3,
      pageCount - 2,
      pageCount - 1,
      pageCount,
    ];
  }
  return [
    1,
    "start-gap",
    currentPage - 1,
    currentPage,
    currentPage + 1,
    "end-gap",
    pageCount,
  ];
}

export function Pagination({
  currentPage,
  pageSize,
  totalItems,
  onPageChange,
  ariaLabel = "페이지 이동",
}: PaginationProps) {
  const pageCount = Math.max(1, Math.ceil(totalItems / pageSize));
  const page = Math.min(Math.max(1, currentPage), pageCount);
  if (pageCount <= 1) return null;
  const firstItem = (page - 1) * pageSize + 1;
  const lastItem = Math.min(page * pageSize, totalItems);

  return (
    <nav className="pagination" aria-label={ariaLabel}>
      <span className="pagination-summary">
        총 {totalItems}건 · {firstItem}–{lastItem}건
      </span>
      <div className="pagination-pages">
        <button
          type="button"
          className="btn sec sm pagination-edge"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          <Icon name="chevron_left" size={13} />이전
        </button>
        {pageItems(page, pageCount).map((item) => (
          typeof item === "number" ? (
            <button
              key={item}
              type="button"
              className="btn sec sm pagination-page"
              aria-current={item === page ? "page" : undefined}
              aria-label={`${item}페이지`}
              onClick={() => onPageChange(item)}
            >
              {item}
            </button>
          ) : (
            <span key={item} className="pagination-gap" aria-hidden>…</span>
          )
        ))}
        <button
          type="button"
          className="btn sec sm pagination-edge"
          disabled={page >= pageCount}
          onClick={() => onPageChange(page + 1)}
        >
          다음<Icon name="chevron_right" size={13} />
        </button>
      </div>
    </nav>
  );
}
