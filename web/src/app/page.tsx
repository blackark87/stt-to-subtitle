"use client";

import { useCallback } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api, type PipelineJob } from "@/lib/api";
import {
  JOB_PHASES,
  PHASE_LABEL,
  STATE_LABEL,
  asJobState,
  reasonLabel,
  type JobPhase,
  type JobState,
} from "@/lib/domain";
import { clock, fileName, parentPath, percent } from "@/lib/format";
import { useLiveQuery } from "@/lib/useLiveQuery";

/* design/templates/dashboard.html (38dbb0d) 구조를 그대로 옮긴다. */

const DASHBOARD_INTERVAL_MS = 5000;

/** 배지 톤. 시안의 .b run/wait/hold/bad/ok 클래스에 대응시킨다. */
const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

/** 단계 칩 클래스. 시안의 .st done/run/fail/hold/wait/idle. */
function stageClass(state: JobState | "pending"): string {
  if (state === "done") return "st done";
  if (state === "running") return "st run";
  if (state === "failed") return "st fail";
  if (state === "blocked" || state === "paused" || state === "stopped") return "st hold";
  if (state === "waiting") return "st wait";
  return "st idle";
}

const PIPELINE_STAGES: { phase: JobPhase; label: string }[] = [
  { phase: "extraction", label: "추출" },
  { phase: "transcription", label: "전사" },
  { phase: "translation", label: "번역" },
];

/** 진행 중인 phase 의 진행률. 없으면 null. */
function jobPercent(job: PipelineJob): number | null {
  if (job.phase === "transcription") {
    return percent(job.chunks_completed, Math.max(job.chunks_created, job.chunks_total_estimate));
  }
  if (job.phase === "translation") {
    return percent(job.translation_chunks_completed, job.translation_chunks_total);
  }
  return null;
}

/** 작업의 단계 스트립. 현재 phase 이전은 done, 이후는 idle. */
function stageStates(job: PipelineJob): { label: string; state: JobState | "pending"; pct: number }[] {
  const order: JobPhase[] = JOB_PHASES.filter((p): p is JobPhase => p !== "complete");
  const current = order.indexOf(job.phase as JobPhase);
  const state = asJobState(job.state);
  return order.map((phase, index) => {
    if (current < 0 || index < current) return { label: PHASE_LABEL[phase], state: "done" as const, pct: 100 };
    if (index > current) return { label: PHASE_LABEL[phase], state: "pending" as const, pct: 0 };
    const pct = jobPercent(job) ?? (state === "done" ? 100 : 0);
    return { label: PHASE_LABEL[phase], state: state ?? "pending", pct };
  });
}

