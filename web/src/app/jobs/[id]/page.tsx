"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api } from "@/lib/api";
import {
  PHASE_LABEL,
  STATE_LABEL,
  asJobState,
  reasonLabel,
  type JobPhase,
  type JobState,
} from "@/lib/domain";
import { fileName, parentPath } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

/* design/mockup body_JobDetail.html: 결과 미리보기 + 자막(2열) + 기록. */

const INTERVAL_MS = 5000;

const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

interface Segment {
  id: string;
  start: number;
  end: number;
  ja: string;
  ko: string;
}

function timecode(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** 산출물 스키마가 배열/객체 어느 쪽이어도 목록을 뽑는다. */
function itemsOf(payload: unknown): Record<string, unknown>[] {
  if (Array.isArray(payload)) return payload as Record<string, unknown>[];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    for (const key of ["segments", "translations", "items"]) {
      const value = record[key];
      if (Array.isArray(value)) return value as Record<string, unknown>[];
    }
  }
  return [];
}

export default function JobDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);

  const fetcher = useCallback(() => api.job(id), [id]);
  const { data: job, status, error, updatedAt, refresh } = useLiveQuery(fetcher, INTERVAL_MS);

  const [segments, setSegments] = useState<Segment[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  // 전사(일본어)와 번역(한국어)을 id 로 합친다. 시안의 병기 목록이 이걸 요구한다.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [transcript, translation] = await Promise.all([
        api.artifact(id, "transcript"),
        api.artifact(id, "translation"),
      ]);
      if (cancelled) return;
      const korean = new Map<string, string>();
      for (const item of itemsOf(translation)) {
        const key = String(item.id ?? "");
        if (key) korean.set(key, String(item.text ?? ""));
      }
      const merged = itemsOf(transcript).map((item) => {
        const key = String(item.id ?? "");
        return {
          id: key,
          start: Number(item.start ?? 0),
          end: Number(item.end ?? 0),
          ja: String(item.text ?? ""),
          ko: korean.get(key) ?? "",
        };
      });
      setSegments(merged);
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  const translated = useMemo(() => segments.filter((s) => s.ko).length, [segments]);

  const seek = (segment: Segment) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = segment.start;
    void video.play().catch(() => undefined);
  };

  const act = async (task: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await task();
      await refresh();
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const state = job ? asJobState(job.state) : null;

  return (
    <>
      <header className="topbar">
        <Link className="btn sec sm" href="/jobs">
          <Icon name="chevron_left" size={14} />
          작업 목록
        </Link>
        <h1 style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {job ? fileName(job.source_rel) : "작업"}
        </h1>
        {state ? <span className={BADGE_CLASS[state]}>{STATE_LABEL[state]}</span> : null}
        <span style={{ marginLeft: "auto" }}>
          <Freshness status={status} updatedAt={updatedAt} error={error} />
        </span>
      </header>

      <div className="content">
        {actionError ? (
          <p role="alert" style={{ margin: 0, color: "var(--bad)", fontSize: ".82rem" }}>{actionError}</p>
        ) : null}

        <section className="card">
          <div className="card-head">
            <h2>진행</h2>
            <span className="btns">
              <span className="b line">{job ? (PHASE_LABEL[job.phase as JobPhase] ?? job.phase) : "—"}</span>
              {job?.reason_code ? <span className="b hold">{reasonLabel(job.reason_code)}</span> : null}
              <button type="button" className="btn sec sm" disabled={!job || busy} onClick={() => void act(() => api.retryJob(id))}>
                <Icon name="refresh" size={13} />
                재시도
              </button>
              <button type="button" className="btn sec sm" disabled={!job || busy} onClick={() => void act(() => api.resumeTranslation(id))}>
                <Icon name="play" size={13} />
                번역 재개
              </button>
              <button type="button" className="btn dgr sm" disabled={!job || busy} onClick={() => void act(() => api.stopJob(id))}>
                <Icon name="alert_triangle" size={13} />
                정지
              </button>
            </span>
          </div>
          <div className="card-body">
            <div className="t-name">
              <b>{job ? fileName(job.source_rel) : "—"}</b>
              <span className="m">{job ? parentPath(job.source_rel) : ""}</span>
            </div>
          </div>
        </section>

        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.45fr) minmax(340px, .55fr)", gap: 14, alignItems: "start" }}>
          <section className="card">
            <div className="card-head">
              <h2>결과 미리보기</h2>
              <span className="btns">
                <a className="btn sec sm" href={`/api/v1/jobs/${encodeURIComponent(id)}/subtitle.srt`}>
                  <Icon name="download" size={13} />
                  SRT
                </a>
                <a className="btn sec sm" href={`/api/v1/jobs/${encodeURIComponent(id)}/subtitle.ass`}>
                  <Icon name="download" size={13} />
                  ASS
                </a>
              </span>
            </div>
            <div className="card-body">
              {job ? (
                <video
                  ref={videoRef}
                  controls
                  preload="metadata"
                  playsInline
                  crossOrigin="anonymous"
                  style={{ width: "100%", aspectRatio: "16 / 9", borderRadius: "var(--r-md)", background: "#10131A" }}
                  onTimeUpdate={(event) => {
                    const t = event.currentTarget.currentTime;
                    const hit = segments.find((s) => t >= s.start && t < s.end);
                    setCurrent(hit ? hit.id : null);
                  }}
                >
                  <source src={api.mediaFileUrl(job.source_rel)} />
                  <track kind="subtitles" srcLang="ko" label="한국어" default src={api.subtitlesUrl(id)} />
                </video>
              ) : (
                <p style={{ margin: 0, color: "var(--muted)", fontSize: ".82rem" }}>불러오는 중</p>
              )}
            </div>
          </section>

          <section className="card">
            <div className="card-head">
              <h2>자막</h2>
              <span className="btns">
                <span className="b run">
                  {translated} / {segments.length}
                </span>
              </span>
            </div>
            <div className="card-body flush" style={{ maxHeight: 520, overflow: "auto" }}>
              <div className="tbl" role="listbox" aria-label="자막 대사 목록">
                {segments.length === 0 ? (
                  <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                    아직 전사 결과가 없습니다
                  </div>
                ) : (
                  segments.map((segment) => {
                    const on = current === segment.id;
                    return (
                      <div
                        key={segment.id}
                        role="option"
                        tabIndex={0}
                        aria-selected={on}
                        onClick={() => seek(segment)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") seek(segment);
                        }}
                        className={on ? "tr on-run" : "tr"}
                        style={{
                          gridTemplateColumns: "96px minmax(0, 1fr)",
                          alignItems: "start",
                          paddingTop: 7,
                          paddingBottom: 7,
                          cursor: "pointer",
                        }}
                      >
                        <span
                          className="code"
                          style={{
                            fontSize: ".7rem",
                            color: on ? "var(--accent)" : "var(--muted)",
                            fontWeight: on ? 600 : 400,
                          }}
                        >
                          {timecode(segment.start)}
                        </span>
                        <span style={{ minWidth: 0 }}>
                          <span style={{ display: "block", fontSize: ".84rem", fontWeight: on ? 650 : 400 }}>
                            {segment.ko || "— 번역 대기"}
                          </span>
                          <span style={{ display: "block", marginTop: 2, fontSize: ".74rem", color: "var(--muted)" }}>
                            {segment.ja}
                          </span>
                        </span>
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </section>
        </div>
      </div>
    </>
  );
}
