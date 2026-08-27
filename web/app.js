const stateLabels = {
  waiting: "대기",
  running: "진행 중",
  paused: "일시 정지",
  blocked: "중단",
  stopped: "정지",
  failed: "실패",
  done: "완료",
};

const phaseLabels = {
  extraction: "추출",
  transcription: "전사",
  translation: "번역",
  render: "자막 생성",
  complete: "완료",
};

const summaryOrder = ["running", "waiting", "blocked", "failed", "paused", "stopped", "done"];
const jobsBody = document.querySelector("#jobs");
const summary = document.querySelector("#summary");
const connection = document.querySelector("#connection");
const error = document.querySelector("#error");
const runtimesBody = document.querySelector("#runtimes");
const runtimeForm = document.querySelector("#runtime-form");
let runtimeItems = [];
let editingRuntimeId = null;

const runtimeStatusLabels = {
  ready: "사용 가능",
  checking: "확인 중",
  unknown: "확인 필요",
  unavailable: "연결 불가",
  disabled: "사용 안 함",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);
}

function fileName(path) {
  return String(path ?? "").split("/").pop() || "-";
}

function renderSummary(jobs) {
  const counts = Object.fromEntries(summaryOrder.map((state) => [state, 0]));
  for (const job of jobs) {
    if (job.state in counts) counts[job.state] += 1;
  }
  summary.innerHTML = summaryOrder.map((state) => `
    <article class="metric">
      <span>${stateLabels[state]}</span>
      <strong>${counts[state]}</strong>
    </article>
  `).join("");
}

function renderJobs(jobs) {
  const recent = jobs.slice(0, 20);
  if (!recent.length) {
    jobsBody.innerHTML = '<tr><td colspan="5">등록된 작업 없음</td></tr>';
    return;
  }
  const runtimeNames = new Map(
    runtimeItems.map((runtime) => [runtime.id, runtime.name]),
  );
  jobsBody.innerHTML = recent.map((job) => `
    <tr>
      <td title="${escapeHtml(job.source_rel)}">${escapeHtml(fileName(job.source_rel))}</td>
      <td>${escapeHtml(phaseLabels[job.phase] || job.phase)}</td>
      <td>${escapeHtml(stateLabels[job.state] || job.state)}</td>
      <td>${escapeHtml(runtimeNames.get(job.stt_runtime_id) || job.stt_runtime_id || "-")}</td>
      <td>${escapeHtml(job.updated_at || "-")}</td>
    </tr>
  `).join("");
}

function renderRuntimes(items) {
  runtimeItems = items;
  if (!items.length) {
    runtimesBody.innerHTML = '<tr><td colspan="5">등록된 Runtime 없음</td></tr>';
    return;
  }
  runtimesBody.innerHTML = items.map((runtime) => `
    <tr>
      <td>${escapeHtml(runtime.name)}${runtime.builtin ? ' <small>기본</small>' : ""}</td>
      <td><span class="runtime-state ${escapeHtml(runtime.status)}">${escapeHtml(runtimeStatusLabels[runtime.status] || runtime.status)}</span></td>
      <td title="${escapeHtml(runtime.base_url)}">${escapeHtml(runtime.base_url)}</td>
      <td>${escapeHtml(runtime.running_jobs)} / ${escapeHtml(runtime.capacity)}</td>
      <td class="actions" data-runtime-id="${escapeHtml(runtime.id)}">
        <button type="button" data-action="probe">확인</button>
        ${runtime.builtin ? "" : `
          <button type="button" data-action="toggle">${runtime.enabled ? "사용 중지" : "사용"}</button>
          <button type="button" data-action="edit">수정</button>
          <button type="button" data-action="delete">삭제</button>
        `}
      </td>
    </tr>
  `).join("");
}