export default function DashboardPage() {
  const fetcher = useCallback(() => api.dashboard(), []);
  const { data, status, error, updatedAt, refresh } = useLiveQuery(fetcher, DASHBOARD_INTERVAL_MS);

  const jobs = data?.recent_jobs ?? [];
  const counts = data?.state_counts ?? {};
  const gpu = data?.gpu ?? null;

  const byState = (states: JobState[]) =>
    jobs.filter((job) => {
      const state = asJobState(job.state);
      return state != null && states.includes(state);
    });

  const running = byState(["running"]);
  const stopped = byState(["blocked", "failed", "paused", "stopped"]);
  const completed = byState(["done"]);
  const queue = byState(["waiting"]);

  // 백엔드가 pipeline_slots 를 주지 않으므로 실행 중 작업에서 phase 로 유도한다.
  const slots = PIPELINE_STAGES.map((stage) => {
    const job = running.find((item) => item.phase === stage.phase) ?? null;
    return { stage: stage.label, job, pct: job ? (jobPercent(job) ?? 0) : 0 };
  });
  const busy = slots.filter((slot) => slot.job).length;

  return (
    <>
      <header className="topbar">
        <h1>대시보드</h1>
        <span className="btns" style={{ gap: 5 }}>
          {(["running", "waiting", "blocked", "failed", "done"] as JobState[]).map((state) => (
            <span key={state} className={BADGE_CLASS[state]}>
              {STATE_LABEL[state]} {counts[state] ?? 0}
            </span>
          ))}
        </span>
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
            <h2>파이프라인</h2>
            <span className="sub m">
              {busy} / {slots.length} 슬롯
            </span>
          </div>
          <div className="card-body">
            <div
              style={{
                display: "grid",
                gridTemplateColumns: `repeat(${slots.length}, minmax(0, 1fr))`,
                gap: 10,
              }}
            >
              {slots.map((slot) => (
                <div
                  key={slot.stage}
                  className={slot.job ? "pn" : "pn is-idle"}
                  style={
                    slot.job
                      ? { borderColor: "var(--accent-line-soft)", background: "var(--accent-08)" }
                      : { borderStyle: "dashed", background: "transparent" }
                  }
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 7 }}>
                    <span className="b line">{slot.stage}</span>
                    {slot.job ? <span className="b run dot">실행</span> : <span className="b">유휴</span>}
                  </div>
                  <div className="t-name">
                    <b style={slot.job ? undefined : { color: "var(--muted)", fontWeight: 650 }}>
                      {slot.job ? fileName(slot.job.source_rel) : "비어 있음"}
                    </b>
                    <span className="m">{slot.job ? parentPath(slot.job.source_rel) : " "}</span>
                  </div>
                  {slot.job ? (
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8 }}>
                      <div className="bar">
                        <i style={{ width: `${slot.pct}%` }} />
                      </div>
                      <span className="m" style={{ fontSize: ".74rem", fontWeight: 600 }}>
                        {slot.pct}%
                      </span>
                    </div>
                  ) : (
                    <div style={{ height: 14 }} />
                  )}
                </div>
              ))}
            </div>
          </div>
        </section>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(0, 1fr) 340px",
            gap: 14,
            alignItems: "start",
          }}
        >
          <div style={{ display: "grid", gap: 14 }}>
            <section className="card">
              <div className="card-head">
                <h2>실행 중</h2>
              </div>
              <div className="card-body flush">
                <div className="tbl">
                  <div className="tr head" style={{ gridTemplateColumns: "minmax(0, 1fr) 368px 88px 116px" }}>
                    <span>작업</span>
                    <span>단계</span>
                    <span className="r">진행</span>
                    <span className="r">갱신</span>
                  </div>
                  {running.length === 0 ? (
                    <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                      {status === "loading" ? "불러오는 중" : "실행 중인 작업이 없습니다"}
                    </div>
                  ) : (
                    running.map((job) => (
                      <div
                        key={job.id}
                        className="tr tall on-run"
                        style={{ gridTemplateColumns: "minmax(0, 1fr) 368px 88px 116px" }}
                      >
                        <div className="t-name">
                          <b>{fileName(job.source_rel)}</b>
                          <span>{parentPath(job.source_rel)}</span>
                        </div>
                        <ol className="stages" aria-label={`${fileName(job.source_rel)} 단계`}>
                          {stageStates(job).map((stage) => (
                            <li key={stage.label} className={stageClass(stage.state)}>
                              <span className="st-i" aria-hidden>
                                <Icon
                                  name={
                                    stage.state === "done"
                                      ? "check"
                                      : stage.state === "running"
                                        ? "activity"
                                        : stage.state === "failed"
                                          ? "alert_triangle"
                                          : "clock"
                                  }
                                  size={11}
                                />
                              </span>
                              <span className="st-l">{stage.label}</span>
                              <span className="st-c">
                                {stage.state === "done"
                                  ? "완료"
                                  : stage.state === "running"
                                    ? `${stage.pct}%`
                                    : "대기"}
                              </span>
                              <div className="bar">
                                <i style={{ width: `${stage.pct}%` }} />
                              </div>
                            </li>
                          ))}
                        </ol>
                        <span
                          className="r m"
                          style={{ fontSize: ".95rem", fontWeight: 600, color: "var(--accent)" }}
                        >
                          {jobPercent(job) ?? 0}%
                        </span>
                        <span className="r m" style={{ fontSize: ".8rem", color: "var(--muted)" }}>
                          {clock(job.updated_at)}
                        </span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </section>

            {stopped.length > 0 ? (
              /* 카드 프레임은 중립. 상태색은 행에만 쓴다(H4). */
              <section className="card">
                <div className="card-head">
                  <h2>
                    멈춤
                    <span className="n" style={{ marginLeft: 7 }}>
                      {stopped.length}
                    </span>
                  </h2>
                </div>
                <div className="card-body flush">
                  <div className="tbl">
                    <div className="tr head" style={{ gridTemplateColumns: "92px minmax(0, 1fr) 250px 168px" }}>
                      <span>상태</span>
                      <span>작업</span>
                      <span>사유</span>
                      <span className="r">갱신</span>
                    </div>
                    {stopped.map((job) => {
                      const state = asJobState(job.state);
                      const reason = reasonLabel(job.reason_code) ?? job.error ?? "—";
                      return (
                        <div
                          key={job.id}
                          className={`tr ${state === "failed" ? "on-bad" : "on-hold"}`}
                          style={{ gridTemplateColumns: "92px minmax(0, 1fr) 250px 168px" }}
                        >
                          <span className={state ? BADGE_CLASS[state] : "b"}>
                            {state ? STATE_LABEL[state] : job.state}
                          </span>
                          <div className="t-name">
                            <b>{fileName(job.source_rel)}</b>
                            <span className="m">{parentPath(job.source_rel)}</span>
                          </div>
                          <span
                            className="m"
                            title={reason}
                            style={{
                              fontSize: ".76rem",
                              color: state === "failed" ? "var(--bad)" : "var(--muted)",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                            }}
                          >
                            {reason}
                          </span>
                          <span className="r m" style={{ fontSize: ".78rem", color: "var(--muted)" }}>
                            {clock(job.updated_at)}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </section>
            ) : null}

            <section className="card">
              <div className="card-head">
                <h2>최근 완료</h2>
              </div>
              <div className="card-body flush">
                <div className="tbl">
                  <div className="tr head" style={{ gridTemplateColumns: "minmax(0, 1fr) 120px 120px 100px" }}>
                    <span>작업</span>
                    <span className="r">전사</span>
                    <span className="r">번역</span>
                    <span className="r">완료 시각</span>
                  </div>
                  {completed.length === 0 ? (
                    <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                      완료 내역 없음
                    </div>
                  ) : (
                    completed.map((job) => (
                      <div
                        key={job.id}
                        className="tr on-ok"
                        style={{ gridTemplateColumns: "minmax(0, 1fr) 120px 120px 100px" }}
                      >
                        <div className="t-name">
                          <b>{fileName(job.source_rel)}</b>
                          <span>{parentPath(job.source_rel)}</span>
                        </div>
                        <span className="r m" style={{ fontSize: ".78rem" }}>
                          {job.chunks_completed || "—"}
                        </span>
                        <span className="r m" style={{ fontSize: ".78rem" }}>
                          {job.translation_chunks_completed || "—"}
                        </span>
                        <span className="r m" style={{ fontSize: ".78rem", fontWeight: 600 }}>
                          {clock(job.updated_at)}
                        </span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </section>
          </div>

          <div style={{ display: "grid", gap: 14 }}>
            <section className="card">
              <div className="card-head">
                <h2>GPU{gpu?.devices?.[0]?.index ? ` ${gpu.devices[0].index}` : " 0"}</h2>
                {gpu?.available ? null : <span className="sub m">수집 안 됨</span>}
              </div>
              <div className="card-body">
                {gpu?.available && gpu.devices.length ? (
                  gpu.devices.map((device, index) => (
                    <div key={device.index ?? index} style={{ display: "grid", gap: 10 }}>
                      <div className="pn inset">
                        <span className="pn-l">사용률</span>
                        <span className="pn-v m">
                          {device.utilization_percent?.toFixed(0) ?? "—"}
                          <small>%</small>
                        </span>
                      </div>
                      <div className="pn inset">
                        <span className="pn-l">메모리</span>
                        <span className="pn-v m">
                          {device.memory_percent?.toFixed(0) ?? "—"}
                          <small>%</small>
                        </span>
                        <span className="pn-d">
                          {device.memory_used_gib?.toFixed(1) ?? "—"} /{" "}
                          {device.memory_total_gib?.toFixed(1) ?? "—"} GiB
                        </span>
                        <div className="bar" style={{ marginTop: 6 }}>
                          <i style={{ width: `${device.memory_percent ?? 0}%` }} />
                        </div>
                      </div>
                      <div className="kv">
                        <span>온도</span>
                        <span className="m">{device.temperature_celsius?.toFixed(0) ?? "—"}°C</span>
                      </div>
                      <div className="kv">
                        <span>전력</span>
                        <span className="m">{device.power_watts?.toFixed(0) ?? "—"}W</span>
                      </div>
                    </div>
                  ))
                ) : (
                  <p className="muted" style={{ margin: 0, fontSize: ".82rem", color: "var(--muted)" }}>
                    GPU 메트릭이 설정되지 않았습니다.
                  </p>
                )}
              </div>
            </section>

            <section className="card">
              <div className="card-head">
                <h2>대기 큐</h2>
                <span className="sub m">{queue.length}건</span>
              </div>
              <div className="card-body flush">
                <div className="tbl">
                  {queue.length === 0 ? (
                    <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                      대기 작업 없음
                    </div>
                  ) : (
                    queue.map((job, index) => (
                      <div key={job.id} className="tr" style={{ gridTemplateColumns: "20px minmax(0, 1fr) 62px" }}>
                        <span className="m r" style={{ fontSize: ".72rem", color: "var(--muted)" }}>
                          {index + 1}
                        </span>
                        <span className="t-name">
                          <b>{fileName(job.source_rel)}</b>
                        </span>
                        <span className="r">
                          <span className="b wait">{PHASE_LABEL[job.phase as JobPhase] ?? job.phase}</span>
                        </span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            </section>
          </div>
        </div>
      </div>
    </>
  );
}
