"use client";

import { useCallback, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api, type PathDisplayRule, type PromptCategory, type RuntimeEndpoint } from "@/lib/api";
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

interface PromptForm {
  id: string | null;
  name: string;
  translation_prompt: string;
  review_prompt: string;
}
const EMPTY_PROMPT: PromptForm = { id: null, name: "", translation_prompt: "", review_prompt: "" };

interface PathRuleForm {
  id: string | null;
  source_pattern: string;
  display_pattern: string;
}
const EMPTY_PATH_RULE: PathRuleForm = { id: null, source_pattern: "", display_pattern: "" };

export default function SettingsPage() {
  const fetcher = useCallback(() => api.settings(), []);
  const { data, status, error, updatedAt, refreshing, refresh } = useLiveQuery(fetcher, SETTINGS_INTERVAL_MS);

  const [form, setForm] = useState<RuntimeForm>(EMPTY);
  const [lmDraft, setLm] = useState<{
    base_url: string;
    model: string;
    token: string;
    workers: number;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [promptForm, setPromptForm] = useState<PromptForm>(EMPTY_PROMPT);
  const [pathRuleForm, setPathRuleForm] = useState<PathRuleForm>(EMPTY_PATH_RULE);

  const servers = data?.servers;
  const lm = lmDraft ?? {
    base_url: servers?.lm_base_url ?? "",
    model: servers?.lm_model ?? "",
    token: "",
    workers: servers?.translation_workers ?? 1,
  };

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
      existing ? "Runtime을 수정했습니다." : "Runtime을 추가했습니다.",
    );
    if (ok) setForm(EMPTY);
  };

  const submitServers = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!servers) return;
    const ok = await guard(
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
    if (ok) setLm(null);
  };

  const submitPrompt = async (event: React.FormEvent) => {
    event.preventDefault();
    const body = {
      name: promptForm.name,
      translation_prompt: promptForm.translation_prompt,
      review_prompt: promptForm.review_prompt,
    };
    const ok = await guard(
      () => promptForm.id
        ? api.updatePromptCategory(promptForm.id, body)
        : api.createPromptCategory(body),
      promptForm.id ? "프롬프트를 수정했습니다." : "프롬프트를 추가했습니다.",
    );
    if (ok) setPromptForm(EMPTY_PROMPT);
  };

  const editPrompt = (category: PromptCategory) => {
    setPromptForm({
      id: category.id,
      name: category.name,
      translation_prompt: category.translation_prompt ?? "",
      review_prompt: category.review_prompt ?? "",
    });
  };

  const submitPathRule = async (event: React.FormEvent) => {
    event.preventDefault();
    const body = {
      source_pattern: pathRuleForm.source_pattern,
      display_pattern: pathRuleForm.display_pattern,
    };
    const ok = await guard(
      () => pathRuleForm.id
        ? api.updatePathDisplayRule(pathRuleForm.id, body)
        : api.createPathDisplayRule(body),
      pathRuleForm.id ? "경로 표시 규칙을 수정했습니다." : "경로 표시 규칙을 추가했습니다.",
    );
    if (ok) setPathRuleForm(EMPTY_PATH_RULE);
  };

  const editPathRule = (rule: PathDisplayRule) => {
    setPathRuleForm({
      id: rule.id,
      source_pattern: rule.source_pattern,
      display_pattern: rule.display_pattern,
    });
  };

  const deletePathRule = async (rule: PathDisplayRule) => {
    if (!window.confirm("이 경로 표시 규칙을 삭제할까요?")) return;
    const ok = await guard(
      () => api.deletePathDisplayRule(rule.id),
      "경로 표시 규칙을 삭제했습니다.",
    );
    if (ok && pathRuleForm.id === rule.id) setPathRuleForm(EMPTY_PATH_RULE);
  };

  const field = "ctl";
  const runtimes = data?.runtimes ?? [];

  return (
    <>
      <header className="topbar">
        <h1>설정</h1>
        <span style={{ marginLeft: "auto" }}>
          <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        </span>
        <button type="button" className="btn sec sm" disabled={refreshing} onClick={() => void refresh()}>
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
            <span className="sub m" title={`${runtimes.length}개 Runtime`}>{runtimes.length}개 Runtime</span>
          </div>
          <div className="card-body flush">
            <div className="tbl" role="table" aria-label="전사 Runtime 목록">
              <div className="tr head" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr) 128px 96px 200px" }}>
                <span role="columnheader">Runtime</span>
                <span role="columnheader">상태</span>
                <span role="columnheader" className="r">작업</span>
                <span role="columnheader" className="r">관리</span>
              </div>
              {runtimes.length === 0 ? (
                <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                  <span role="cell">{status === "loading" ? "불러오는 중" : "등록된 Runtime 없음"}</span>
                </div>
              ) : (
                runtimes.map((runtime: RuntimeEndpoint) => {
                  const parsed = asRuntimeStatus(runtime.status);
                  const tone = parsed ? RUNTIME_STATUS_TONE[parsed] : null;
                  return (
                    <div key={runtime.id} className="tr" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr) 128px 96px 200px" }}>
                      <div className="t-name" role="cell" data-label="Runtime" title={`${runtime.name}\n${runtime.base_url}`}>
                        <b>
                          {runtime.name}
                          {runtime.builtin ? <span className="b line" style={{ marginLeft: 6 }}>기본</span> : null}
                        </b>
                        <span className="m">{runtime.base_url}</span>
                      </div>
                      <span role="cell" data-label="상태" className={tone ? BADGE_CLASS[tone] : "b"} title={runtime.message ?? undefined}>
                        {parsed ? RUNTIME_STATUS_LABEL[parsed] : runtime.status}
                      </span>
                      <span role="cell" data-label="작업" className="r m" style={{ fontSize: ".78rem" }}>
                        {runtime.running_jobs} / {runtime.capacity}
                      </span>
                      <span role="cell" data-label="관리" className="btns" style={{ justifyContent: "flex-end" }}>
                        <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.probeRuntime(runtime.id))}>
                          확인
                        </button>
                        {runtime.builtin ? null : (
                          <>
                            <button
                              type="button"
                              className="btn sec sm"
                              aria-label={`${runtime.name} ${runtime.enabled ? "사용 중지" : "사용"}`}
                              title={runtime.enabled ? "사용 중지" : "사용"}
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
                              aria-label={`${runtime.name} 설정 수정`}
                              title="설정 수정"
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
                              aria-label={`${runtime.name} 삭제`}
                              title="삭제"
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
            <span className="sub m" title={`${(data?.prompt_categories ?? []).length}개`}>{(data?.prompt_categories ?? []).length}개</span>
          </div>
          <div className="card-body flush">
            <div className="tbl" role="table" aria-label="번역 프롬프트 목록">
              {(data?.prompt_categories ?? []).length === 0 ? (
                <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}><span role="cell">등록된 프롬프트 없음</span></div>
              ) : (
                (data?.prompt_categories ?? []).map((category) => (
                  <div key={category.id} className="tr" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr) 220px" }}>
                    <span role="cell" className="t-name" data-label="프롬프트" title={category.name}><b>{category.name}</b></span>
                    <span role="cell" className="btns" data-label="관리" style={{ justifyContent: "flex-end" }}>
                      {category.archived ? <span className="b hold">보관</span> : <span className="b ok">사용</span>}
                      <button type="button" className="btn sec sm" disabled={busy} onClick={() => editPrompt(category)}>
                        <Icon name="pencil" size={13} />수정
                      </button>
                      <button
                        type="button"
                        className="btn sec sm"
                        disabled={busy}
                        onClick={() => void guard(
                          () => api.setPromptCategoryArchived(category.id, !category.archived),
                          category.archived ? "프롬프트를 다시 사용합니다." : "프롬프트를 보관했습니다.",
                        )}
                      >
                        {category.archived ? "복원" : "보관"}
                      </button>
                    </span>
                  </div>
                ))
              )}
            </div>
          </div>
          <div className="card-body">
            <form className="prompt-form" onSubmit={submitPrompt}>
              <label className="f">
                <span className="lb">이름</span>
                <input className="ctl" required maxLength={120} value={promptForm.name} onChange={(event) => setPromptForm({ ...promptForm, name: event.target.value })} />
              </label>
              <label className="f">
                <span className="lb">번역 프롬프트</span>
                <textarea className="ctl textarea" required rows={8} value={promptForm.translation_prompt} onChange={(event) => setPromptForm({ ...promptForm, translation_prompt: event.target.value })} />
              </label>
              <label className="f">
                <span className="lb">검토 프롬프트</span>
                <textarea className="ctl textarea" rows={5} value={promptForm.review_prompt} onChange={(event) => setPromptForm({ ...promptForm, review_prompt: event.target.value })} />
              </label>
              <div className="btns">
                <button type="submit" className="btn" disabled={busy}>{promptForm.id ? "변경 저장" : "프롬프트 추가"}</button>
                {promptForm.id ? <button type="button" className="btn sec" onClick={() => setPromptForm(EMPTY_PROMPT)}>취소</button> : null}
              </div>
            </form>
          </div>
        </section>

        <section className="card path-settings-card" id="path-display-rules" aria-labelledby="path-display-title">
          <div className="card-head">
            <div>
              <h2 id="path-display-title">폴더 경로 단축 표기</h2>
              <span className="sub" title="{이름}은 경로 한 단계를 나타냅니다."><code>{"{이름}"}</code>은 경로 한 단계를 나타냅니다.</span>
            </div>
            <span className="sub m" title={`${(data?.path_display_rules ?? []).length}개 규칙`}>{(data?.path_display_rules ?? []).length}개 규칙</span>
          </div>
          <div className="card-body path-settings-body">
            {(data?.path_display_rules ?? []).length === 0 ? (
              <div className="empty-state compact">
                <strong>등록된 경로 표시 규칙이 없습니다</strong>
                <span>아래에서 첫 번째 단축 규칙을 추가하세요.</span>
              </div>
            ) : (
              <div className="path-rule-list" aria-label="폴더 경로 단축 표기 규칙">
                {(data?.path_display_rules ?? []).map((rule) => (
                  <article className={pathRuleForm.id === rule.id ? "path-rule-item is-editing" : "path-rule-item"} key={rule.id}>
                    <div className="path-rule-mapping">
                      <div className="path-rule-pattern">
                        <span>원본 구조</span>
                        <code>{rule.source_pattern}</code>
                      </div>
                      <span className="path-rule-arrow" aria-hidden><Icon name="chevron_right" size={16} /></span>
                      <div className="path-rule-pattern target">
                        <span>화면 표시</span>
                        <code>{rule.display_pattern}</code>
                      </div>
                    </div>
                    <div className="btns path-rule-actions">
                      <button type="button" className="btn sec sm" disabled={busy} onClick={() => editPathRule(rule)}>
                        <Icon name="pencil" size={13} />수정
                      </button>
                      <button type="button" className="btn dgr sm" disabled={busy} onClick={() => void deletePathRule(rule)}>
                        <Icon name="trash" size={13} />삭제
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}

            <div className="path-rule-editor">
              <div className="path-rule-editor-head">
                <strong>{pathRuleForm.id ? "규칙 수정" : "새 규칙 추가"}</strong>
                <span>원본에서 생략할 경로 단계를 표시 패턴에서 제거하세요.</span>
              </div>
              <form className="path-rule-form" onSubmit={submitPathRule}>
              <label className="f">
                <span className="lb">원본 패턴</span>
                <input
                  className="ctl m"
                  required
                  value={pathRuleForm.source_pattern}
                  onChange={(event) => setPathRuleForm({ ...pathRuleForm, source_pattern: event.target.value })}
                  placeholder="AV/japan/{actress}/{content_id}/{filename}"
                />
              </label>
              <label className="f">
                <span className="lb">표시 패턴</span>
                <input
                  className="ctl m"
                  required
                  value={pathRuleForm.display_pattern}
                  onChange={(event) => setPathRuleForm({ ...pathRuleForm, display_pattern: event.target.value })}
                  placeholder="AV/japan/{actress}/{filename}"
                />
              </label>
              <div className="btns path-rule-editor-actions">
                <button type="submit" className="btn" disabled={busy}>
                  {pathRuleForm.id ? "변경 저장" : "규칙 추가"}
                </button>
                {pathRuleForm.id ? (
                  <button type="button" className="btn sec" disabled={busy} onClick={() => setPathRuleForm(EMPTY_PATH_RULE)}>취소</button>
                ) : null}
              </div>
              </form>
            </div>
          </div>
        </section>
      </div>
    </>
  );
}