async function apiRequest(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    let detail = `Backend 응답 ${response.status}`;
    try {
      const payload = await response.json();
      if (payload.detail) detail = payload.detail;
    } catch (_reason) {
      // Keep the HTTP status when the response has no JSON error body.
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

async function load() {
  try {
    const [jobsPayload, runtimesPayload] = await Promise.all([
      apiRequest("/api/v1/jobs?limit=20"),
      apiRequest("/api/v1/runtimes"),
    ]);
    const jobs = jobsPayload.items;
    renderRuntimes(runtimesPayload.items);
    renderSummary(jobs);
    renderJobs(jobs);
    connection.textContent = "Backend 연결됨";
    connection.className = "status ok";
    error.hidden = true;
  } catch (reason) {
    connection.textContent = "Backend 연결 실패";
    connection.className = "status bad";
    error.textContent = reason instanceof Error ? reason.message : String(reason);
    error.hidden = false;
  }
}

runtimeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = new FormData(runtimeForm);
  try {
    const current = runtimeItems.find((item) => item.id === editingRuntimeId);
    const token = String(values.get("token") || "");
    await apiRequest(
      current ? `/api/v1/runtimes/${encodeURIComponent(current.id)}` : "/api/v1/runtimes",
      {
        method: current ? "PUT" : "POST",
        body: JSON.stringify({
          name: values.get("name"),
          base_url: values.get("base_url"),
          ...(current ? { token: token || null } : { token }),
          capacity: Number(values.get("capacity")),
          enabled: current ? current.enabled : true,
        }),
      },
    );
    resetRuntimeForm();
    await load();
  } catch (reason) {
    error.textContent = reason instanceof Error ? reason.message : String(reason);
    error.hidden = false;
  }
});

function resetRuntimeForm() {
  editingRuntimeId = null;
  runtimeForm.reset();
  runtimeForm.elements.capacity.value = "1";
  document.querySelector("#runtime-submit").textContent = "Runtime 추가";
  document.querySelector("#runtime-cancel").hidden = true;
}

document.querySelector("#runtime-cancel").addEventListener("click", resetRuntimeForm);

runtimesBody.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  const actionCell = button?.closest("[data-runtime-id]");
  if (!button || !actionCell) return;
  const runtime = runtimeItems.find((item) => item.id === actionCell.dataset.runtimeId);
  if (!runtime) return;
  button.disabled = true;
  try {
    if (button.dataset.action === "probe") {
      await apiRequest(`/api/v1/runtimes/${encodeURIComponent(runtime.id)}/probe`, { method: "POST" });
    } else if (button.dataset.action === "toggle") {
      await apiRequest(`/api/v1/runtimes/${encodeURIComponent(runtime.id)}`, {
        method: "PUT",
        body: JSON.stringify({
          name: runtime.name,
          base_url: runtime.base_url,
          capacity: runtime.capacity,
          enabled: !runtime.enabled,
        }),
      });
    } else if (button.dataset.action === "edit") {
      editingRuntimeId = runtime.id;
      runtimeForm.elements.name.value = runtime.name;
      runtimeForm.elements.base_url.value = runtime.base_url;
      runtimeForm.elements.token.value = "";
      runtimeForm.elements.capacity.value = String(runtime.capacity);
      document.querySelector("#runtime-submit").textContent = "변경 저장";
      document.querySelector("#runtime-cancel").hidden = false;
      runtimeForm.elements.name.focus();
      button.disabled = false;
      return;
    } else if (button.dataset.action === "delete") {
      if (!window.confirm(`${runtime.name} Runtime을 삭제할까요?`)) {
        button.disabled = false;
        return;
      }
      await apiRequest(`/api/v1/runtimes/${encodeURIComponent(runtime.id)}`, { method: "DELETE" });
    }
    await load();
  } catch (reason) {
    error.textContent = reason instanceof Error ? reason.message : String(reason);
    error.hidden = false;
    button.disabled = false;
  }
});

document.querySelector("#refresh").addEventListener("click", load);
load();
window.setInterval(load, 10_000);
