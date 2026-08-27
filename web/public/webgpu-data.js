/* /api/v1/dashboard 응답을 3D 씬이 기대하는 DATA 모양으로 바꾼다.
   구 web_app.py 의 webgpu_scene_context() 자리를 대신한다.
   씬 스크립트는 window.__SCENE_DATA__ 를 읽는다. */
(() => {
  const LIMIT = 3;
  const byState = (jobs, states) => jobs.filter((job) => states.includes(job.state));
  const view = (job) => ({
    id: job.id,
    source_rel: job.source_rel,
    title: String(job.source_rel || "").split("/").pop() || job.source_rel,
    phase: job.phase,
    state: job.state,
    reason_code: job.reason_code,
    percent: (() => {
      if (job.phase === "transcription") {
        const total = Math.max(job.chunks_created || 0, job.chunks_total_estimate || 0);
        return total ? Math.round(((job.chunks_completed || 0) / total) * 100) : 0;
      }
      if (job.phase === "translation" && job.translation_chunks_total) {
        return Math.round(((job.translation_chunks_completed || 0) / job.translation_chunks_total) * 100);
      }
      return 0;
    })(),
  });

  const build = (payload) => {
    const jobs = (payload.recent_jobs || []).map((job) => ({ ...job }));
    const counts = payload.state_counts || {};
    const pick = (states) => byState(jobs, states).map(view);
    const running = pick(["running"]);
    const queue = pick(["waiting"]);
    const blocked = pick(["blocked"]);
    const failed = pick(["failed"]);
    const paused = pick(["paused"]);
    const stopped = pick(["stopped"]);
    const completed = pick(["done"]);
    const gpu = payload.gpu && payload.gpu.available ? payload.gpu.devices[0] || null : null;

    return {
      rendering: { backend: "webgpu" },
      gpu: gpu
        ? {
            index: gpu.index ?? 0,
            display_name: gpu.display_name ?? "GPU",
            utilization_percent: gpu.utilization_percent ?? 0,
            memory_percent: gpu.memory_percent ?? 0,
            loaded: [],
          }
        : null,
      workers: running.map((job) => ({ stage: job.phase, job })),
      slots: ["extraction", "transcription", "translation"].map((phase) => ({
        stage: phase,
        job: running.find((job) => job.phase === phase) || null,
      })),
      queue: queue.slice(0, LIMIT),
      queue_rest: Math.max(0, (counts.waiting || queue.length) - LIMIT),
      blocked: blocked.slice(0, LIMIT),
      blocked_rest: Math.max(0, (counts.blocked || blocked.length) - LIMIT),
      failed: failed.slice(0, LIMIT),
      failed_rest: Math.max(0, (counts.failed || failed.length) - LIMIT),
      paused: paused.slice(0, LIMIT),
      paused_rest: Math.max(0, (counts.paused || paused.length) - LIMIT),
      stopped: stopped.slice(0, LIMIT),
      stopped_rest: Math.max(0, (counts.stopped || stopped.length) - LIMIT),
      completed: completed.slice(0, LIMIT),
      completed_total: counts.done || completed.length,
      // 미디어 트리 집계는 백엔드가 주지 않는다. 지어내지 않고 비워 둔다.
      media_tree: [],
      media_tree_rest: 0,
      state_counts: counts,
    };
  };

  const paint = (counts) => {
    for (const node of document.querySelectorAll("[data-count]")) {
      node.textContent = String(counts[node.dataset.count] ?? 0);
    }
  };

  const load = async () => {
    const response = await fetch("/api/v1/dashboard", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`dashboard ${response.status}`);
    const payload = await response.json();
    window.__SCENE_DATA__ = build(payload);
    paint(payload.state_counts || {});
  };

  // 씬 스크립트는 모듈이라 이 스크립트 이후에 실행된다. 먼저 채워 둔다.
  window.__SCENE_DATA_READY__ = load().catch((reason) => {
    window.__SCENE_DATA__ = build({ recent_jobs: [], state_counts: {}, gpu: null });
    console.error("scene data load failed", reason);
  });
})();
