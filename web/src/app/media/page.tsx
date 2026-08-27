"use client";

import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api, type MediaFile } from "@/lib/api";
import { useLiveQuery } from "@/lib/useLiveQuery";

/* design/mockup body_Media.html 구조: 필터 레일 + .board 카드 그리드. */

const MEDIA_INTERVAL_MS = 15000;

type SubtitleFilter = "all" | "none" | "done";

function duration(seconds: number | null): string {
  if (!seconds || seconds <= 0) return "—";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}` : `${m}분`;
}

export default function MediaPage() {
  const [folder, setFolder] = useState("");
  const [query, setQuery] = useState("");
  const [subtitle, setSubtitle] = useState<SubtitleFilter>("all");
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const fetcher = useCallback(() => api.media({ folder, q: query }), [folder, query]);
  const { data, status, error, updatedAt, refresh } = useLiveQuery(fetcher, MEDIA_INTERVAL_MS);
  const prompts = useLiveQuery(useCallback(() => api.promptCategories(), []), 60000);
  const [promptId, setPromptId] = useState<string>("");

  const files = useMemo(() => {
    const list = data?.files ?? [];
    if (subtitle === "none") return list.filter((file) => !file.has_subtitle);
    if (subtitle === "done") return list.filter((file) => file.has_subtitle);
    return list;
  }, [data, subtitle]);

  const toggle = (path: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const start = async () => {
    const rels = [...selected];
    if (!rels.length) return;
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await api.createJobs({
        source_rels: rels,
        operation: "full",
        prompt_category_id: promptId || null,
      });
      setNotice(`${rels.length}건을 시작했습니다.`);
      setSelected(new Set());
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const enter = (path: string) => {
    setFolder(path);
    setSelected(new Set());
  };

  return (
    <>
      <header className="topbar">
        <h1>미디어</h1>
        <span style={{ marginLeft: "auto" }}>
          <Freshness status={status} updatedAt={updatedAt} error={error} />
        </span>
        <button type="button" className="btn sec sm" onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />
          새로고침
        </button>
      </header>

      <div className="content">
        <section className="card">
          <div className="card-head">
            <h2>탐색</h2>
            <span className="btns">
              <span className="seg">
                {(
                  [
                    ["all", "전체"],
                    ["none", "없음"],
                    ["done", "완료"],
                  ] as [SubtitleFilter, string][]
                ).map(([key, label]) => (
                  <span
                    key={key}
                    role="button"
                    tabIndex={0}
                    aria-pressed={subtitle === key}
                    className={subtitle === key ? "on" : undefined}
                    onClick={() => setSubtitle(key)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") setSubtitle(key);
                    }}
                  >
                    {label}
                  </span>
                ))}
              </span>
            </span>
          </div>
          <div className="card-body">
            <div className="fg">
              <label className="f">
                <span className="lb">제목 · 파일명 · 배우</span>
                <input
                  className="ctl"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="검색어"
                />
              </label>
            </div>
            <div className="rail" style={{ marginTop: 10 }}>
              {(data?.breadcrumbs ?? []).map((crumb) => (
                <button
                  key={crumb.path}
                  type="button"
                  className={crumb.path === folder ? "chip on" : "chip"}
                  style={{ paddingLeft: 9 }}
                  onClick={() => enter(crumb.path)}
                >
                  {crumb.name}
                </button>
              ))}
            </div>
          </div>
        </section>

        {(data?.folders ?? []).length > 0 ? (
          <section className="card">
            <div className="card-head">
              <h2>폴더</h2>
              <span className="sub m">{data?.folders.length}개</span>
            </div>
            <div className="card-body">
              <div className="rail">
                {(data?.folders ?? []).map((item) => (
                  <button
                    key={item.path}
                    type="button"
                    className="chip"
                    style={{ paddingLeft: 9 }}
                    onClick={() => enter(item.path)}
                  >
                    <Icon name="folder" size={14} />
                    {item.name}
                  </button>
                ))}
              </div>
            </div>
          </section>
        ) : null}

        <section className="card">
          <div className="card-head">
            <h2>
              파일
              <span className="n" style={{ marginLeft: 7 }}>
                {files.length}
              </span>
            </h2>
            <span className="btns">
              {prompts.data?.items?.length ? (
                <select
                  className="ctl sm"
                  value={promptId}
                  onChange={(event) => setPromptId(event.target.value)}
                  aria-label="번역 프롬프트"
                >
                  <option value="">기본 프롬프트</option>
                  {prompts.data.items
                    .filter((item) => !item.archived)
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                </select>
              ) : null}
              <button
                type="button"
                className="btn sec sm"
                onClick={() => setSelected(new Set(files.map((file) => file.path)))}
                disabled={!files.length}
              >
                전체 선택
              </button>
              <button
                type="button"
                className="btn sec sm"
                onClick={() => setSelected(new Set())}
                disabled={!selected.size}
              >
                해제
              </button>
              <button type="button" className="btn sm" disabled={!selected.size || busy} onClick={() => void start()}>
                <Icon name="play" size={13} />
                선택 {selected.size}건 시작
              </button>
            </span>
          </div>
          <div className="card-body">
            {notice ? (
              <p role="status" style={{ margin: "0 0 10px", color: "var(--ok)", fontSize: ".82rem" }}>
                {notice}
              </p>
            ) : null}
            {actionError ? (
              <p role="alert" style={{ margin: "0 0 10px", color: "var(--bad)", fontSize: ".82rem" }}>
                {actionError}
              </p>
            ) : null}
            {files.length === 0 ? (
              <p style={{ margin: 0, color: "var(--muted)", fontSize: ".82rem" }}>
                {status === "loading" ? "불러오는 중" : "표시할 파일이 없습니다"}
              </p>
            ) : (
              <div className="board">
                {files.map((file: MediaFile) => {
                  const on = selected.has(file.path);
                  return (
                    <button
                      key={file.path}
                      type="button"
                      className={on ? "mc on" : "mc"}
                      aria-pressed={on}
                      onClick={() => toggle(file.path)}
                      style={{ textAlign: "left", cursor: "pointer", font: "inherit" }}
                    >
                      <span className="pf">
                        {file.poster_path ? "포스터" : <Icon name="captions" size={20} />}
                      </span>
                      <span className="mb">
                        <span className="mt">{file.title}</span>
                        <span className="ml">{duration(file.duration_seconds)}</span>
                        <span className="ma">
                          {file.has_subtitle ? (
                            <span className="b ok">자막 있음</span>
                          ) : file.has_external_subtitle ? (
                            <span className="b line">외부 자막</span>
                          ) : (
                            <span className="b">미처리</span>
                          )}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </section>

        <p style={{ margin: 0, fontSize: ".78rem", color: "var(--muted)" }}>
          시작한 작업은 <Link href="/jobs">작업 목록</Link>에서 확인할 수 있습니다.
        </p>
      </div>
    </>
  );
}
