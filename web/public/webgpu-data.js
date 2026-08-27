/* /api/v1/dashboard 응답을 3D 씬이 사용하는 작고 안정적인 계약으로 바꾼다. */
(() => {
  const showRendererFailure = () => {
    const state = document.getElementById("renderer-status");
    if (state) {
      state.textContent = "3D 초기화 실패";
      state.className = "b bad dot";
    }
  };
  window.addEventListener("unhandledrejection", showRendererFailure);
  window.addEventListener("error", (event) => {
    if (String(event.filename || "").includes("webgpu-scene")) showRendererFailure();
  });

  const LIMIT = 3;
  const MEDIA_LIMIT = 9;
  const PHASE_LABEL = {
    extraction: "추출",
    transcription: "전사",
    translation: "번역",
    render: "렌더",
    complete: "완료",
  };
  const STATE_LABEL = {
    running: "진행 중",
    waiting: "대기",
    paused: "일시 정지",
    blocked: "중단",
    stopped: "정지",
    failed: "실패",
    done: "완료",
  };
  const TRANSCRIPTION_STAGE_LABEL = {
    model_loading: "모델 준비",
    scene_detection: "장면 분석",
    primary_transcription: "1차 전사",
    secondary_transcription: "2차 전사",
    forced_alignment: "강제 정렬",
    speaker_diarization: "화자 분리",
    quality_analysis: "문제 구간 분석",
    rescue_transcription: "문제 구간 재전사",
    transcription_merge: "전사 결과 병합",
    subtitle_normalization: "자막 구간 구성",
  };

  const percent = (job) => {
    if (job.phase === "transcription") {
      const total = Math.max(job.chunks_created || 0, job.chunks_total_estimate || 0);
      return total ? Math.round(((job.chunks_completed || 0) / total) * 100) : null;
    }
    if (job.phase === "translation" && job.translation_chunks_total) {
      return Math.round(((job.translation_chunks_completed || 0) / job.translation_chunks_total) * 100);
    }
    return job.state === "done" ? 100 : null;
  };

  const view = (job, runtimeNames) => ({
    id: job.id,
    source_rel: job.source_rel,
    title: String(job.source_rel || "").split("/").pop() || job.source_rel,
    phase: PHASE_LABEL[job.phase] || job.phase,
    stage: PHASE_LABEL[job.phase] || job.phase,
    state: job.state,
    status: STATE_LABEL[job.state] || job.state,
    status_label: STATE_LABEL[job.state] || job.state,
    reason_code: job.reason_code,
    error: job.error || "",
    detail: [
      PHASE_LABEL[job.phase] || job.phase,
      job.phase === "transcription" && job.transcription_stage
        ? TRANSCRIPTION_STAGE_LABEL[job.transcription_stage] || job.transcription_stage
        : null,
      job.stt_runtime_id ? `전사 서버 ${runtimeNames.get(job.stt_runtime_id) || job.stt_runtime_id}` : null,
      STATE_LABEL[job.state] || job.state,
    ].filter(Boolean).join(" · "),
    percent: percent(job),
    href: `/jobs/${encodeURIComponent(job.id)}`,
  });

  const emptyGpu = (snapshot) => ({
    available: false,
    display_name: snapshot?.configured ? "메트릭 수집 안 됨" : "모니터링 미설정",
    utilization_percent: null,
    memory_percent: null,
    memory_used_gib: null,
    memory_total_gib: null,
    temperature_celsius: null,
    power_watts: null,
  });

  const build = (payload, mediaPayload = null, runtimesPayload = null) => {
    const activeJobs = payload.active_jobs || payload.recent_jobs || [];
    const recentCompleted = payload.recent_completed
      || (payload.recent_jobs || []).filter((job) => job.state === "done");
    const counts = payload.state_counts || {};
    const stateSamples = payload.state_samples || {};
    const runtimeNames = new Map(
      (runtimesPayload?.items || []).map((runtime) => [runtime.id, runtime.name]),
    );
    const active = activeJobs.map((job) => ({ ...job }));
    const pick = (states) => states.flatMap((state) => {
      const source = Array.isArray(stateSamples[state])
        ? stateSamples[state]
        : active.filter((job) => job.state === state);
      return source.map((job) => view(job, runtimeNames));
    });
    const runningRaw = active.filter((job) => job.state === "running");
    const queue = pick(["waiting"]);
    const blocked = pick(["blocked"]);
    const failed = pick(["failed"]);
    const paused = pick(["paused"]);
    const stopped = pick(["stopped"]);
    const completed = recentCompleted.map((job) => view(job, runtimeNames));
    const mediaFolders = mediaPayload?.folders || [];
    const gpuSnapshot = payload.gpu || null;
    const gpuDevice = gpuSnapshot?.devices?.[0] || null;
    const gpu = gpuDevice
      ? { ...emptyGpu(gpuSnapshot), ...gpuDevice, available: Boolean(gpuSnapshot.available) }
      : emptyGpu(gpuSnapshot);
    const renderingJob = runningRaw.find((job) => job.phase === "render") || null;
    const stationPhases = [
      ["extraction", "추출"],
      ["transcription", "전사"],
      ["translation", "번역"],
    ];

    return {
      rendering: renderingJob ? {
        endpoint: "완료",
        source_rel: renderingJob.source_rel,
        display_label: "렌더 중",
        detail: "자막 파일을 생성하고 있습니다.",
        href: `/jobs/${encodeURIComponent(renderingJob.id)}`,
      } : null,
      gpu,
      workers: {
        total: stationPhases.length,
        idle: stationPhases.filter(([phase]) => !runningRaw.some((job) => job.phase === phase)).length,
      },
      slots: stationPhases.map(([phase, label]) => {
        const phaseJobs = runningRaw.filter((job) => job.phase === phase);
        const job = phaseJobs[0] || null;
        return {
          stage: label,
          job: job?.source_rel || null,
          detail: job ? [
            PHASE_LABEL[job.phase],
            phase === "transcription" && job.transcription_stage
              ? TRANSCRIPTION_STAGE_LABEL[job.transcription_stage] || job.transcription_stage
              : null,
            phase === "transcription" && job.stt_runtime_id
              ? `전사 서버 ${runtimeNames.get(job.stt_runtime_id) || job.stt_runtime_id}`
              : null,
            STATE_LABEL[job.state],
          ].filter(Boolean).join(" · ") : "",
          percent: job ? percent(job) : null,
          count: phaseJobs.length,
          href: job ? `/jobs/${encodeURIComponent(job.id)}` : null,
        };
      }),
      queue: queue.slice(0, LIMIT).map((job) => ({
        ...job,
        list_href: "/jobs?state=waiting",
        state_total: counts.waiting || queue.length,
      })),
      queue_rest: Math.max(0, (counts.waiting || queue.length) - LIMIT),
      blocked: blocked.slice(0, LIMIT).map((job) => ({
        ...job,
        list_href: "/jobs?state=blocked",
        state_total: counts.blocked || blocked.length,
      })),
      blocked_rest: Math.max(0, (counts.blocked || blocked.length) - LIMIT),
      failed: failed.slice(0, LIMIT).map((job) => ({
        ...job,
        list_href: "/jobs?state=failed",
        state_total: counts.failed || failed.length,
      })),
      failed_rest: Math.max(0, (counts.failed || failed.length) - LIMIT),
      paused: paused.slice(0, LIMIT).map((job) => ({
        ...job,
        list_href: "/jobs?state=paused",
        state_total: counts.paused || paused.length,
      })),
      paused_rest: Math.max(0, (counts.paused || paused.length) - LIMIT),
      stopped: stopped.slice(0, LIMIT).map((job) => ({
        ...job,
        list_href: "/jobs?state=stopped",
        state_total: counts.stopped || stopped.length,
      })),
      stopped_rest: Math.max(0, (counts.stopped || stopped.length) - LIMIT),
      completed: completed.slice(0, LIMIT),
      completed_total: counts.done || completed.length,
      media_tree: mediaFolders.slice(0, MEDIA_LIMIT).map((folder) => ({
        path: folder.path,
        label: folder.display_name || folder.name || folder.path,
        shape: "directory",
      })),
      media_tree_rest: Math.max(
        0,
        (mediaPayload?.folder_total || mediaFolders.length) - MEDIA_LIMIT,
      ),
      state_counts: counts,
    };
  };

  const value = (number, unit = "") => Number.isFinite(number) ? `${Math.round(number)}${unit}` : "—";
  const paint = (payload) => {
    const counts = payload.state_counts || {};
    for (const node of document.querySelectorAll("[data-count]")) {
      const key = node.dataset.count;
      node.textContent = String(key === "completed" ? (counts.done || 0) : (counts[key] || 0));
    }

    const snapshot = payload.gpu;
    const gpu = snapshot?.devices?.[0];
    const fields = {
      name: gpu?.display_name || (snapshot?.configured ? "GPU 메트릭 없음" : "모니터링 미설정"),
      util: value(gpu?.utilization_percent, "%"),
      memory: value(gpu?.memory_percent, "%"),
      memory_detail: Number.isFinite(gpu?.memory_used_gib) && Number.isFinite(gpu?.memory_total_gib)
        ? `${gpu.memory_used_gib.toFixed(1)} / ${gpu.memory_total_gib.toFixed(1)} GiB`
        : "사용량 확인 불가",
      temperature: value(gpu?.temperature_celsius, "°C"),
      power: value(gpu?.power_watts, "W"),
    };
    for (const [key, text] of Object.entries(fields)) {
      const node = document.querySelector(`[data-gpu="${key}"]`);
      if (node) node.textContent = text;
    }
    for (const [key, number] of [["util", gpu?.utilization_percent], ["memory", gpu?.memory_percent]]) {
      const bar = document.querySelector(`[data-gpu-bar="${key}"]`);
      if (bar) bar.style.width = `${Math.max(0, Math.min(100, Number(number) || 0))}%`;
    }

    const state = document.getElementById("data-status");
    if (!state) return;
    if (snapshot?.available) {
      state.textContent = "데이터 정상";
      state.className = "b ok dot";
    } else if (snapshot?.stale) {
      state.textContent = "이전 GPU 값";
      state.className = "b wait dot";
    } else {
      state.textContent = snapshot?.configured ? "GPU 수집 실패" : "GPU 미설정";
      state.className = "b bad dot";
      state.title = snapshot?.error || "";
    }
  };

  const load = async () => {
    const headers = { Accept: "application/json" };
    const response = await fetch("/api/v1/dashboard", { headers });
    if (!response.ok) throw new Error(`dashboard ${response.status}`);
    const [payload, mediaPayload, runtimesPayload] = await Promise.all([
      response.json(),
      fetch(`/api/v1/media?folder_limit=${MEDIA_LIMIT}`, { headers })
        .then((result) => result.ok ? result.json() : null)
        .catch(() => null),
      fetch("/api/v1/runtimes", { headers })
        .then((result) => result.ok ? result.json() : null)
        .catch(() => null),
    ]);
    window.__SCENE_DATA__ = build(payload, mediaPayload, runtimesPayload);
    paint(payload);
  };

  window.__SCENE_DATA_READY__ = load().catch((reason) => {
    window.__SCENE_DATA__ = build({ active_jobs: [], recent_completed: [], state_counts: {}, gpu: null });
    const state = document.getElementById("data-status");
    if (state) {
      state.textContent = "Backend 연결 실패";
      state.className = "b bad dot";
      state.title = String(reason);
    }
    console.error("scene data load failed", reason);
  });
})();
