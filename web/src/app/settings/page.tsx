"use client";

import { useCallback, useState } from "react";
import { Icon } from "@/components/Icon";
import { Freshness } from "@/components/Freshness";
import { LoadingOverlay } from "@/components/LoadingOverlay";
import {
  api,
  type ExternalModelProfile,
  type ExternalModelProvider,
  type PathDisplayRule,
  type PromptCategory,
  type TranscriberEndpoint,
  type TranslationServer,
  type TranslationStage,
} from "@/lib/api";
import { RUNTIME_STATUS_LABEL, RUNTIME_STATUS_TONE, asRuntimeStatus, type JobState } from "@/lib/domain";
import { useLiveQuery } from "@/lib/useLiveQuery";

/* design/mockup body_Settings.html 구조: 전사 서버 / 번역 서버 / 번역 프롬프트. */

const SETTINGS_INTERVAL_MS = 15000;

const PROMPT_RUN_STATUS_LABEL = {
  queued: "대기",
  running: "실행 중",
  ready: "검토 대기",
  failed: "실패",
  cancelled: "취소",
  rejected: "거절",
  activated: "활성화",
} as const;

const EXTERNAL_PROVIDER_LABEL: Record<ExternalModelProvider, string> = {
  openrouter: "OpenRouter",
  bedrock: "AWS Bedrock",
  nvidia_build: "NVIDIA Build",
};

type PromptStage = "translation" | "review";

const PROMPT_STAGE_LABEL: Record<PromptStage, string> = {
  translation: "1차 번역",
  review: "2차 검토",
};

const BADGE_CLASS: Record<JobState, string> = {
  running: "b run dot",
  waiting: "b wait dot",
  paused: "b hold dot",
  blocked: "b hold dot",
  stopped: "b hold dot",
  failed: "b bad dot",
  done: "b ok dot",
};

interface TranscriberForm {
  id: string | null;
  name: string;
  base_url: string;
  token: string;
  capacity: number;
  resource_group_id: string;
  kotoba_batch_size: number | null;
  whisperx_batch_size: number | null;
}
const EMPTY: TranscriberForm = {
  id: null,
  name: "",
  base_url: "",
  token: "",
  capacity: 1,
  resource_group_id: "local-gpu",
  kotoba_batch_size: null,
  whisperx_batch_size: null,
};

