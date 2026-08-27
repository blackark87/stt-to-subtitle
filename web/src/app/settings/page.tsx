"use client";

import { useCallback, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api, type RuntimeEndpoint } from "@/lib/api";
import { RUNTIME_STATUS_LABEL, RUNTIME_STATUS_TONE, asRuntimeStatus, type JobState } from "@/lib/domain";
import { useLiveQuery } from "@/lib/useLiveQuery";

/* design/mockup body_Settings.html 구조: 전사 서버 / 번역 서버 / 번역 프롬프트. */

const SETTINGS_INTERVAL_MS = 15000;

const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

interface RuntimeForm {
  id: string | null;
  name: string;
  base_url: string;
  token: string;
  capacity: number;
}
const EMPTY: RuntimeForm = { id: null, name: "", base_url: "", token: "", capacity: 1 };

export default function SettingsPage() {
  const fetcher = useCallback(() => api.settings(), []);
  const { data, status, error, updatedAt, refresh } = useLiveQuery(fetcher, SETTINGS_INTERVAL_MS);

  const [form, setForm] = useState<RuntimeForm>(EMPTY);
  const [lm, setLm] = useState({ base_url: "", model: "", token: "", workers: 1 });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const servers = data?.servers;
  // 서버 값이 처음 도착했을 때만 폼을 채운다. 이후 배경 갱신이 입력 중인 값을
  // 덮어쓰지 않도록 렌더 중 파생으로 처리한다(effect 에서 setState 금지).
  const [seeded, setSeeded] = useState(false);
  if (servers && !seeded) {
    setSeeded(true);
    setLm({
      base_url: servers.lm_base_url ?? "",
      model: servers.lm_model ?? "",
      token: "",
      workers: servers.translation_workers ?? 1,
    });
  }

  const guard = async (task: () => Promise<unknown>, message?: string) => {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await task();
      if (message) setNotice(message);
      await refresh();
      return true;
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const submitRuntime = async (event: React.FormEvent) => {
    event.preventDefault();
    const existing = (data?.runtimes ?? []).find((item) => item.id === form.id);
    const ok = await guard(
      () =>
        existing
          ? api.updateRuntime(existing.id, {
              name: form.name,
              base_url: form.base_url,
              token: form.token || null,
              enabled: existing.enabled,
              capacity: form.capacity,
            })
          : api.createRuntime({
              name: form.name,
              base_url: form.base_url,
              token: form.token,
              enabled: true,
              capacity: form.capacity,
            }),
      existing ? "Runtime 을 수정했습니다." : "Runtime 을 추가했습니다.",
    );
    if (ok) setForm(EMPTY);
  };

  const submitServers = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!servers) return;
    await guard(
      () =>
        api.updateServers({
          stt_base_url: servers.stt_base_url,
          lm_base_url: lm.base_url,
          lm_token: lm.token || null,
          lm_model: lm.model,
          translation_workers: lm.workers,
        }),
      "번역 서버 설정을 저장했습니다.",
    );
  };

  const field = "ctl";
  const runtimes = data?.runtimes ?? [];

  return (
    <>
      <header className="topbar">
        <h1>설정</h1>
        <span style={{ marginLeft: "auto" }}>
          <Freshness status={status} updatedAt={updatedAt} error={error} />
        </span>
        <button type="button" className="btn sec sm" onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />
          새로고침
        </button>
      </header>

      <div className="content">
        {notice ? (
          <p role="status" style={{ margin: 0, color: "var(--ok)", fontSize: ".82rem" }}>{notice}</p>
        ) : null}
        {actionError ? (
          <p role="alert" style={{ margin: 0, color: "var(--bad)", fontSize: ".82rem" }}>{actionError}</p>
        ) : null}

        <section className="card">
          <div className="card-head">
            <h2>전사 서버</h2>
            <span className="sub m">{runtimes.length}개 Runtime</span>
          </div>
          <div className="card-body flush">
            <div className="tbl">
              <div className="tr head" style={{ gridTemplateColumns: "minmax(0, 1fr) 128px 96px 200px" }}>
                <span>Runtime</span>
                <span>상태</span>
                <span className="r">작업</span>
                <span className="r">관리</span>
              </div>
              {runtimes.length === 0 ? (
                <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                  {status === "loading" ? "불러오는 중" : "등록된 Runtime 없음"}
                </div>
              ) : (
                runtimes.map((runtime: RuntimeEndpoint) => {
                  const parsed = asRuntimeStatus(runtime.status);
                  const tone = parsed ? RUNTIME_STATUS_TONE[parsed] : null;
                  return (
                    <div key={runtime.id} className="tr" style={{ gridTemplateColumns: "minmax(0, 1fr) 128px 96px 200px" }}>
                      <div className="t-name">
                        <b>
                          {runtime.name}
                          {runtime.builtin ? <span className="b line" style={{ marginLeft: 6 }}>기본</span> : null}
                        </b>
                        <span className="m">{runtime.base_url}</span>
                      </div>
                      <span className={tone ? BADGE_CLASS[tone] : "b"} title={runtime.message ?? undefined}>
                        {parsed ? RUNTIME_STATUS_LABEL[parsed] : runtime.status}
                      </span>
                      <span className="r m" style={{ fontSize: ".78rem" }}>
                        {runtime.running_jobs} / {runtime.capacity}
                      </span>
                      <span className="btns" style={{ justifyContent: "flex-end" }}>
                        <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.probeRuntime(runtime.id))}>
                          확인
                        </button>
                        {runtime.builtin ? null : (
                          <>
                            <button
                              type="button"
                              className="btn sec sm"
                              disabled={busy}
                              onClick={() =>
                                void guard(() =>
                                  api.updateRuntime(runtime.id, {
                                    name: runtime.name,
                                    base_url: runtime.base_url,
                                    enabled: !runtime.enabled,
                                    capacity: runtime.capacity,
                                  }),
                                )
                              }
                            >
                              {runtime.enabled ? "중지" : "사용"}
                            </button>
                            <button
                              type="button"
                              className="btn sec sm"
                              disabled={busy}
                              onClick={() =>
                                setForm({
                                  id: runtime.id,
                                  name: runtime.name,
                                  base_url: runtime.base_url,
                                  token: "",
                                  capacity: runtime.capacity,
                                })
                              }
                            >
                              <Icon name="pencil" size={13} />
                            </button>
                            <button
                              type="button"
                              className="btn dgr sm"
                              disabled={busy}
                              onClick={() => {
                                if (!window.confirm(`${runtime.name} Runtime을 삭제할까요?`)) return;
                                void guard(() => api.deleteRuntime(runtime.id), "삭제했습니다.");
                              }}
                            >
                              <Icon name="trash" size={13} />
                            </button>
                          </>
                        )}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
          <div className="card-body">
            <form onSubmit={submitRuntime} className="fg">
              <label className="f">
                <span className="lb">이름</span>
                <input required maxLength={80} className={field} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="GPU Runtime 02" />
              </label>
              <label className="f">
                <span className="lb">API 주소</span>
                <input required type="url" className={field} value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="http://runtime-host:8100" />
              </label>
              <label className="f">
                <span className="lb">API 토큰</span>
                <input type="password" autoComplete="new-password" className={field} value={form.token} onChange={(e) => setForm({ ...form, token: e.target.value })} placeholder={form.id ? "비우면 유지" : ""} />
              </label>
              <label className="f">
                <span className="lb">할당 슬롯</span>
                <input type="number" min={1} max={8} className={field} value={form.capacity} onChange={(e) => setForm({ ...form, capacity: Number(e.target.value) })} />
              </label>
              <div className="w btns">
                <button type="submit" className="btn sm" disabled={busy}>
                  {form.id ? "변경 저장" : "Runtime 추가"}
                </button>
                {form.id ? (
                  <button type="button" className="btn sec sm" onClick={() => setForm(EMPTY)}>취소</button>
                ) : null}
              </div>
            </form>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>번역 서버</h2>
            {servers ? (
              <span className={servers.translation_configured ? "b ok" : "b wait"}>
                {servers.translation_configured ? "설정됨" : "미설정"}
              </span>
            ) : null}
          </div>
          <div className="card-body">
            <form onSubmit={submitServers} className="fg">
              <label className="f">
                <span className="lb">API 주소</span>
                <input type="url" className={field} value={lm.base_url} onChange={(e) => setLm({ ...lm, base_url: e.target.value })} placeholder="http://lm-host:8000/v1" />
              </label>
              <label className="f">
                <span className="lb">모델</span>
                <input className={field} value={lm.model} onChange={(e) => setLm({ ...lm, model: e.target.value })} />
              </label>
              <label className="f">
                <span className="lb">API 토큰</span>
                <input type="password" autoComplete="new-password" className={field} value={lm.token} onChange={(e) => setLm({ ...lm, token: e.target.value })} placeholder={servers?.lm_token_configured ? "비우면 유지" : ""} />
              </label>
              <label className="f">
                <span className="lb">번역 동시 실행</span>
                <input type="number" min={1} max={8} className={field} value={lm.workers} onChange={(e) => setLm({ ...lm, workers: Number(e.target.value) })} />
              </label>
              <div className="w btns">
                <button type="submit" className="btn sm" disabled={busy}>저장</button>
              </div>
            </form>
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <h2>번역 프롬프트</h2>
            <span className="sub m">{(data?.prompt_categories ?? []).length}개</span>
          </div>
          <div className="card-body flush">
            <div className="tbl">
              {(data?.prompt_categories ?? []).length === 0 ? (
                <div className="tr empty" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>등록된 프롬프트 없음</div>
              ) : (
                (data?.prompt_categories ?? []).map((category) => (
                  <div key={category.id} className="tr" style={{ gridTemplateColumns: "minmax(0, 1fr) 74px" }}>
                    <span className="t-name"><b>{category.name}</b></span>
                    <span className="r">
                      {category.archived ? <span className="b hold">보관</span> : <span className="b ok">사용</span>}
                    </span>
                  </div>
                ))
              )}
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
