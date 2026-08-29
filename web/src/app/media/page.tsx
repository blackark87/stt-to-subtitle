"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { LoadingOverlay } from "@/components/LoadingOverlay";
import { api, type MediaFile, type MediaFolder } from "@/lib/api";
import { useLiveQuery } from "@/lib/useLiveQuery";

const MEDIA_INTERVAL_MS = 15000;
type SubtitleFilter = "all" | "none" | "done";
type Operation = "full" | "compare";
type FolderSort = "name" | "modified_desc" | "modified_asc";
type FileSort = "filename" | "created_desc" | "modified_desc" | "nfo_title" | "nfo_release_desc";

function duration(seconds: number | null): string {
  if (!seconds || seconds <= 0) return "재생 시간 미확인";
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return h > 0 ? `${h}시간 ${m}분` : `${m}분`;
}

function folderModifiedAt(value: number | null): string {
  if (value == null) return "수정일 미확인";
  return new Date(value * 1000).toLocaleDateString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
}

export default function MediaPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const folder = searchParams.get("folder") ?? "";
  const query = searchParams.get("q") ?? "";
  const subtitleValue = searchParams.get("subtitle");
  const subtitle: SubtitleFilter = subtitleValue === "none" || subtitleValue === "done"
    ? subtitleValue
    : "all";
  const operation: Operation = searchParams.get("operation") === "compare" ? "compare" : "full";
  const folderSortValue = searchParams.get("folder_sort");
  const folderSort: FolderSort = folderSortValue === "modified_desc" || folderSortValue === "modified_asc"
    ? folderSortValue
    : "name";
  const fileSortValue = searchParams.get("file_sort");
  const fileSort: FileSort = fileSortValue === "created_desc"
    || fileSortValue === "modified_desc"
    || fileSortValue === "nfo_title"
    || fileSortValue === "nfo_release_desc"
    ? fileSortValue
    : "filename";
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [promptId, setPromptId] = useState("");

  const updateLocation = (
    changes: Record<string, string | null>,
    { push = false }: { push?: boolean } = {},
  ) => {
    const params = new URLSearchParams(searchParams.toString());
    Object.entries(changes).forEach(([key, value]) => {
      if (value) params.set(key, value);
      else params.delete(key);
    });
    const suffix = params.toString();
    const href = suffix ? `/media?${suffix}` : "/media";
    if (push) router.push(href);
    else router.replace(href, { scroll: false });
    setSelected(new Set());
    setNotice(null);
  };

  const fetcher = useCallback(
    () => api.media({ folder, q: query, folderSort, fileSort, folderLimit: 20 }),
    [fileSort, folder, folderSort, query],
  );
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, MEDIA_INTERVAL_MS);
  const prompts = useLiveQuery(useCallback(() => api.promptCategories(), []), 60000);

  const files = useMemo(() => {
    const list = data?.files ?? [];
    if (subtitle === "none") return list.filter((file) => !file.has_subtitle);
    if (subtitle === "done") return list.filter((file) => file.has_subtitle);
    return list;
  }, [data, subtitle]);
  const hasNfoTitles = files.some((file) => Boolean(file.nfo_title));
  const hasNfoReleaseDates = files.some((file) => Boolean(file.nfo_release_date));

  useEffect(() => {
    if (!data) return;
    const unavailable = (fileSort === "nfo_title" && !hasNfoTitles)
      || (fileSort === "nfo_release_desc" && !hasNfoReleaseDates);
    if (!unavailable) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("file_sort");
    const suffix = params.toString();
    router.replace(suffix ? `/media?${suffix}` : "/media", { scroll: false });
  }, [data, fileSort, hasNfoReleaseDates, hasNfoTitles, router, searchParams]);

  const selectedFiles = useMemo(
    () => files.filter((file) => selected.has(file.path)),
    [files, selected],
  );

  const folders = useMemo(() => {
    return data?.folders ?? [];
  }, [data?.folders]);

  const toggle = (path: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const start = async () => {
    const rels = selectedFiles.flatMap((file) => file.paths ?? [file.path]);
    if (!rels.length) return;
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await api.createJobs({
        source_rels: rels,
        operation,
        prompt_category_id: operation === "full" ? (promptId || null) : null,
      });
      setNotice(`${rels.length}건의 ${operation === "compare" ? "전사 비교" : "자막 작업"}을 시작했습니다.`);
      setSelected(new Set());
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const enter = (path: string) => {
    updateLocation({ folder: path || null }, { push: true });
  };

  return (
    <>
      <LoadingOverlay
        active={refreshing || prompts.refreshing || busy}
        message={busy ? "작업 요청을 처리하는 중입니다" : "미디어 파일을 스캔하는 중입니다"}
        detail={busy ? "선택한 파일을 작업 목록에 등록하고 있습니다." : "폴더의 파일과 메타데이터를 확인하고 있습니다. 잠시만 기다려 주세요."}
      />
      <header className="topbar">
        <div className="page-title">
          <h1>미디어</h1>
          <p>원본을 찾고 자막 작업 또는 전사 비교를 시작합니다.</p>
        </div>
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        <button type="button" className="btn sec" disabled={refreshing} onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />
          새로고침
        </button>
      </header>

      <div className="content">
        <section className="card explore-card" aria-labelledby="media-search-title">
          <div className="card-head">
            <h2 id="media-search-title">미디어 탐색</h2>
            <div className="seg" role="group" aria-label="자막 상태 필터">
              {([
                ["all", "전체"],
                ["none", "자막 없음"],
                ["done", "자막 있음"],
              ] as [SubtitleFilter, string][]).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  aria-pressed={subtitle === key}
                  className={subtitle === key ? "on" : undefined}
                  onClick={() => updateLocation({ subtitle: key === "all" ? null : key })}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="card-body search-panel">
            <form className="media-search-form" onSubmit={(event) => {
              event.preventDefault();
              const form = new FormData(event.currentTarget);
              updateLocation({ q: String(form.get("q") ?? "").trim() || null });
            }}>
              <label className="f media-search">
                <span className="lb">제목 · 파일명 · 배우</span>
                <input key={query} name="q" type="search" className="ctl" defaultValue={query} placeholder="검색어를 입력하세요" />
              </label>
              <button type="submit" className="btn sec"><Icon name="search" size={14} />검색</button>
            </form>
            <nav className="breadcrumbs" aria-label="현재 폴더 경로">
              {(data?.breadcrumbs ?? []).map((crumb) => (
                <button
                  key={crumb.path}
                  type="button"
                  aria-current={crumb.path === folder ? "location" : undefined}
                  onClick={() => enter(crumb.path)}
                >
                  {crumb.name}
                </button>
              ))}
            </nav>
          </div>
        </section>

        {folders.length > 0 ? (
          <section className="card" aria-labelledby="folder-title">
            <div className="card-head">
              <div>
                <h2 id="folder-title">하위 폴더</h2>
                <span className="sub m" title={`최대 20개 표시 · 전체 ${data?.folder_total ?? folders.length}개`}>최대 20개 표시 · 전체 {data?.folder_total ?? folders.length}개</span>
              </div>
              <label className="compact-field folder-sort-control">
                <span>정렬</span>
                <select
                  className="ctl"
                  value={folderSort}
                  onChange={(event) => updateLocation({
                    folder_sort: event.target.value === "name" ? null : event.target.value,
                  })}
                >
                  <option value="name">이름순</option>
                  <option value="modified_desc">수정일 최신순</option>
                  <option value="modified_asc">수정일 오래된순</option>
                </select>
              </label>
            </div>
            <div className="card-body folder-grid">
              {folders.map((item: MediaFolder) => (
                <button key={item.path} type="button" className="folder-button" onClick={() => enter(item.path)}>
                  {item.actor_image_path ? (
                    <span className="folder-profile">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={api.actorImageUrl(item.actor_image_path)} alt="" loading="lazy" />
                    </span>
                  ) : <span className="folder-icon"><Icon name="folder" size={18} /></span>}
                  <span className="folder-copy">
                    <strong title={item.display_path ?? item.path}>{item.display_name ?? item.name}</strong>
                    <small>{folderModifiedAt(item.modified_at)}</small>
                  </span>
                  <Icon name="chevron_right" size={14} />
                </button>
              ))}
            </div>
          </section>
        ) : null}

        <section className="card" aria-labelledby="files-title">
          <div className="card-head file-toolbar">
            <div>
              <h2 id="files-title">파일</h2>
              <span className="sub m" title={`${files.length}개 표시`}>{files.length}개 표시</span>
            </div>
            <div className="btns">
              <label className="compact-field">
                <span>정렬</span>
                <select
                  className="ctl"
                  value={fileSort}
                  onChange={(event) => updateLocation({
                    file_sort: event.target.value === "filename" ? null : event.target.value,
                  })}
                >
                  <option value="created_desc">미디어 생성일 최신순</option>
                  <option value="modified_desc">미디어 수정일 최신순</option>
                  {hasNfoTitles ? <option value="nfo_title">NFO 제목순</option> : null}
                  <option value="filename">파일명순</option>
                  {hasNfoReleaseDates ? <option value="nfo_release_desc">NFO 출시일 최신순</option> : null}
                </select>
              </label>
              <label className="compact-field">
                <span>작업</span>
                <select className="ctl" value={operation} onChange={(event) => updateLocation({ operation: event.target.value === "compare" ? "compare" : null })}>
                  <option value="full">자막 생성</option>
                  <option value="compare">전사 비교</option>
                </select>
              </label>
              {operation === "full" && prompts.data?.items?.length ? (
                <label className="compact-field">
                  <span>번역 프롬프트</span>
                  <select className="ctl" value={promptId} onChange={(event) => setPromptId(event.target.value)}>
                    <option value="">기본 프롬프트</option>
                    {prompts.data.items.filter((item) => !item.archived).map((item) => (
                      <option key={item.id} value={item.id}>{item.name}</option>
                    ))}
                  </select>
                </label>
              ) : null}
              <button type="button" className="btn sec" onClick={() => setSelected(new Set(files.map((file) => file.path)))} disabled={!files.length}>
                전체 선택
              </button>
              <button type="button" className="btn sec" onClick={() => setSelected(new Set())} disabled={!selectedFiles.length}>
                선택 해제
              </button>
              <button type="button" className="btn" disabled={!selectedFiles.length || busy} onClick={() => void start()}>
                <Icon name="play" size={14} />
                {busy ? "요청 중" : `선택 ${selectedFiles.length}건 시작`}
              </button>
            </div>
          </div>
          <div className="card-body">
            {notice ? <p className="notice success" role="status">{notice} <Link href="/jobs">작업 목록 보기</Link></p> : null}
            {actionError ? <p className="notice error" role="alert">{actionError}</p> : null}
            {files.length === 0 ? (
              <div className="empty-state">
                <Icon name="folder" size={24} />
                <strong>표시할 파일이 없습니다</strong>
                <span>검색어나 자막 상태 필터를 변경해 보세요.</span>
              </div>
            ) : (
              <div className="board">
                {files.map((file: MediaFile) => {
                  const on = selected.has(file.path);
                  const title = file.title || file.name;
                  return (
                    <button
                      key={file.path}
                      type="button"
                      className={on ? "mc on" : "mc"}
                      aria-pressed={on}
                      onClick={() => toggle(file.path)}
                    >
                      <span className="pf">
                        {file.poster_path ? (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img src={api.posterUrl(file.poster_path)} alt="" loading="lazy" />
                        ) : (
                          <span className="poster-fallback"><Icon name="captions" size={26} /><small>포스터 없음</small></span>
                        )}
                        <span className="selection-mark" aria-hidden>{on ? "선택됨" : "선택"}</span>
                      </span>
                      <span className="mb">
                        <strong className="mt" title={title}>{title}</strong>
                        <span className="ml" title={file.display_path ?? file.path}>
                          {file.display_path ?? file.display_name ?? file.name}
                        </span>
                        <span className="media-meta">
                          <span title={duration(file.duration_seconds)}>{duration(file.duration_seconds)}</span>
                          {file.nfo_release_date ? <span title={`NFO 출시일 ${file.nfo_release_date}`}>출시 {file.nfo_release_date}</span> : null}
                          {file.actors.length ? <span title={file.actors.join(", ")}>{file.actors.slice(0, 2).join(" · ")}</span> : null}
                        </span>
                        <span className="ma">
                          {file.has_subtitle ? <span className="b ok">자막 있음</span>
                            : file.has_external_subtitle ? <span className="b line">외부 자막</span>
                              : <span className="b">미처리</span>}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </section>
      </div>
    </>
  );
}
