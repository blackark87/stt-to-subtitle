"use client";

import { useCallback, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import {
  api,
  type PathDisplayRule,
  type PromptCategory,
  type RuntimeEndpoint,
  type TranslationServer,
  type TranslationStage,
} from "@/lib/api";
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
  enabled: boolean;
  batch_preferred: boolean;
}
const EMPTY_TRANSLATION_ENDPOINT: TranslationEndpointForm = {
  id: null,
  name: "",
  base_url: "",
  token: "",
  capacity: 1,
  enabled: true,
  batch_preferred: false,
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
  const [translationEditorStage, setTranslationEditorStage] = useState<TranslationStage | null>(null);
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
    if (!translationEditorStage) return;
    const stage = translationEditorStage;
    const group = (data?.translation_groups ?? []).find((item) => item.stage === stage);
    const existing = group?.servers.find((item) => item.id === translationForm.id);
    const ok = await guard(
      async () => {
        const saved = existing
          ? await api.updateTranslationEndpoint(stage, existing.id, {
              name: translationForm.name,
              base_url: translationForm.base_url,
              token: translationForm.token || null,
              enabled: translationForm.enabled,
              capacity: translationForm.capacity,
            })
          : await api.createTranslationEndpoint(stage, {
              name: translationForm.name,
              base_url: translationForm.base_url,
              token: translationForm.token,
              enabled: translationForm.enabled,
              capacity: translationForm.capacity,
            });
        if (
          saved.enabled !== translationForm.enabled
          || saved.batch_preferred !== translationForm.batch_preferred
        ) {
          await api.updateTranslationEndpointRouting(stage, saved.id, {
            enabled: translationForm.enabled,
            batch_preferred: translationForm.batch_preferred,
          });
        }
      },
      existing ? "번역 서버를 수정했습니다." : "번역 서버를 추가했습니다.",
    );
    if (ok) {
      setTranslationForm(EMPTY_TRANSLATION_ENDPOINT);
      setTranslationEditorStage(null);
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

  const editTranslationEndpoint = (stage: TranslationStage, endpoint: TranslationServer) => {
    setTranslationForm({
      id: endpoint.id,
      name: endpoint.name,
      base_url: endpoint.base_url,
      token: "",
      capacity: endpoint.capacity,
      enabled: endpoint.enabled,
      batch_preferred: endpoint.batch_preferred,
    });
    setTranslationEditorStage(stage);
  };

  const updateTranslationRouting = (
    stage: TranslationStage,
    endpoint: TranslationServer,
    update: Partial<Pick<TranslationServer, "enabled" | "batch_preferred">>,
  ) => guard(
    () => api.updateTranslationEndpointRouting(stage, endpoint.id, {
      enabled: update.enabled ?? endpoint.enabled,
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
  const translationGroups = [...(data?.translation_groups ?? [])].sort(
    (left, right) => (left.stage === "draft" ? -1 : right.stage === "draft" ? 1 : 0),
  );
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
            <div><h2>전사 Runtime</h2><span className="sub m">{runtimes.length}개 서버</span></div>
            {runtimeEditorOpen ? <span className="b line">{form.id ? "수정 중" : "추가 중"}</span> : (
              <button type="button" className="btn sec sm" onClick={() => { setForm(EMPTY); setRuntimeEditorOpen(true); }}><Icon name="plus" size={14} />서버 추가</button>
            )}
          </div>
          <div className="card-body flush">
            <div className="tbl settings-runtime-table" role="table" aria-label="전사 Runtime 서버 목록">
              <div className="tr head runtime-grid" role="row">
                <span role="columnheader">Runtime</span><span role="columnheader">상태</span><span role="columnheader" className="r">작업</span><span role="columnheader">Kotoba 배치</span><span role="columnheader">WhisperX 배치</span><span role="columnheader" className="r">관리</span>
              </div>
              {runtimes.length === 0 && !runtimeEditorOpen ? <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}><span role="cell">{status === "loading" ? "불러오는 중" : "등록된 Runtime 없음"}</span></div> : null}
              {runtimes.map((runtime: RuntimeEndpoint) => {
                const parsed = asRuntimeStatus(runtime.status);
                const tone = parsed ? RUNTIME_STATUS_TONE[parsed] : null;
                if (runtimeEditorOpen && form.id === runtime.id) return (
                  <form key={runtime.id} className="tr runtime-grid inline-edit-row" role="row" onSubmit={submitRuntime}>
                    <div role="cell" data-label="Runtime" className="inline-server-fields">
                      {runtime.builtin ? <div className="runtime-title"><strong>{runtime.name}</strong><span className="b line">기본</span></div> : <>
                        <input required maxLength={80} className={field} aria-label="Runtime 이름" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
                        <input required type="url" className={field} aria-label="Runtime API 주소" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} />
                        <input type="password" autoComplete="new-password" className={field} aria-label="Runtime API 토큰" value={form.token} onChange={(event) => setForm({ ...form, token: event.target.value })} placeholder="토큰: 비우면 유지" />
                      </>}
                    </div>
                    <span role="cell" data-label="상태"><span className="b line">수정 중</span></span>
                    <label role="cell" data-label="작업" className="inline-number-field"><input disabled={runtime.builtin} type="number" min={1} max={8} className={field} aria-label="할당 슬롯" value={form.capacity} onChange={(event) => setForm({ ...form, capacity: Number(event.target.value) })} /></label>
                    <label role="cell" data-label="Kotoba 배치" className="inline-number-field"><input type="number" min={1} max={64} className={field} aria-label="Kotoba 배치" value={form.kotoba_batch_size ?? ""} onChange={(event) => setForm({ ...form, kotoba_batch_size: event.target.value ? Number(event.target.value) : null })} placeholder="기본값" /></label>
                    <label role="cell" data-label="WhisperX 배치" className="inline-number-field"><input type="number" min={1} max={64} className={field} aria-label="WhisperX 배치" value={form.whisperx_batch_size ?? ""} onChange={(event) => setForm({ ...form, whisperx_batch_size: event.target.value ? Number(event.target.value) : null })} placeholder="기본값" /></label>
                    <span role="cell" data-label="관리" className="btns runtime-actions"><button type="submit" className="btn sm" disabled={busy}>저장</button><button type="button" className="btn sec sm" onClick={() => { setForm(EMPTY); setRuntimeEditorOpen(false); }}>취소</button></span>
                  </form>
                );
                return <div key={runtime.id} className="tr runtime-grid" role="row">
                  <div className="t-name" role="cell" data-label="Runtime" title={`${runtime.name}\n${runtime.base_url}`}><div className="runtime-title"><strong>{runtime.name}</strong>{runtime.builtin ? <span className="b line">기본</span> : null}</div><span className="m">{runtime.base_url}</span></div>
                  <div role="cell" data-label="상태" className="runtime-status-cell"><span className={tone ? BADGE_CLASS[tone] : "b"} title={runtime.message ?? undefined}>{parsed ? RUNTIME_STATUS_LABEL[parsed] : runtime.status}</span></div>
                  <span role="cell" data-label="작업" className="runtime-count-cell r m">{runtime.running_jobs} / {runtime.capacity}</span>
                  <span role="cell" data-label="Kotoba 배치" className="runtime-batch-cell m">{runtime.kotoba_batch_size ?? "기본값"}</span>
                  <span role="cell" data-label="WhisperX 배치" className="runtime-batch-cell m">{runtime.whisperx_batch_size ?? "기본값"}</span>
                  <span role="cell" data-label="관리" className="btns runtime-actions">
                    <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.probeRuntime(runtime.id))}>확인</button>
                    {!runtime.builtin ? <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.updateRuntime(runtime.id, { name: runtime.name, base_url: runtime.base_url, enabled: !runtime.enabled, capacity: runtime.capacity }))}>{runtime.enabled ? "중지" : "사용"}</button> : null}
                    <button type="button" className="btn sec sm" aria-label={`${runtime.name} 수정`} disabled={busy} onClick={() => editRuntime(runtime)}><Icon name="pencil" size={13} />수정</button>
                    {!runtime.builtin ? <button type="button" className="btn dgr sm" disabled={busy} onClick={() => { if (window.confirm(`${runtime.name} Runtime을 삭제할까요?`)) void guard(() => api.deleteRuntime(runtime.id), "삭제했습니다."); }}><Icon name="trash" size={13} /></button> : null}
                  </span>
                </div>;
              })}
              {runtimeEditorOpen && !form.id ? <form className="tr runtime-grid inline-edit-row" role="row" onSubmit={submitRuntime}>
                <div role="cell" data-label="Runtime" className="inline-server-fields"><input required maxLength={80} className={field} aria-label="Runtime 이름" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Runtime 이름" /><input required type="url" className={field} aria-label="Runtime API 주소" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="http://runtime-host:8100" /><input type="password" autoComplete="new-password" className={field} aria-label="Runtime API 토큰" value={form.token} onChange={(event) => setForm({ ...form, token: event.target.value })} placeholder="API 토큰" /></div>
                <span role="cell" data-label="상태"><span className="b line">추가 중</span></span>
                <label role="cell" data-label="작업" className="inline-number-field"><input type="number" min={1} max={8} className={field} aria-label="할당 슬롯" value={form.capacity} onChange={(event) => setForm({ ...form, capacity: Number(event.target.value) })} /></label>
                <label role="cell" data-label="Kotoba 배치" className="inline-number-field"><input type="number" min={1} max={64} className={field} aria-label="Kotoba 배치" value={form.kotoba_batch_size ?? ""} onChange={(event) => setForm({ ...form, kotoba_batch_size: event.target.value ? Number(event.target.value) : null })} placeholder="기본값" /></label>
                <label role="cell" data-label="WhisperX 배치" className="inline-number-field"><input type="number" min={1} max={64} className={field} aria-label="WhisperX 배치" value={form.whisperx_batch_size ?? ""} onChange={(event) => setForm({ ...form, whisperx_batch_size: event.target.value ? Number(event.target.value) : null })} placeholder="기본값" /></label>
                <span role="cell" data-label="관리" className="btns runtime-actions"><button type="submit" className="btn sm" disabled={busy}>추가</button><button type="button" className="btn sec sm" onClick={() => { setForm(EMPTY); setRuntimeEditorOpen(false); }}>취소</button></span>
              </form> : null}
            </div>
          </div>
        </section>

        {data?.translation_groups_error ? <p role="alert" className="translation-router-error">번역 서버 설정 조회 실패: {data.translation_groups_error}</p> : null}
        {translationGroups.map((group) => {
          const models = Array.from(new Set([group.model, ...group.servers.flatMap((server) => server.models)].filter(Boolean)));
          const editingServer = group.servers.find((server) => server.id === translationForm.id);
          const batchServer = group.servers.find((server) => server.batch_preferred);
          return <section className="card" key={group.stage}>
            <div className="card-head">
              <div><h2>{group.label}</h2><span className="sub m">{group.servers.length}개 서버 · 일괄 처리 서버 {batchServer?.name ?? "미선택"}</span></div>
              {translationEditorStage === group.stage ? <span className="b line">{translationForm.id ? "수정 중" : "추가 중"}</span> : <button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorStage(group.stage); }}><Icon name="plus" size={14} />서버 추가</button>}
            </div>
            <div className="card-body translation-group-model"><label className="f"><span className="lb">모델 선택</span><select className="ctl" value={group.model} disabled={busy} onChange={(event) => void guard(() => api.updateTranslationGroupModel(group.stage, event.target.value), "번역 모델을 저장했습니다.")}>{models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label></div>
            <div className="card-body flush">
              <div className="tbl settings-translation-table" role="table" aria-label={`${group.label} 서버 목록`}>
                <div className="tr head translation-server-grid" role="row"><span role="columnheader">서버</span><span role="columnheader">상태</span><span role="columnheader" className="r">요청</span><span role="columnheader">사용</span><span role="columnheader">일괄 처리 서버</span><span role="columnheader" className="r">관리</span></div>
                {group.servers.map((server) => {
                  if (translationEditorStage === group.stage && translationForm.id === server.id) return <form key={server.id} className="tr translation-server-grid inline-edit-row" role="row" onSubmit={submitTranslationEndpoint}>
                    <div role="cell" data-label="서버" className="inline-server-fields"><input required maxLength={80} className={field} aria-label="서버 이름" value={translationForm.name} onChange={(event) => setTranslationForm({ ...translationForm, name: event.target.value })} /><input required type="url" className={field} aria-label="API 주소" value={translationForm.base_url} onChange={(event) => setTranslationForm({ ...translationForm, base_url: event.target.value })} /><input type="password" autoComplete="new-password" className={field} aria-label="API 토큰" value={translationForm.token} onChange={(event) => setTranslationForm({ ...translationForm, token: event.target.value })} placeholder={editingServer?.token_configured ? "토큰: 비우면 유지" : "API 토큰"} /></div>
                    <span role="cell" data-label="상태"><span className="b line">수정 중</span></span>
                    <label role="cell" data-label="요청" className="inline-number-field"><input type="number" min={1} max={8} className={field} aria-label="동시 요청" value={translationForm.capacity} onChange={(event) => setTranslationForm({ ...translationForm, capacity: Number(event.target.value) })} /></label>
                    <label role="cell" data-label="사용" className="translation-toggle"><input type="checkbox" checked={translationForm.enabled} onChange={(event) => setTranslationForm({ ...translationForm, enabled: event.target.checked })} /><span>{translationForm.enabled ? "ON" : "OFF"}</span></label>
                    <span role="cell" data-label="일괄 처리 서버" className="m">{translationForm.batch_preferred ? "선택됨" : "미선택"}</span>
                    <span role="cell" data-label="관리" className="btns translation-server-actions"><button type="submit" className="btn sm" disabled={busy}>저장</button><button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorStage(null); }}>취소</button></span>
                  </form>;
                  const statusLabel = server.status === "ready" ? "사용 가능" : server.status === "disabled" ? "사용 안 함" : server.status === "unconfigured" ? "주소 미설정" : server.status === "unavailable" ? "연결 실패" : "확인 전";
                  return <div key={server.id} className="tr translation-server-grid" role="row">
                    <div className="t-name" role="cell" data-label="서버" title={`${server.name}\n${server.base_url}`}><div className="runtime-title"><strong>{server.name}</strong>{server.builtin ? <span className="b line">기본</span> : null}</div><span className="m">{server.base_url || "API 주소 미설정"}</span></div>
                    <span role="cell" data-label="상태"><span className={server.status === "ready" ? "b ok dot" : server.status === "unavailable" ? "b bad dot" : "b wait dot"}>{statusLabel}</span></span>
                    <span role="cell" data-label="요청" className="r m">{server.running_jobs} / {server.capacity}</span>
                    <label role="cell" data-label="사용" className="translation-toggle"><input type="checkbox" checked={server.enabled} disabled={busy} onChange={(event) => void updateTranslationRouting(group.stage, server, { enabled: event.target.checked })} /><span>{server.enabled ? "ON" : "OFF"}</span></label>
                    <label role="cell" data-label="일괄 처리 서버" className="translation-toggle"><input type="radio" name={`batch-server-${group.stage}`} checked={server.batch_preferred} disabled={busy || !server.enabled} onChange={() => void updateTranslationRouting(group.stage, server, { batch_preferred: true })} /><span>{server.batch_preferred ? "선택됨" : "선택"}</span></label>
                    <span role="cell" data-label="관리" className="btns translation-server-actions"><button type="button" className="btn sec sm" disabled={busy || !server.base_url} onClick={() => void guard(() => api.probeTranslationEndpoint(group.stage, server.id), "모델 목록을 갱신했습니다.")}>확인</button><button type="button" className="btn sec sm" disabled={busy} onClick={() => editTranslationEndpoint(group.stage, server)}><Icon name="pencil" size={13} />수정</button>{!server.builtin ? <button type="button" className="btn dgr sm" disabled={busy} onClick={() => { if (window.confirm(`${group.label}의 ${server.name} 서버를 삭제할까요?`)) void guard(() => api.deleteTranslationEndpoint(group.stage, server.id), "번역 서버를 삭제했습니다."); }}><Icon name="trash" size={13} /></button> : null}</span>
                  </div>;
                })}
                {translationEditorStage === group.stage && !translationForm.id ? <form className="tr translation-server-grid inline-edit-row" role="row" onSubmit={submitTranslationEndpoint}>
                  <div role="cell" data-label="서버" className="inline-server-fields"><input required maxLength={80} className={field} aria-label="서버 이름" value={translationForm.name} onChange={(event) => setTranslationForm({ ...translationForm, name: event.target.value })} placeholder="서버 이름" /><input required type="url" className={field} aria-label="API 주소" value={translationForm.base_url} onChange={(event) => setTranslationForm({ ...translationForm, base_url: event.target.value })} placeholder="http://model-server:1234/v1" /><input type="password" autoComplete="new-password" className={field} aria-label="API 토큰" value={translationForm.token} onChange={(event) => setTranslationForm({ ...translationForm, token: event.target.value })} placeholder="API 토큰" /></div>
                  <span role="cell" data-label="상태"><span className="b line">추가 중</span></span>
                  <label role="cell" data-label="요청" className="inline-number-field"><input type="number" min={1} max={8} className={field} aria-label="동시 요청" value={translationForm.capacity} onChange={(event) => setTranslationForm({ ...translationForm, capacity: Number(event.target.value) })} /></label>
                  <label role="cell" data-label="사용" className="translation-toggle"><input type="checkbox" checked={translationForm.enabled} onChange={(event) => setTranslationForm({ ...translationForm, enabled: event.target.checked })} /><span>{translationForm.enabled ? "ON" : "OFF"}</span></label>
                  <span role="cell" data-label="일괄 처리 서버" className="m">저장 후 선택</span>
                  <span role="cell" data-label="관리" className="btns translation-server-actions"><button type="submit" className="btn sm" disabled={busy}>추가</button><button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorStage(null); }}>취소</button></span>
                </form> : null}
              </div>
            </div>
          </section>;
        })}

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