interface TranslationEndpointForm {
  id: string | null;
  name: string;
  base_url: string;
  token: string;
  capacity: number;
  resource_group_id: string;
  enabled: boolean;
  thinking_enabled: boolean;
  batch_preferred: boolean;
}
const EMPTY_TRANSLATION_ENDPOINT: TranslationEndpointForm = {
  id: null,
  name: "",
  base_url: "",
  token: "",
  capacity: 1,
  resource_group_id: "local-gpu",
  enabled: true,
  thinking_enabled: false,
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

  const [form, setForm] = useState<TranscriberForm>(EMPTY);
  const [translationForm, setTranslationForm] = useState<TranslationEndpointForm>(EMPTY_TRANSLATION_ENDPOINT);
  const [translationEditorStage, setTranslationEditorStage] = useState<TranslationStage | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [runtimeEditorOpen, setRuntimeEditorOpen] = useState(false);
  const [promptForm, setPromptForm] = useState<PromptForm>(EMPTY_PROMPT);
  const [promptEditorOpen, setPromptEditorOpen] = useState(false);
  const [promptDomainDescription, setPromptDomainDescription] = useState("");
  const [promptDraftProvider, setPromptDraftProvider] = useState<ExternalModelProvider | "">("");
  const [promptDraftSummary, setPromptDraftSummary] = useState("");
  const [feedbackCategoryId, setFeedbackCategoryId] = useState("");
  const [feedbackStage, setFeedbackStage] = useState<PromptStage>("translation");
  const [improvementProvider, setImprovementProvider] = useState<ExternalModelProvider | "">("");
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

  const submitTranscriber = async (event: React.FormEvent) => {
    event.preventDefault();
    const existing = (data?.transcribers ?? []).find((item) => item.id === form.id);
    const ok = await guard(
      () =>
        existing
          ? api.updateTranscriber(existing.id, {
              name: form.name,
              base_url: form.base_url,
              token: form.token || null,
              enabled: existing.enabled,
              capacity: form.capacity,
              resource_group_id: form.resource_group_id,
              kotoba_batch_size: form.kotoba_batch_size,
              whisperx_batch_size: form.whisperx_batch_size,
              clear_kotoba_batch_size: form.kotoba_batch_size == null,
              clear_whisperx_batch_size: form.whisperx_batch_size == null,
            })
          : api.createTranscriber({
              name: form.name,
              base_url: form.base_url,
              token: form.token,
              enabled: true,
              capacity: form.capacity,
              resource_group_id: form.resource_group_id,
              kotoba_batch_size: form.kotoba_batch_size,
              whisperx_batch_size: form.whisperx_batch_size,
            }),
      existing ? "전사 모델 서버를 수정했습니다." : "전사 모델 서버를 추가했습니다.",
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
              resource_group_id: translationForm.resource_group_id,
              thinking_enabled: translationForm.thinking_enabled,
            })
          : await api.createTranslationEndpoint(stage, {
              name: translationForm.name,
              base_url: translationForm.base_url,
              token: translationForm.token,
              enabled: translationForm.enabled,
              capacity: translationForm.capacity,
              resource_group_id: translationForm.resource_group_id,
              thinking_enabled: translationForm.thinking_enabled,
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
      setPromptDomainDescription("");
      setPromptDraftSummary("");
      setPromptEditorOpen(false);
    }
  };

  const editRuntime = (runtime: TranscriberEndpoint) => {
    setForm({
      id: runtime.id,
      name: runtime.name,
      base_url: runtime.base_url,
      token: "",
      capacity: runtime.capacity,
      resource_group_id: runtime.resource_group_id,
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
      resource_group_id: endpoint.resource_group_id,
      enabled: endpoint.enabled,
      thinking_enabled: endpoint.thinking_enabled,
      batch_preferred: endpoint.batch_preferred,
    });
    setTranslationEditorStage(stage);
  };

  const updateTranslationRouting = (
    stage: TranslationStage,
    endpoint: TranslationServer,
    update: Partial<Pick<TranslationServer, "enabled" | "batch_preferred">>,
  ) => {
    const enabled = update.enabled ?? endpoint.enabled;
    return guard(
      () => api.updateTranslationEndpointRouting(stage, endpoint.id, {
        enabled,
        batch_preferred: enabled
          && (update.batch_preferred ?? endpoint.batch_preferred),
      }),
      "번역 라우팅 설정을 저장했습니다.",
    );
  };

  const updateTranslationThinking = (
    stage: TranslationStage,
    endpoint: TranslationServer,
    thinkingEnabled: boolean,
  ) => guard(
    () => api.updateTranslationEndpoint(stage, endpoint.id, {
      name: endpoint.name,
      base_url: endpoint.base_url,
      enabled: endpoint.enabled,
      capacity: endpoint.capacity,
      resource_group_id: endpoint.resource_group_id,
      thinking_enabled: thinkingEnabled,
    }),
    `Thinking을 ${thinkingEnabled ? "사용" : "미사용"}으로 저장했습니다.`,
  );

  const editPrompt = (category: PromptCategory) => {
    setPromptForm({
      id: category.id,
      name: category.name,
      translation_prompt: category.translation_prompt ?? "",
      review_prompt: category.review_prompt ?? "",
    });
    setPromptDomainDescription("");
    setPromptDraftSummary("");
    setPromptEditorOpen(true);
  };

  const generatePromptDraft = async () => {
    if (!selectedPromptDraftProfile) {
      setActionError("점검이 끝나고 모델이 선택된 외부 제공자가 필요합니다.");
      return;
    }
    if (!promptForm.name.trim() || !promptDomainDescription.trim()) {
      setActionError("프롬프트 이름과 도메인 설명을 입력하세요.");
      return;
    }
    if (
      (promptForm.translation_prompt.trim() || promptForm.review_prompt.trim())
      && !window.confirm("현재 1차·2차 프롬프트 입력값을 외부 모델의 초안으로 교체할까요?")
    ) return;
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const draft = await api.createPromptDraft({
        name: promptForm.name,
        domain_description: promptDomainDescription,
        provider: selectedPromptDraftProfile.provider,
        model: selectedPromptDraftProfile.selected_model,
      });
      setPromptForm((current) => ({
        ...current,
        translation_prompt: draft.translation_prompt,
        review_prompt: draft.review_prompt,
      }));
      setPromptDraftSummary(draft.summary);
      setNotice(`${EXTERNAL_PROVIDER_LABEL[draft.provider]} · ${draft.model}이 1차·2차 프롬프트 초안을 생성했습니다. 저장 전 내용을 검토하세요.`);
    } catch (reason) {
      setActionError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const startPromptImprovement = () => {
    if (!selectedFeedbackCategory || !selectedImprovementProfile) return;
    const stageLabel = PROMPT_STAGE_LABEL[feedbackStage];
    const providerLabel = EXTERNAL_PROVIDER_LABEL[selectedImprovementProfile.provider];
    const confirmed = window.confirm(
      `${selectedFeedbackCategory.name}의 ${stageLabel} 개선안을 생성할까요?\n\n`
      + `${providerLabel} · ${selectedImprovementProfile.selected_model}\n`
      + `포함 피드백 ${includedScopedFeedback.length}개 · 작업 ${includedFeedbackJobCount}개\n\n`
      + "후보만 생성되며 승인하기 전에는 현재 프롬프트가 바뀌지 않습니다.",
    );
    if (!confirmed) return;
    void guard(
      () => api.createPromptImprovement({
        category_id: selectedFeedbackCategory.id,
        stage: feedbackStage,
        provider: selectedImprovementProfile.provider,
        model: selectedImprovementProfile.selected_model,
      }),
      `${selectedFeedbackCategory.name} ${stageLabel} 개선안 생성을 시작했습니다.`,
    );
  };

  const submitExternalModel = async (
    event: React.FormEvent<HTMLFormElement>,
    profile: ExternalModelProfile,
  ) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    await guard(
      async () => {
        await api.updateExternalModel(profile.provider, {
          base_url: String(values.get("base_url") ?? ""),
          credential: String(values.get("credential") ?? "") || null,
          region: String(values.get("region") ?? ""),
        });
        await api.probeExternalModel(profile.provider);
      },
      `${profile.provider} 인증과 모델 목록을 확인했습니다.`,
    );
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
  const runtimes = data?.transcribers ?? [];
  const translationGroups = [...(data?.translation_groups ?? [])].sort(
    (left, right) => (left.stage === "draft" ? -1 : right.stage === "draft" ? 1 : 0),
  );
  const activePromptCategories = (data?.prompt_categories ?? []).filter((category) => !category.archived);
  const selectedFeedbackCategory = activePromptCategories.find((category) => category.id === feedbackCategoryId)
    ?? activePromptCategories[0]
    ?? null;
  const externalAuthoringProfiles = (data?.external_models ?? []).filter(
    (profile) => profile.configured && profile.status === "ready" && Boolean(profile.selected_model),
  );
  const selectedPromptDraftProfile = externalAuthoringProfiles.find(
    (profile) => profile.provider === promptDraftProvider,
  ) ?? externalAuthoringProfiles[0] ?? null;
  const selectedImprovementProfile = externalAuthoringProfiles.find(
    (profile) => profile.provider === improvementProvider,
  ) ?? externalAuthoringProfiles[0] ?? null;
  const scopedFeedback = (data?.translation_feedback ?? []).filter(
    (feedback) => feedback.category_id === selectedFeedbackCategory?.id
      && feedback.base_revision_id === selectedFeedbackCategory.prompt_revision_id
      && feedback.stage === feedbackStage,
  );
  const includedScopedFeedback = scopedFeedback.filter((feedback) => feedback.included);
  const includedFeedbackJobCount = new Set(includedScopedFeedback.map((feedback) => feedback.job_id)).size;
  const feedbackReady = includedScopedFeedback.length >= 20 && includedFeedbackJobCount >= 3;
  const scopedImprovementRuns = (data?.prompt_improvement_runs ?? []).filter(
    (run) => run.category_id === selectedFeedbackCategory?.id && run.stage === feedbackStage,
  );
  return (
    <>
      <LoadingOverlay
        active={refreshing || busy}
        message={busy ? "설정 변경을 적용하는 중입니다" : "설정을 불러오는 중입니다"}
      />
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
            <div><h2>전사 모델 서버</h2><span className="sub m">{runtimes.length}개 서버</span></div>
            {runtimeEditorOpen ? <span className="b line">{form.id ? "수정 중" : "추가 중"}</span> : (
              <button type="button" className="btn sec sm" onClick={() => { setForm(EMPTY); setRuntimeEditorOpen(true); }}><Icon name="plus" size={14} />서버 추가</button>
            )}
          </div>
          <div className="card-body flush">
            <div className="tbl settings-runtime-table" role="table" aria-label="전사 모델 서버 목록">
              <div className="tr head runtime-grid" role="row">
                <span role="columnheader">서버</span><span role="columnheader">상태</span><span role="columnheader" className="r">작업</span><span role="columnheader">Kotoba 배치</span><span role="columnheader">WhisperX 배치</span><span role="columnheader" className="r">관리</span>
              </div>
              {runtimes.length === 0 && !runtimeEditorOpen ? <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}><span role="cell">등록된 전사 모델 서버 없음</span></div> : null}
              {runtimes.map((runtime: TranscriberEndpoint) => {
                const parsed = asRuntimeStatus(runtime.status);
                const tone = parsed ? RUNTIME_STATUS_TONE[parsed] : null;
                if (runtimeEditorOpen && form.id === runtime.id) return (
                  <form key={runtime.id} className="tr runtime-grid inline-edit-row" role="row" onSubmit={submitTranscriber}>
                    <div role="cell" data-label="서버" className="inline-server-fields">
                      {runtime.builtin ? <>
                        <div className="runtime-title"><strong>{runtime.name}</strong><span className="b line">기본</span></div>
                        <label className="server-resource-field"><span>GPU 공유 그룹</span><input required maxLength={80} className={field} aria-label="GPU 공유 그룹" value={form.resource_group_id} onChange={(event) => setForm({ ...form, resource_group_id: event.target.value })} /><small>같은 GPU를 사용하는 전사·번역 서버에 같은 값을 지정합니다.</small></label>
                      </> : <>
                        <input required maxLength={80} className={field} aria-label="전사 모델 서버 이름" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
                        <input required type="url" className={field} aria-label="전사 모델 서버 API 주소" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} />
                        <input type="password" autoComplete="new-password" className={field} aria-label="전사 모델 서버 API 토큰" value={form.token} onChange={(event) => setForm({ ...form, token: event.target.value })} placeholder="토큰: 비우면 유지" />
                        <label className="server-resource-field"><span>GPU 공유 그룹</span><input required maxLength={80} className={field} aria-label="GPU 공유 그룹" value={form.resource_group_id} onChange={(event) => setForm({ ...form, resource_group_id: event.target.value })} placeholder="예: runtime-02-gpu" /><small>같은 GPU를 사용하는 전사·번역 서버에 같은 값을 지정합니다.</small></label>
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
                  <div className="t-name" role="cell" data-label="서버" title={`${runtime.name}\n${runtime.base_url}`}><div className="runtime-title"><strong>{runtime.name}</strong>{runtime.builtin ? <span className="b line">기본</span> : null}</div><span className="m">{runtime.base_url}</span><span className="server-resource-group"><span>GPU 공유 그룹</span><code>{runtime.resource_group_id}</code></span></div>
                  <div role="cell" data-label="상태" className="runtime-status-cell"><span className={tone ? BADGE_CLASS[tone] : "b"} title={runtime.message ?? undefined}>{parsed ? RUNTIME_STATUS_LABEL[parsed] : runtime.status}</span></div>
                  <span role="cell" data-label="작업" className="runtime-count-cell r m">{runtime.running_jobs} / {runtime.capacity}</span>
                  <span role="cell" data-label="Kotoba 배치" className="runtime-batch-cell m">{runtime.kotoba_batch_size ?? "기본값"}</span>
                  <span role="cell" data-label="WhisperX 배치" className="runtime-batch-cell m">{runtime.whisperx_batch_size ?? "기본값"}</span>
                  <span role="cell" data-label="관리" className="btns runtime-actions">
                    <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.probeTranscriber(runtime.id))}>확인</button>
                    {!runtime.builtin ? <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.updateTranscriber(runtime.id, { name: runtime.name, base_url: runtime.base_url, enabled: !runtime.enabled, capacity: runtime.capacity, resource_group_id: runtime.resource_group_id }))}>{runtime.enabled ? "중지" : "사용"}</button> : null}
                    <button type="button" className="btn sec sm" aria-label={`${runtime.name} 수정`} disabled={busy} onClick={() => editRuntime(runtime)}><Icon name="pencil" size={13} />수정</button>
                    {!runtime.builtin ? <button type="button" className="btn dgr sm" disabled={busy} onClick={() => { if (window.confirm(`${runtime.name} 전사 모델 서버를 삭제할까요?`)) void guard(() => api.deleteTranscriber(runtime.id), "삭제했습니다."); }}><Icon name="trash" size={13} /></button> : null}
                  </span>
                </div>;
              })}
              {runtimeEditorOpen && !form.id ? <form className="tr runtime-grid inline-edit-row" role="row" onSubmit={submitTranscriber}>
                <div role="cell" data-label="서버" className="inline-server-fields"><input required maxLength={80} className={field} aria-label="전사 모델 서버 이름" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="서버 이름" /><input required type="url" className={field} aria-label="전사 모델 서버 API 주소" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="http://transcriber-host:8100" /><input type="password" autoComplete="new-password" className={field} aria-label="전사 모델 서버 API 토큰" value={form.token} onChange={(event) => setForm({ ...form, token: event.target.value })} placeholder="API 토큰" /><label className="server-resource-field"><span>GPU 공유 그룹</span><input required maxLength={80} className={field} aria-label="GPU 공유 그룹" value={form.resource_group_id} onChange={(event) => setForm({ ...form, resource_group_id: event.target.value })} placeholder="예: gpu-main" /><small>같은 GPU를 사용하는 전사·번역 서버에 같은 값을 지정합니다.</small></label></div>
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
          const editingServer = group.servers.find((server) => server.id === translationForm.id);
          return <section className="card" key={group.stage}>
            <div className="card-head">
              <h2>{group.label}</h2>
              {translationEditorStage === group.stage ? <span className="b line">{translationForm.id ? "수정 중" : "추가 중"}</span> : <button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorStage(group.stage); }}><Icon name="plus" size={14} />서버 추가</button>}
            </div>
            <div className="card-body flush">
              <div className="tbl settings-translation-table" role="table" aria-label={`${group.label} 서버 목록`}>
                <div className="tr head translation-server-grid" role="row"><span role="columnheader">서버</span><span role="columnheader">모델 선택</span><span role="columnheader">Thinking</span><span role="columnheader">연결 / 라우팅</span><span role="columnheader" className="r">요청</span><span role="columnheader">사용</span><span role="columnheader">일괄 처리 우선</span><span role="columnheader" className="r">관리</span></div>
                {group.servers.map((server) => {
                  if (translationEditorStage === group.stage && translationForm.id === server.id) return <form key={server.id} className="tr translation-server-grid inline-edit-row" role="row" onSubmit={submitTranslationEndpoint}>
                    <div role="cell" data-label="서버" className="inline-server-fields"><input required maxLength={80} className={field} aria-label="서버 이름" value={translationForm.name} onChange={(event) => setTranslationForm({ ...translationForm, name: event.target.value })} /><input required type="url" className={field} aria-label="API 주소" value={translationForm.base_url} onChange={(event) => setTranslationForm({ ...translationForm, base_url: event.target.value })} /><input type="password" autoComplete="new-password" className={field} aria-label="API 토큰" value={translationForm.token} onChange={(event) => setTranslationForm({ ...translationForm, token: event.target.value })} placeholder={editingServer?.token_configured ? "토큰: 비우면 유지" : "API 토큰"} /><label className="server-resource-field"><span>GPU 공유 그룹</span><input required maxLength={80} className={field} aria-label="GPU 공유 그룹" value={translationForm.resource_group_id} onChange={(event) => setTranslationForm({ ...translationForm, resource_group_id: event.target.value })} /><small>같은 GPU를 사용하는 전사·번역 서버에 같은 값을 지정합니다.</small></label></div>
                    <label role="cell" data-label="모델 선택"><select className="ctl translation-model-select" aria-label={`${server.name} 모델 선택`} value={server.selected_model} disabled={busy || server.models.length === 0} onChange={(event) => void guard(() => api.updateTranslationServerModel(group.stage, server.id, event.target.value), "번역 모델을 저장했습니다.")}><option value="" disabled>{server.models.length ? "모델 선택" : "확인 후 선택"}</option>{server.models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
                    <label role="cell" data-label="Thinking" className="translation-toggle"><input type="checkbox" checked={translationForm.thinking_enabled} onChange={(event) => setTranslationForm({ ...translationForm, thinking_enabled: event.target.checked })} /><span>{translationForm.thinking_enabled ? "사용" : "미사용"}</span></label>
                    <span role="cell" data-label="연결 상태"><span className="b line">수정 중</span></span>
                    <label role="cell" data-label="요청" className="inline-number-field"><input type="number" min={1} max={8} className={field} aria-label="동시 요청" value={translationForm.capacity} onChange={(event) => setTranslationForm({ ...translationForm, capacity: Number(event.target.value) })} /></label>
                    <label role="cell" data-label="사용" className="translation-toggle"><input type="checkbox" checked={translationForm.enabled} onChange={(event) => setTranslationForm({ ...translationForm, enabled: event.target.checked, batch_preferred: event.target.checked && translationForm.batch_preferred })} /><span>{translationForm.enabled ? "ON" : "OFF"}</span></label>
                    <span role="cell" data-label="일괄 처리 우선" className="m">{translationForm.batch_preferred ? "선택됨" : "미선택"}</span>
                    <span role="cell" data-label="관리" className="btns translation-server-actions"><button type="submit" className="btn sm" disabled={busy}>저장</button><button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorStage(null); }}>취소</button></span>
                  </form>;
                  const statusLabel = server.status === "ready" ? "연결 정상" : server.status === "unconfigured" ? "주소 미설정" : server.status === "unavailable" ? "연결 실패" : "확인 전";
                  const routingLabel = server.routing_reason === "stt_hard_breaker" ? "전사 중 요청 대기" : server.routing_reason === "review_priority" ? "2차 검수 우선" : null;
                  return <div key={server.id} className="tr translation-server-grid" role="row">
                    <div className="t-name" role="cell" data-label="서버" title={`${server.name}\n${server.base_url}`}><div className="runtime-title"><strong>{server.name}</strong>{server.builtin ? <span className="b line">기본</span> : null}</div><span className="m">{server.base_url || "API 주소 미설정"}</span><span className="server-resource-group"><span>GPU 공유 그룹</span><code>{server.resource_group_id}</code></span></div>
                    <label role="cell" data-label="모델 선택"><select className="ctl translation-model-select" aria-label={`${server.name} 모델 선택`} value={server.selected_model} disabled={busy || server.models.length === 0} onChange={(event) => void guard(() => api.updateTranslationServerModel(group.stage, server.id, event.target.value), "번역 모델을 저장했습니다.")}><option value="" disabled>{server.models.length ? "모델 선택" : "확인 후 선택"}</option>{server.models.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
                    <label role="cell" data-label="Thinking" className="translation-toggle"><input type="checkbox" checked={server.thinking_enabled} disabled={busy} onChange={(event) => void updateTranslationThinking(group.stage, server, event.target.checked)} /><span>{server.thinking_enabled ? "사용" : "미사용"}</span></label>
                    <span role="cell" data-label="연결 / 라우팅" className="translation-server-state"><span title={server.message ?? undefined} className={server.status === "ready" ? "b ok dot" : server.status === "unavailable" ? "b bad dot" : "b wait dot"}>{statusLabel}</span>{routingLabel ? <span className="b hold" title={server.routing_message ?? undefined}>{routingLabel}</span> : null}</span>
                    <span role="cell" data-label="요청" className="r m">{server.running_jobs} / {server.capacity}</span>
                    <label role="cell" data-label="사용" className="translation-toggle"><input type="checkbox" checked={server.enabled} disabled={busy} onChange={(event) => void updateTranslationRouting(group.stage, server, { enabled: event.target.checked })} /><span>{server.enabled ? "ON" : "OFF"}</span></label>
                    <label role="cell" data-label="일괄 처리 우선" className="translation-toggle"><input type="radio" name={`batch-server-${group.stage}`} checked={server.batch_preferred} disabled={busy || !server.enabled} onChange={() => void updateTranslationRouting(group.stage, server, { batch_preferred: true })} /><span>{server.batch_preferred ? "선택됨" : "선택"}</span></label>
                    <span role="cell" data-label="관리" className="btns translation-server-actions"><button type="button" className="btn sec sm" disabled={busy || !server.base_url} onClick={() => void guard(() => api.probeTranslationEndpoint(group.stage, server.id), "연결 상태와 모델 목록을 확인했습니다.")}>확인</button><button type="button" className="btn sec sm" disabled={busy} onClick={() => editTranslationEndpoint(group.stage, server)}><Icon name="pencil" size={13} />수정</button>{!server.builtin ? <button type="button" className="btn dgr sm" disabled={busy} onClick={() => { if (window.confirm(`${group.label}의 ${server.name} 서버를 삭제할까요?`)) void guard(() => api.deleteTranslationEndpoint(group.stage, server.id), "번역 서버를 삭제했습니다."); }}><Icon name="trash" size={13} /></button> : null}</span>
                  </div>;
                })}
                {translationEditorStage === group.stage && !translationForm.id ? <form className="tr translation-server-grid inline-edit-row" role="row" onSubmit={submitTranslationEndpoint}>
                  <div role="cell" data-label="서버" className="inline-server-fields"><input required maxLength={80} className={field} aria-label="서버 이름" value={translationForm.name} onChange={(event) => setTranslationForm({ ...translationForm, name: event.target.value })} placeholder="서버 이름" /><input required type="url" className={field} aria-label="API 주소" value={translationForm.base_url} onChange={(event) => setTranslationForm({ ...translationForm, base_url: event.target.value })} placeholder="http://model-server:1234/v1" /><input type="password" autoComplete="new-password" className={field} aria-label="API 토큰" value={translationForm.token} onChange={(event) => setTranslationForm({ ...translationForm, token: event.target.value })} placeholder="API 토큰" /><label className="server-resource-field"><span>GPU 공유 그룹</span><input required maxLength={80} className={field} aria-label="GPU 공유 그룹" value={translationForm.resource_group_id} onChange={(event) => setTranslationForm({ ...translationForm, resource_group_id: event.target.value })} placeholder="예: gpu-main" /><small>같은 GPU를 사용하는 전사·번역 서버에 같은 값을 지정합니다.</small></label></div>
                  <span role="cell" data-label="모델 선택" className="m">추가 후 선택</span>
                  <label role="cell" data-label="Thinking" className="translation-toggle"><input type="checkbox" checked={translationForm.thinking_enabled} onChange={(event) => setTranslationForm({ ...translationForm, thinking_enabled: event.target.checked })} /><span>{translationForm.thinking_enabled ? "사용" : "미사용"}</span></label>
                  <span role="cell" data-label="연결 상태"><span className="b line">추가 중</span></span>
                  <label role="cell" data-label="요청" className="inline-number-field"><input type="number" min={1} max={8} className={field} aria-label="동시 요청" value={translationForm.capacity} onChange={(event) => setTranslationForm({ ...translationForm, capacity: Number(event.target.value) })} /></label>
                  <label role="cell" data-label="사용" className="translation-toggle"><input type="checkbox" checked={translationForm.enabled} onChange={(event) => setTranslationForm({ ...translationForm, enabled: event.target.checked })} /><span>{translationForm.enabled ? "ON" : "OFF"}</span></label>
                  <span role="cell" data-label="일괄 처리 우선" className="m">저장 후 선택</span>
                  <span role="cell" data-label="관리" className="btns translation-server-actions"><button type="submit" className="btn sm" disabled={busy}>추가</button><button type="button" className="btn sec sm" onClick={() => { setTranslationForm(EMPTY_TRANSLATION_ENDPOINT); setTranslationEditorStage(null); }}>취소</button></span>
                </form> : null}
              </div>
            </div>
          </section>;
        })}

        <section className="card" aria-labelledby="external-models-title">
          <div className="card-head">
            <div>
              <h2 id="external-models-title">외부 모델</h2>
              <span className="sub m">외부 검토와 프롬프트 작성에 사용할 모델을 등록합니다.</span>
            </div>
          </div>
          <div className="card-body settings-external-models">
            {(data?.external_models ?? []).map((profile) => {
              const statusClass = profile.status === "ready"
                ? "b ok dot"
                : profile.status === "failed"
                  ? "b bad dot"
                  : "b wait dot";
              return (
                <form
                  key={`${profile.provider}-${profile.updated_at}`}
                  className="settings-editor"
                  onSubmit={(event) => void submitExternalModel(event, profile)}
                >
                  <div className="settings-editor-head">
                    <strong>{EXTERNAL_PROVIDER_LABEL[profile.provider]}</strong>
                    <span className={statusClass} title={profile.message ?? undefined}>
                      {profile.status === "ready" ? "인증 완료" : profile.status === "failed" ? "점검 실패" : "점검 필요"}
                    </span>
                  </div>
                  {profile.provider === "nvidia_build" ? (
                    <label className="f">
                      <span className="lb">API 주소</span>
                      <input name="base_url" type="url" required className="ctl" defaultValue={profile.base_url} />
                    </label>
                  ) : <input name="base_url" type="hidden" value={profile.base_url} />}
                  {profile.provider === "bedrock" ? (
                    <label className="f">
                      <span className="lb">리전</span>
                      <input name="region" required className="ctl" defaultValue={profile.region} placeholder="ap-northeast-2" />
                    </label>
                  ) : <input name="region" type="hidden" value="" />}
                  <label className="f">
                    <span className="lb">API 키 또는 credential</span>
                    <input
                      name="credential"
                      type="password"
                      autoComplete="new-password"
                      className="ctl"
                      placeholder={profile.credential_configured ? "비우면 기존 값 유지" : "자격 증명 입력"}
                    />
                  </label>
                  <label className="f">
                    <span className="lb">사용 모델</span>
                    <select
                      className="ctl"
                      value={profile.selected_model}
                      disabled={busy || profile.status !== "ready" || profile.models.length === 0}
                      onChange={(event) => void guard(
                        () => api.selectExternalModel(profile.provider, event.target.value),
                        `${EXTERNAL_PROVIDER_LABEL[profile.provider]} 모델을 저장했습니다.`,
                      )}
                    >
                      <option value="">{profile.models.length ? "모델 선택" : "연결 점검 후 선택"}</option>
                      {profile.models.map((model) => <option key={model} value={model}>{model}</option>)}
                    </select>
                  </label>
                  <p className="external-model-current" aria-live="polite">
                    <span>현재 사용 모델</span>
                    <strong className="code">
                      {profile.selected_model || "선택되지 않음"}
                    </strong>
                  </p>
                  {profile.message ? <p className="m" role={profile.status === "failed" ? "alert" : "status"}>{profile.message}</p> : null}
                  <div className="btns">
                    <button type="submit" className="btn" disabled={busy}>저장 및 연결 점검</button>
                    <button
                      type="button"
                      className="btn sec"
                      disabled={busy || !profile.credential_configured}
                      onClick={() => void guard(
                        () => api.probeExternalModel(profile.provider),
                        `${EXTERNAL_PROVIDER_LABEL[profile.provider]} 인증과 모델 목록을 확인했습니다.`,
                      )}
                    >
                      다시 점검
                    </button>
                  </div>
                </form>
              );
            })}
          </div>
        </section>

        <section className="card">
          <div className="card-head">
            <div><h2>번역 프롬프트</h2><span className="sub m" title={`${(data?.prompt_categories ?? []).length}개`}>{(data?.prompt_categories ?? []).length}개</span></div>
            {promptEditorOpen ? (
              <span className="b line">{promptForm.id ? "수정 중" : "추가 중"}</span>
            ) : (
              <button type="button" className="btn sec sm" aria-expanded="false" aria-controls="prompt-editor" onClick={() => { setPromptForm(EMPTY_PROMPT); setPromptDomainDescription(""); setPromptDraftSummary(""); setPromptEditorOpen(true); }}>
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
            <div className="settings-editor-head"><strong>{promptForm.id ? "프롬프트 수정" : "새 프롬프트 추가"}</strong><span>장르별 1차 번역과 2차 검증 지시문을 한 쌍의 revision으로 관리합니다.</span></div>
            <form className="prompt-form settings-prompt-form" onSubmit={submitPrompt}>
              <label className="f">
                <span className="lb">이름</span>
                <input className="ctl" required maxLength={120} value={promptForm.name} onChange={(event) => setPromptForm({ ...promptForm, name: event.target.value })} />
              </label>
              <section className="prompt-draft-builder" aria-labelledby="prompt-draft-builder-title">
                <div className="prompt-draft-builder-head">
                  <div>
                    <strong id="prompt-draft-builder-title">외부 모델로 기초 프롬프트 만들기</strong>
                    <span>도메인 설명으로 1차·2차 초안을 만들며, 아래 입력란에만 채워지고 자동 저장되지 않습니다.</span>
                  </div>
                  <span className="b line">{data?.prompt_authoring?.draft_instruction_version ?? "-"}</span>
                </div>
                <div className="prompt-draft-builder-fields">
                  <label className="f">
                    <span className="lb">외부 모델</span>
                    <select
                      className="ctl"
                      value={selectedPromptDraftProfile?.provider ?? ""}
                      disabled={busy || externalAuthoringProfiles.length === 0}
                      onChange={(event) => setPromptDraftProvider(event.target.value as ExternalModelProvider)}
                    >
                      {externalAuthoringProfiles.length === 0 ? <option value="">사용 가능한 외부 모델 없음</option> : null}
                      {externalAuthoringProfiles.map((profile) => (
                        <option key={profile.provider} value={profile.provider}>
                          {EXTERNAL_PROVIDER_LABEL[profile.provider]} · {profile.selected_model}
                        </option>
                      ))}
                    </select>
                    <span className="hp">설정의 외부 모델에서 인증 점검과 모델 선택을 먼저 완료해야 합니다.</span>
                  </label>
                  <label className="f prompt-domain-field">
                    <span className="lb">도메인 설명</span>
                    <textarea
                      className="ctl textarea"
                      rows={4}
                      maxLength={10000}
                      value={promptDomainDescription}
                      onChange={(event) => setPromptDomainDescription(event.target.value)}
                      placeholder="예: 일본 버라이어티 토크쇼. 다중 화자, 간사이 사투리, 짧은 맞장구와 겹말이 많음. 누락 방지와 호칭 일관성을 우선함."
                    />
                  </label>
                </div>
                <div className="prompt-draft-builder-actions">
                  <details className="prompt-instruction-details">
                    <summary>외부 모델 지시문 보기</summary>
                    <pre>{data?.prompt_authoring?.draft_system_prompt ?? "지시문을 불러오는 중입니다."}</pre>
                  </details>
                  <button
                    type="button"
                    className="btn sec"
                    disabled={busy || !selectedPromptDraftProfile || !promptForm.name.trim() || !promptDomainDescription.trim()}
                    onClick={() => void generatePromptDraft()}
                  >
                    1·2차 초안 생성
                  </button>
                </div>
                {promptDraftSummary ? <p className="notice success" role="status">{promptDraftSummary}</p> : null}
              </section>
              <label className="f">
                <span className="lb">1차(초벌) 번역 프롬프트</span>
                <textarea className="ctl textarea" required rows={8} value={promptForm.translation_prompt} onChange={(event) => setPromptForm({ ...promptForm, translation_prompt: event.target.value })} />
              </label>
              <label className="f">
                <span className="lb">2차(검사·교정) 프롬프트</span>
                <textarea className="ctl textarea" required rows={8} value={promptForm.review_prompt} onChange={(event) => setPromptForm({ ...promptForm, review_prompt: event.target.value })} />
              </label>
              <div className="btns">
                <button type="submit" className="btn" disabled={busy}>{promptForm.id ? "변경 저장" : "프롬프트 추가"}</button>
                <button type="button" className="btn sec" onClick={() => { setPromptForm(EMPTY_PROMPT); setPromptDomainDescription(""); setPromptDraftSummary(""); setPromptEditorOpen(false); }}>취소</button>
              </div>
            </form>
          </div> : null}
        </section>

        <section className="card" aria-labelledby="prompt-feedback-title">
          <div className="card-head">
            <div>
              <h2 id="prompt-feedback-title">번역 수정 피드백</h2>
              <span className="sub m">프롬프트와 단계를 선택해 피드백과 개선 이력을 관리합니다.</span>
            </div>
          </div>
          <div className="card-body prompt-improvement-launcher">
            <div className="prompt-improvement-fields">
              <label className="f">
                <span className="lb">대상 프롬프트</span>
                <select
                  className="ctl"
                  value={selectedFeedbackCategory?.id ?? ""}
                  disabled={busy || activePromptCategories.length === 0}
                  onChange={(event) => setFeedbackCategoryId(event.target.value)}
                >
                  {activePromptCategories.length === 0 ? <option value="">사용 중인 프롬프트 없음</option> : null}
                  {activePromptCategories.map((category) => (
                    <option key={category.id} value={category.id}>{category.name} · rev.{category.prompt_revision_number}</option>
                  ))}
                </select>
              </label>
              <fieldset className="prompt-stage-field">
                <legend>개선 단계</legend>
                <div className="seg" aria-label="개선 단계">
                  {(["translation", "review"] as const).map((stage) => (
                    <button
                      key={stage}
                      type="button"
                      className={feedbackStage === stage ? "on" : ""}
                      aria-pressed={feedbackStage === stage}
                      disabled={busy}
                      onClick={() => setFeedbackStage(stage)}
                    >
                      {PROMPT_STAGE_LABEL[stage]}
                    </button>
                  ))}
                </div>
              </fieldset>
              <label className="f">
                <span className="lb">개선 수행 외부 모델</span>
                <select
                  className="ctl"
                  value={selectedImprovementProfile?.provider ?? ""}
                  disabled={busy || externalAuthoringProfiles.length === 0}
                  onChange={(event) => setImprovementProvider(event.target.value as ExternalModelProvider)}
                >
                  {externalAuthoringProfiles.length === 0 ? <option value="">사용 가능한 외부 모델 없음</option> : null}
                  {externalAuthoringProfiles.map((profile) => (
                    <option key={profile.provider} value={profile.provider}>
                      {EXTERNAL_PROVIDER_LABEL[profile.provider]} · {profile.selected_model}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="prompt-improvement-summary">
              <div>
                <span className={feedbackReady ? "b ok" : "b wait"}>{feedbackReady ? "표본 준비됨" : "표본 부족"}</span>
                <strong>포함 피드백 {includedScopedFeedback.length}개 · 작업 {includedFeedbackJobCount}개</strong>
                <span>실행하려면 현재 revision의 포함 피드백 20개와 서로 다른 작업 3개가 필요합니다.</span>
              </div>
              <div className="prompt-improvement-actions">
                <details className="prompt-instruction-details">
                  <summary>외부 모델 지시문 보기</summary>
                  <pre>{data?.prompt_authoring?.improvement_system_prompt ?? "지시문을 불러오는 중입니다."}</pre>
                </details>
                <button
                  type="button"
                  className="btn"
                  disabled={busy || !selectedFeedbackCategory || !selectedImprovementProfile || !feedbackReady}
                  onClick={startPromptImprovement}
                >
                  개선안 생성
                </button>
              </div>
            </div>
            <p className="prompt-improvement-note">
              외부 모델이 후보와 예상 평가를 생성합니다. 자동 적용되지 않으며, 개선안을 확인한 뒤 별도로 승인해야 합니다.
            </p>
          </div>
          <div className="card-body flush">
            <div className="tbl prompt-feedback-table" role="table" aria-label="번역 수정 피드백">
              <div className="tr head prompt-feedback-grid" role="row"><span role="columnheader">단계</span><span role="columnheader">원문</span><span role="columnheader">모델 결과</span><span role="columnheader">사용자 수정</span><span role="columnheader">포함</span></div>
              {scopedFeedback.map((feedback) => <div className="tr prompt-feedback-grid" role="row" key={feedback.id}>
                <span role="cell" data-label="단계" className="m">{feedback.stage === "translation" ? "1차" : "2차"}</span>
                <span role="cell" data-label="원문" title={feedback.source_text}>{feedback.source_text}</span>
                <span role="cell" data-label="모델 결과" title={feedback.model_text}>{feedback.model_text}</span>
                <span role="cell" data-label="사용자 수정" title={feedback.edited_text}>{feedback.edited_text}</span>
                <label role="cell" data-label="포함" className="translation-toggle"><input type="checkbox" checked={feedback.included} disabled={busy} onChange={(event) => void guard(() => api.setTranslationFeedbackIncluded(feedback.id, event.target.checked), event.target.checked ? "피드백을 포함했습니다." : "피드백을 제외했습니다.")} /><span>{feedback.included ? "포함" : "제외"}</span></label>
              </div>)}
              {scopedFeedback.length === 0 ? <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}><span role="cell">선택한 프롬프트와 단계의 수정 피드백 없음</span></div> : null}
            </div>
          </div>
          <div className="card-body flush">
            <div className="tbl prompt-run-table" role="table" aria-label="프롬프트 개선 작업">
              <div className="tr head prompt-run-grid" role="row"><span role="columnheader">프롬프트 / 단계</span><span role="columnheader">상태</span><span role="columnheader">생성 모델</span><span role="columnheader">표본</span><span role="columnheader">모델 예상 평가</span><span role="columnheader" className="r">관리</span></div>
              {scopedImprovementRuns.map((run) => {
                const category = (data?.prompt_categories ?? []).find((item) => item.id === run.category_id);
                const provider = run.endpoint_contract as ExternalModelProvider;
                const providerName = provider in EXTERNAL_PROVIDER_LABEL ? EXTERNAL_PROVIDER_LABEL[provider] : "기존 실행 경로";
                return <div className="tr prompt-run-grid" role="row" key={run.id}>
                  <span role="cell" data-label="프롬프트 / 단계"><strong>{category?.name ?? run.category_id}</strong><span className="m">{run.stage === "translation" ? "1차" : "2차"}</span></span>
                  <span role="cell" data-label="상태"><span className={run.status === "ready" || run.status === "activated" ? "b ok" : run.status === "failed" ? "b bad" : "b wait"}>{PROMPT_RUN_STATUS_LABEL[run.status]}</span>{run.error ? <span className="m" title={run.error}>{run.error}</span> : null}</span>
                  <span role="cell" data-label="생성 모델" className="prompt-run-model"><strong>{providerName}</strong><span className="m code" title={run.model_contract}>{run.model_contract}</span></span>
                  <span role="cell" data-label="표본" className="m">학습 {run.train_feedback_ids.length} · 검증 {run.holdout_feedback_ids.length}</span>
                  <span role="cell" data-label="모델 예상 평가" className="m">{run.evaluation ? `${run.evaluation.current_score} → ${run.evaluation.candidate_score} · 회귀 위험 ${run.evaluation.regressions.length}` : "-"}</span>
                  <span role="cell" data-label="관리" className="btns">
                    {run.proposed_prompt ? <details><summary className="btn sec sm">개선안</summary><div className="prompt-proposal-popover"><strong>후보 프롬프트</strong><pre>{run.proposed_prompt}</pre></div></details> : null}
                    {run.evaluation ? <details><summary className="btn sec sm">예상 평가</summary><div className="prompt-proposal-popover"><strong>모델 자체 평가</strong><p>{run.evaluation.summary}</p><strong>예상 회귀 위험 {run.evaluation.regressions.length}건</strong>{run.evaluation.regressions.length > 0 ? <ul>{run.evaluation.regressions.map((regression) => <li key={`${regression.feedback_id}:${regression.reason}`}><code>{regression.feedback_id}</code> {regression.reason}</li>)}</ul> : <p>모델이 예상한 회귀 위험 없음</p>}</div></details> : null}
                    {run.status === "queued" || run.status === "running" ? <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.cancelPromptImprovement(run.id), "개선 작업을 취소했습니다.")}>취소</button> : null}
                    {run.status === "ready" ? <button type="button" className="btn sm" disabled={busy} onClick={() => { if (window.confirm("이 개선안을 새 revision으로 승인하고 활성화할까요?")) void guard(() => api.activatePromptImprovement(run.id), "새 프롬프트 revision을 활성화했습니다."); }}>승인하고 활성화</button> : null}
                    {run.status === "ready" ? <button type="button" className="btn sec sm" disabled={busy} onClick={() => void guard(() => api.rejectPromptImprovement(run.id), "개선안을 거절했습니다.")}>거절</button> : null}
                  </span>
                </div>;
              })}
              {scopedImprovementRuns.length === 0 ? <div className="tr empty" role="row" style={{ gridTemplateColumns: "minmax(0, 1fr)" }}><span role="cell">선택한 프롬프트와 단계의 개선 작업 없음</span></div> : null}
            </div>
          </div>
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
