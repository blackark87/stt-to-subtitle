"use client";

import { useCallback, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { api, type PathDisplayRule, type PromptCategory, type RuntimeEndpoint, type TranslationEndpoint } from "@/lib/api";
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
  kotoba_batch_size: number | null;
  whisperx_batch_size: number | null;
}
const EMPTY: RuntimeForm = {
  id: null,
  name: "",
  base_url: "",
  token: "",
  capacity: 1,
  kotoba_batch_size: null,
  whisperx_batch_size: null,
};

interface TranslationEndpointForm {
  id: string | null;
  name: string;
  base_url: string;
  token: string;
  capacity: number;
}
const EMPTY_TRANSLATION_ENDPOINT: TranslationEndpointForm = {
  id: null,
  name: "",
  base_url: "",
  token: "",
  capacity: 1,
};

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
  const [translationForm, setTranslationForm] = useState<TranslationEndpointForm>(EMPTY_TRANSLATION_ENDPOINT);
  const [translationEditorOpen, setTranslationEditorOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [runtimeEditorOpen, setRuntimeEditorOpen] = useState(false);
  const [promptForm, setPromptForm] = useState<PromptForm>(EMPTY_PROMPT);
  const [promptEditorOpen, setPromptEditorOpen] = useState(false);
  const [pathRuleForm, setPathRuleForm] = useState<PathRuleForm>(EMPTY_PATH_RULE);
  const [pathRuleEditorOpen, setPathRuleEditorOpen] = useState(false);

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
              kotoba_batch_size: form.kotoba_batch_size,
              whisperx_batch_size: form.whisperx_batch_size,
              clear_kotoba_batch_size: form.kotoba_batch_size == null,
              clear_whisperx_batch_size: form.whisperx_batch_size == null,
            })
          : api.createRuntime({
              name: form.name,
              base_url: form.base_url,
              token: form.token,
              enabled: true,
              capacity: form.capacity,
              kotoba_batch_size: form.kotoba_batch_size,
              whisperx_batch_size: form.whisperx_batch_size,
            }),
      existing ? "Runtime을 수정했습니다." : "Runtime을 추가했습니다.",
    );
    if (ok) {
      setForm(EMPTY);
      setRuntimeEditorOpen(false);
    }
  };

  const submitTranslationEndpoint = async (event: React.FormEvent) => {
    event.preventDefault();
    const existing = (data?.translation_endpoints ?? []).find((item) => item.id === translationForm.id);
    const ok = await guard(
      () =>
        existing
          ? api.updateTranslationEndpoint(existing.id, {
              name: translationForm.name,
              base_url: translationForm.base_url,
              token: translationForm.token || null,
              enabled: existing.enabled,
              capacity: translationForm.capacity,
            })
          : api.createTranslationEndpoint({
              name: translationForm.name,
              base_url: translationForm.base_url,
              token: translationForm.token,
              enabled: true,
              capacity: translationForm.capacity,
            }),
      existing ? "번역 서버를 수정했습니다." : "번역 서버를 추가했습니다.",
    );
    if (ok) {
      setTranslationForm(EMPTY_TRANSLATION_ENDPOINT);
      setTranslationEditorOpen(false);
    }
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
    if (ok) {
      setPromptForm(EMPTY_PROMPT);
      setPromptEditorOpen(false);
    }
  };

  const editRuntime = (runtime: RuntimeEndpoint) => {
    setForm({
      id: runtime.id,
      name: runtime.name,
      base_url: runtime.base_url,
      token: "",
      capacity: runtime.capacity,
      kotoba_batch_size: runtime.kotoba_batch_size,
      whisperx_batch_size: runtime.whisperx_batch_size,
    });
    setRuntimeEditorOpen(true);
  };

  const editTranslationEndpoint = (endpoint: TranslationEndpoint) => {
    setTranslationForm({
      id: endpoint.id,
      name: endpoint.name,
      base_url: endpoint.base_url,
      token: "",
      capacity: endpoint.capacity,
    });
    setTranslationEditorOpen(true);
  };

  const updateTranslationRouting = (
    endpoint: TranslationEndpoint,
    update: Partial<Pick<TranslationEndpoint, "draft_model" | "review_model" | "review_enabled" | "batch_preferred">>,
  ) => guard(
    () => api.updateTranslationEndpointRouting(endpoint.id, {
      draft_model: update.draft_model ?? endpoint.draft_model,
      review_model: update.review_model ?? endpoint.review_model,
      review_enabled: update.review_enabled ?? endpoint.review_enabled,
      batch_preferred: update.batch_preferred ?? endpoint.batch_preferred,
    }),
    "번역 라우팅 설정을 저장했습니다.",
  );

  const editPrompt = (category: PromptCategory) => {
    setPromptForm({
      id: category.id,
      name: category.name,
      translation_prompt: category.translation_prompt ?? "",
      review_prompt: category.review_prompt ?? "",
    });
    setPromptEditorOpen(true);
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
    if (ok) {
      setPathRuleForm(EMPTY_PATH_RULE);
      setPathRuleEditorOpen(false);
    }
  };

  const editPathRule = (rule: PathDisplayRule) => {
    setPathRuleForm({
      id: rule.id,
      source_pattern: rule.source_pattern,
      display_pattern: rule.display_pattern,
    });
    setPathRuleEditorOpen(true);
  };

  const deletePathRule = async (rule: PathDisplayRule) => {
    if (!window.confirm("이 경로 표시 규칙을 삭제할까요?")) return;
    const ok = await guard(
      () => api.deletePathDisplayRule(rule.id),
      "경로 표시 규칙을 삭제했습니다.",
    );
    if (ok && pathRuleForm.id === rule.id) {
      setPathRuleForm(EMPTY_PATH_RULE);
      setPathRuleEditorOpen(false);
    }
  };

  const field = "ctl";
  const runtimes = data?.runtimes ?? [];
  const translationEndpoints = data?.translation_endpoints ?? [];
  const editingRuntime = runtimes.find((item) => item.id === form.id);
  const editingTranslationEndpoint = translationEndpoints.find((item) => item.id === translationForm.id);

  return (
    <>
      <header className="topbar">
        <div className="page-title"><h1>설정</h1><p>전사·번역 서버와 화면 표시 규칙을 관리합니다.</p></div>
        <span className="topbar-spacer" />
        <Freshness status={status} updatedAt={updatedAt} error={error} refreshing={refreshing} />
        <button type="button" className="btn sec sm" disabled={refreshing} onClick={() => void refresh()}>
          <Icon name="refresh" size={14} />
          새로고침
        </button>
      </header>

      <div className="content settings-content">
        {notice ? (
          <p role="status" style={{ margin: 0, color: "var(--ok)", fontSize: ".82rem" }}>{notice}</p>
        ) : null}
        {actionError ? (
          <p role="alert" style={{ margin: 0, color: "var(--bad)", fontSize: ".82rem" }}>{actionError}</p>
        ) : null}

        <section className="card">
          <div className="card-head">
            <div><h2>전사 서버</h2><span className="sub m" title={`${runtimes.length}개 Runtime`}>{runtimes.length}개 Runtime</span></div>
            {runtimeEditorOpen ? (
              <span className="b line">{form.id ? "수정 중" : "추가 중"}</span>
            ) : (
              <button type="button" className="btn sec sm" aria-expanded="false" aria-controls="runtime-editor" onClick={() => { setForm(EMPTY); setRuntimeEditorOpen(true); }}>
                <Icon name="plus" size={14} />Runtime 추가
              </button>
            )}
          </div>
          <div className="card-body flush">
            <div className="tbl settings-runtime-table" role="table" aria-label="전사 Runtime 목록">
              <div className="tr head runtime-grid" role="row">
                <span role="columnheader">Runtime</span>
                <span role="columnheader">상태</span>
                <span role="columnheader" className="r">작업</span>
                <span role="columnheader">배치</span>
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
                    <div key={runtime.id} className="tr runtime-grid" role="row">
                      <div className="t-name" role="cell" data-label="Runtime" title={`${runtime.name}\n${runtime.base_url}`}>
                        <div className="runtime-title">
                          <strong>{runtime.name}</strong>
                          {runtime.builtin ? <span className="b line">기본</span> : null}
                        </div>
                        <span className="m">{runtime.base_url}</span>
                      </div>
                      <div role="cell" data-label="상태" className="runtime-status-cell">
                        <span className={tone ? BADGE_CLASS[tone] : "b"} title={runtime.message ?? undefined}>
                          {parsed ? RUNTIME_STATUS_LABEL[parsed] : runtime.status}
                        </span>
                      </div>
                      <span role="cell" data-label="작업" className="runtime-count-cell r m">
                        {runtime.running_jobs} / {runtime.capacity}
                      </span>
                      <span role="cell" data-label="배치" className="runtime-batch-cell m">
                        Kotoba 배치 {runtime.kotoba_batch_size ?? "기본값"} · WhisperX 배치 {runtime.whisperx_batch_size ?? "기본값"}
                      </span>
                      <span role="cell" data-label="관리" className="btns runtime-actions">
                        <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.probeRuntime(runtime.id))}>
                          확인
                        </button>
                        {!runtime.builtin ? (
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
                              onClick={() => editRuntime(runtime)}
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
                        ) : (
                          <button
                            type="button"
                            className="btn sec sm"
                            aria-label={`${runtime.name} 배치 설정 수정`}
                            title="배치 설정 수정"
                            disabled={busy}
                            onClick={() => editRuntime(runtime)}
                          >
                            <Icon name="pencil" size={13} />
                          </button>
                        )}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>
          {runtimeEditorOpen ? <div className="card-body settings-editor" id="runtime-editor">
            <div className="settings-editor-head"><strong>{form.id ? "Runtime 수정" : "새 Runtime 추가"}</strong><span>{editingRuntime?.builtin ? "기본 Runtime은 배치 크기만 변경할 수 있습니다." : form.id ? "비밀번호를 비우면 기존 API 토큰을 유지합니다." : "전사 서버 연결 정보와 할당 슬롯을 입력하세요."}</span></div>
            <form onSubmit={submitRuntime} className="fg settings-form-grid">
              <label className="f">
                <span className="lb">이름</span>
                <input required disabled={editingRuntime?.builtin} maxLength={80} className={field} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="GPU Runtime 02" />
              </label>
              <label className="f">
                <span className="lb">API 주소</span>
                <input required disabled={editingRuntime?.builtin} type="url" className={field} value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} placeholder="http://runtime-host:8100" />
              </label>
              <label className="f">
                <span className="lb">API 토큰</span>
                <input disabled={editingRuntime?.builtin} type="password" autoComplete="new-password" className={field} value={form.token} onChange={(e) => setForm({ ...form, token: e.target.value })} placeholder={form.id ? "비우면 유지" : ""} />
              </label>
              <label className="f">
                <span className="lb">할당 슬롯</span>
                <input disabled={editingRuntime?.builtin} type="number" min={1} max={8} className={field} value={form.capacity} onChange={(e) => setForm({ ...form, capacity: Number(e.target.value) })} />
              </label>
              <label className="f">
                <span className="lb">Kotoba 배치</span>
                <input type="number" min={1} max={64} className={field} value={form.kotoba_batch_size ?? ""} onChange={(e) => setForm({ ...form, kotoba_batch_size: e.target.value ? Number(e.target.value) : null })} placeholder="Runtime 기본값" />
              </label>
              <label className="f">
                <span className="lb">WhisperX 배치</span>
                <input type="number" min={1} max={64} className={field} value={form.whisperx_batch_size ?? ""} onChange={(e) => setForm({ ...form, whisperx_batch_size: e.target.value ? Number(e.target.value) : null })} placeholder="Runtime 기본값" />
              </label>
              <div className="w btns">
                <button type="submit" className="btn sm" disabled={busy}>
                  {form.id ? "변경 저장" : "Runtime 추가"}
                </button>
                <button type="button" className="btn sec sm" onClick={() => { setForm(EMPTY); setRuntimeEditorOpen(false); }}>취소</button>
              </div>
            </form>
          </div> : null}
        </section>

        <section className="card">
          <div className="card-head">
            <div><h2>번역 서버</h2><span className="sub m">{translationEndpoints.length}개 서버</span></div>
            {translationEditorOpen ? (
              <span className="b line">{translationForm.id ? "수정 중" : "추가 중"}</span>
            ) : (
              <button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorOpen(true); }}>
                <Icon name="plus" size={14} />서버 추가
              </button>
            )}
          </div>
          <div className="card-body flush">
            {data?.translation_router_error ? (
              <p role="alert" className="translation-router-error">번역 라우터 연결 실패: {data.translation_router_error}</p>
            ) : null}
            <div className="tbl settings-translation-table" role="table" aria-label="번역 서버 목록">
              <div className="tr head translation-server-grid" role="row">
                <span role="columnheader">서버</span>
                <span role="columnheader">상태</span>
                <span role="columnheader" className="r">요청</span>
                <span role="columnheader" className="r">관리</span>
              </div>
              {translationEndpoints.length === 0 ? (
                <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}>
                  <span role="cell">등록된 번역 서버 없음</span>
                </div>
              ) : translationEndpoints.map((endpoint) => (
                <div key={endpoint.id} className="tr translation-server-grid" role="row">
                  <div className="t-name" role="cell" data-label="서버" title={`${endpoint.name}\n${endpoint.base_url}`}>
                    <div className="runtime-title"><strong>{endpoint.name}</strong>{endpoint.builtin ? <span className="b line">기본</span> : null}</div>
                    <span className="m">{endpoint.base_url}</span>
                  </div>
                  <span role="cell" data-label="상태"><span className={endpoint.status === "ready" ? "b ok dot" : endpoint.status === "unavailable" ? "b bad dot" : "b wait dot"}>{endpoint.status}</span></span>
                  <span role="cell" data-label="요청" className="r m">{endpoint.running_jobs} / {endpoint.capacity}</span>
                  <span role="cell" data-label="관리" className="btns translation-server-actions">
                    <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.probeTranslationEndpoint(endpoint.id), "모델 목록을 갱신했습니다.")}>확인</button>
                    {!endpoint.builtin ? (
                      <>
                        <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.updateTranslationEndpoint(endpoint.id, { name: endpoint.name, base_url: endpoint.base_url, enabled: !endpoint.enabled, capacity: endpoint.capacity }))}>{endpoint.enabled ? "중지" : "사용"}</button>
                        <button type="button" className="btn sec sm" disabled={busy} onClick={() => editTranslationEndpoint(endpoint)}><Icon name="pencil" size={13} />수정</button>
                        <button type="button" className="btn dgr sm" disabled={busy} onClick={() => { if (window.confirm(`${endpoint.name} 번역 서버를 삭제할까요?`)) void guard(() => api.deleteTranslationEndpoint(endpoint.id), "번역 서버를 삭제했습니다."); }}><Icon name="trash" size={13} /></button>
                      </>
                    ) : null}
                  </span>
                </div>
              ))}
            </div>
          </div>
          {translationEditorOpen ? (
            <div className="card-body settings-editor">
              <div className="settings-editor-head"><strong>{translationForm.id ? "번역 서버 수정" : "새 번역 서버 추가"}</strong><span>{editingTranslationEndpoint ? "토큰을 비우면 기존 값을 유지합니다." : "OpenAI 호환 서버 연결 정보를 입력하세요."}</span></div>
              <form onSubmit={submitTranslationEndpoint} className="fg settings-form-grid">
                <label className="f"><span className="lb">이름</span><input required maxLength={80} className={field} value={translationForm.name} onChange={(event) => setTranslationForm({ ...translationForm, name: event.target.value })} /></label>
                <label className="f"><span className="lb">API 주소</span><input required type="url" className={field} value={translationForm.base_url} onChange={(event) => setTranslationForm({ ...translationForm, base_url: event.target.value })} placeholder="http://model-server:1234/v1" /></label>
                <label className="f"><span className="lb">API 토큰</span><input type="password" autoComplete="new-password" className={field} value={translationForm.token} onChange={(event) => setTranslationForm({ ...translationForm, token: event.target.value })} placeholder={editingTranslationEndpoint?.token_configured ? "비우면 유지" : ""} /></label>
                <label className="f"><span className="lb">동시 요청</span><input type="number" min={1} max={8} className={field} value={translationForm.capacity} onChange={(event) => setTranslationForm({ ...translationForm, capacity: Number(event.target.value) })} /></label>
                <div className="w btns"><button type="submit" className="btn sm" disabled={busy}>{translationForm.id ? "변경 저장" : "서버 추가"}</button><button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorOpen(false); }}>취소</button></div>
              </form>
            </div>
          ) : null}
        </section>

        <div className="translation-stage-settings" aria-label="번역 단계 설정">
          <section className="card">
            <div className="card-head"><div><h2>초벌 번역</h2><span className="sub m">가용 서버 중 여유 슬롯을 사용하며, 일괄 작업은 우선 서버를 먼저 사용합니다.</span></div></div>
            <div className="card-body translation-model-list">
              {translationEndpoints.map((endpoint) => {
                const models = Array.from(new Set([endpoint.draft_model, ...endpoint.models].filter(Boolean)));
                return <div className="translation-model-row" key={`draft-${endpoint.id}`}>
                  <div className="runtime-title"><strong>{endpoint.name}</strong>{endpoint.builtin ? <span className="b line">기본</span> : null}</div>
                  <label className="f"><span className="lb">모델 선택</span><select className="ctl" value={endpoint.draft_model} disabled={busy || !endpoint.enabled} onChange={(event) => void updateTranslationRouting(endpoint, { draft_model: event.target.value })}><option value="">사용 안 함</option>{models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
                  <label className="translation-toggle"><input type="checkbox" checked={endpoint.batch_preferred} disabled={busy || !endpoint.enabled} onChange={(event) => void updateTranslationRouting(endpoint, { batch_preferred: event.target.checked })} /><span>일괄 처리 우선</span></label>
                </div>;
              })}
            </div>
          </section>

          <section className="card">
            <div className="card-head"><div><h2>검증 번역</h2><span className="sub m">서버별 사용 여부를 명시적으로 켠 경우에만 검증 모델을 호출합니다.</span></div></div>
            <div className="card-body translation-model-list">
              {translationEndpoints.map((endpoint) => {
                const models = Array.from(new Set([endpoint.review_model, ...endpoint.models].filter(Boolean)));
                return <div className="translation-model-row" key={`review-${endpoint.id}`}>
                  <div className="runtime-title"><strong>{endpoint.name}</strong>{endpoint.builtin ? <span className="b line">기본</span> : null}</div>
                  <label className="f"><span className="lb">모델 선택</span><select className="ctl" value={endpoint.review_model} disabled={busy || !endpoint.enabled} onChange={(event) => void updateTranslationRouting(endpoint, { review_model: event.target.value })}><option value="">선택 안 함</option>{models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
                  <label className="translation-toggle"><input type="checkbox" checked={endpoint.review_enabled} disabled={busy || !endpoint.enabled} onChange={(event) => void updateTranslationRouting(endpoint, { review_enabled: event.target.checked })} /><span>검증 서버 사용</span></label>
                </div>;
              })}
            </div>
          </section>
        </div>

        <section className="card">
          <div className="card-head">
            <div><h2>번역 프롬프트</h2><span className="sub m" title={`${(data?.prompt_categories ?? []).length}개`}>{(data?.prompt_categories ?? []).length}개</span></div>
            {promptEditorOpen ? (
              <span className="b line">{promptForm.id ? "수정 중" : "추가 중"}</span>
            ) : (
              <button type="button" className="btn sec sm" aria-expanded="false" aria-controls="prompt-editor" onClick={() => { setPromptForm(EMPTY_PROMPT); setPromptEditorOpen(true); }}>
                <Icon name="plus" size={14} />프롬프트 추가
              </button>
            )}
          </div>
          <div className="card-body flush">
            <div className="tbl settings-prompt-table" role="table" aria-label="번역 프롬프트 목록">
              {(data?.prompt_categories ?? []).length === 0 ? (
                <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}><span role="cell">등록된 프롬프트 없음</span></div>
              ) : (
                (data?.prompt_categories ?? []).map((category) => (
                  <div key={category.id} className="tr settings-prompt-row" role="row">
                    <span role="cell" className="t-name" data-label="프롬프트" title={category.name}><b>{category.name}</b></span>
                    <span role="cell" className="btns settings-prompt-actions" data-label="관리">
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
          {promptEditorOpen ? <div className="card-body settings-editor" id="prompt-editor">
            <div className="settings-editor-head"><strong>{promptForm.id ? "프롬프트 수정" : "새 프롬프트 추가"}</strong><span>초벌 번역과 검증 단계에서 사용할 지시문을 관리합니다.</span></div>
            <form className="prompt-form settings-prompt-form" onSubmit={submitPrompt}>
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
                <button type="button" className="btn sec" onClick={() => { setPromptForm(EMPTY_PROMPT); setPromptEditorOpen(false); }}>취소</button>
              </div>
            </form>
          </div> : null}
        </section>

        <section className="card path-settings-card" id="path-display-rules" aria-labelledby="path-display-title">
          <div className="card-head">
            <div>
              <h2 id="path-display-title">폴더 경로 단축 표기</h2>
              <span className="sub" title={`${(data?.path_display_rules ?? []).length}개 규칙 · {이름}은 경로 한 단계를 나타냅니다.`}>
                {(data?.path_display_rules ?? []).length}개 규칙 · <code>{"{이름}"}</code>은 경로 한 단계를 나타냅니다.
              </span>
            </div>
            {pathRuleEditorOpen ? (
              <span className="b line">{pathRuleForm.id ? "수정 중" : "추가 중"}</span>
            ) : (
              <button
                type="button"
                className="btn sec sm"
                aria-expanded="false"
                aria-controls="path-rule-editor"
                onClick={() => {
                  setPathRuleForm(EMPTY_PATH_RULE);
                  setPathRuleEditorOpen(true);
                }}
              >
                <Icon name="plus" size={14} />규칙 추가
              </button>
            )}
          </div>
          <div className="card-body path-settings-body">
            {(data?.path_display_rules ?? []).length === 0 ? (
              <div className="empty-state compact">
                <strong>등록된 경로 표시 규칙이 없습니다</strong>
                <span>위의 규칙 추가 버튼으로 첫 번째 단축 규칙을 등록하세요.</span>
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
          </div>
          {pathRuleEditorOpen ? (
            <div className="card-body settings-editor" id="path-rule-editor">
              <div className="settings-editor-head">
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
                <button
                  type="button"
                  className="btn sec"
                  disabled={busy}
                  onClick={() => {
                    setPathRuleForm(EMPTY_PATH_RULE);
                    setPathRuleEditorOpen(false);
                  }}
                >
                  취소
                </button>
              </div>
              </form>
            </div>
          ) : null}
        </section>
      </div>
    </>
  );
}
