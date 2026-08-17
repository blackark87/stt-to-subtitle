document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-server-settings]");
  if (!form) return;

  const baseURL = form.querySelector('[name="lm_base_url"]');
  const token = form.querySelector('[name="lm_token"]');
  const clearToken = form.querySelector('[name="clear_lm_token"]');
  const csrfToken = form.querySelector('[name="csrf_token"]');
  const select = form.querySelector("[data-model-select]");
  const refresh = form.querySelector("[data-model-refresh]");
  const status = form.querySelector("[data-model-status]");

  if (!baseURL || !select || !refresh || !status) return;

  const showStatus = (message, invalid = false) => {
    status.textContent = message;
    status.classList.toggle("error-text", invalid);
  };

  const replaceModels = (models) => {
    const selected = select.value || select.dataset.savedModel || "";
    select.replaceChildren();

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = models.length
      ? "번역 모델을 선택하세요"
      : "조회된 모델이 없습니다";
    placeholder.disabled = true;
    placeholder.selected = true;
    select.append(placeholder);

    for (const model of models) {
      const option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      select.append(option);
    }

    if (models.includes(selected)) {
      select.value = selected;
    }
  };

  const loadModels = async () => {
    if (!baseURL.value.trim()) {
      showStatus("OpenAI 호환 API 주소를 먼저 입력하세요.", true);
      baseURL.focus();
      return;
    }

    refresh.disabled = true;
    showStatus("모델 목록을 조회하고 있습니다.");
    const payload = new FormData();
    payload.set("csrf_token", csrfToken?.value || "");
    payload.set("lm_base_url", baseURL.value);
    payload.set("lm_token", token?.value || "");
    if (clearToken?.checked) {
      payload.set("clear_lm_token", "true");
    }

    try {
      const response = await window.fetch(form.dataset.modelsUrl, {
        method: "POST",
        credentials: "same-origin",
        body: payload,
        headers: { Accept: "application/json" },
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(result.detail || `HTTP ${response.status}`);
      }
      const models = Array.isArray(result.models) ? result.models : [];
      replaceModels(models);
      showStatus(
        models.length
          ? `모델 ${models.length}개를 조회했습니다.`
          : "서버가 조회 가능한 모델을 반환하지 않았습니다.",
        models.length === 0
      );
    } catch (error) {
      showStatus(`모델 조회 실패: ${error.message}`, true);
    } finally {
      refresh.disabled = false;
    }
  };

  refresh.addEventListener("click", loadModels);
  if (baseURL.value.trim()) {
    loadModels();
  }
});
