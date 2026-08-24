(() => {
  const selectedJobIds = new Set();
  const selectedStopJobIds = new Set();
  const selectedRetryJobIds = new Set();
  const selectedPrompts = new Map();
  const selectedComparisonJobs = new Map();

  const formsIn = (root) => {
    const forms = Array.from(
      root.querySelectorAll?.("[data-job-translation-form]") || [],
    );
    if (root.matches?.("[data-job-translation-form]")) {
      forms.unshift(root);
    }
    return forms;
  };

  const checkboxesFor = (form) => Array.from(
    document.querySelectorAll(
      `[data-translation-job-checkbox][form="${form.id}"]`,
    ),
  );

  const comparisonFormsIn = (root) => {
    const forms = Array.from(
      root.querySelectorAll?.("[data-comparison-translation-form]") || [],
    );
    if (root.matches?.("[data-comparison-translation-form]")) {
      forms.unshift(root);
    }
    return forms;
  };

  const stopFormsIn = (root) => {
    const forms = Array.from(
      root.querySelectorAll?.("[data-job-stop-form]") || [],
    );
    if (root.matches?.("[data-job-stop-form]")) {
      forms.unshift(root);
    }
    return forms;
  };

  const stopCheckboxesFor = (form) => Array.from(
    document.querySelectorAll(`[data-stop-job-checkbox][form="${form.id}"]`),
  );

  const stopCandidateIdsFor = (form) => new Set(
    Array.from(form.querySelectorAll("[data-stop-job-candidate]"))
      .map((input) => input.value),
  );

  const retryFormsIn = (root) => {
    const forms = Array.from(
      root.querySelectorAll?.("[data-job-retry-form]") || [],
    );
    if (root.matches?.("[data-job-retry-form]")) {
      forms.unshift(root);
    }
    return forms;
  };

  const retryCheckboxesFor = (form) => Array.from(
    document.querySelectorAll(`[data-retry-job-checkbox][form="${form.id}"]`),
  );

  const retryCandidateIdsFor = (form) => new Set(
    Array.from(form.querySelectorAll("[data-retry-job-candidate]"))
      .map((input) => input.value),
  );

  const syncRetryForm = (form) => {
    const candidateIds = retryCandidateIdsFor(form);
    for (const jobId of selectedRetryJobIds) {
      if (!candidateIds.has(jobId)) {
        selectedRetryJobIds.delete(jobId);
      }
    }
    for (const checkbox of retryCheckboxesFor(form)) {
      checkbox.checked = selectedRetryJobIds.has(checkbox.value);
    }
    const selectedIds = Array.from(selectedRetryJobIds)
      .filter((jobId) => candidateIds.has(jobId));
    const inputs = form.querySelector("[data-retry-selected-inputs]");
    if (inputs) {
      inputs.replaceChildren(...selectedIds.map((jobId) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "job_ids";
        input.value = jobId;
        return input;
      }));
    }
    const count = form.querySelector("[data-retry-selection-count]");
    if (count) {
      count.textContent = String(selectedIds.length);
    }
    const submit = form.querySelector("[data-retry-selected]");
    if (submit) {
      submit.disabled = selectedIds.length === 0;
    }
  };

  const syncStopForm = (form) => {
    const candidateIds = stopCandidateIdsFor(form);
    for (const jobId of selectedStopJobIds) {
      if (!candidateIds.has(jobId)) {
        selectedStopJobIds.delete(jobId);
      }
    }
    for (const checkbox of stopCheckboxesFor(form)) {
      checkbox.checked = selectedStopJobIds.has(checkbox.value);
    }
    const selectedIds = Array.from(selectedStopJobIds)
      .filter((jobId) => candidateIds.has(jobId));
    const inputs = form.querySelector("[data-stop-selected-inputs]");
    if (inputs) {
      inputs.replaceChildren(...selectedIds.map((jobId) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "job_ids";
        input.value = jobId;
        return input;
      }));
    }
    const count = form.querySelector("[data-stop-selection-count]");
    if (count) {
      count.textContent = String(selectedIds.length);
    }
    const submit = form.querySelector("[data-stop-selected]");
    if (submit) {
      submit.disabled = selectedIds.length === 0;
    }
  };

  const comparisonSelectsFor = (form) => Array.from(
    form.querySelectorAll("[data-comparison-translation-source]"),
  );

  const comparisonSelectionKey = (form, select) => (
    `${form.id}\u0000${select.dataset.comparisonTranslationSource}`
  );

  const syncForm = (form) => {
    const checkboxes = checkboxesFor(form);
    for (const checkbox of checkboxes) {
      checkbox.checked = selectedJobIds.has(checkbox.value);
    }
    const selectedCount = checkboxes.filter((checkbox) => checkbox.checked).length;
    const count = form.querySelector("[data-job-selection-count]");
    const prompt = form.querySelector("[data-translation-prompt]");
    const submit = form.querySelector("[data-translate-selected]");
    if (count) {
      count.textContent = String(selectedCount);
    }
    if (submit) {
      submit.disabled = selectedCount === 0 || !prompt?.value;
    }
  };

  const syncComparisonForm = (form) => {
    const selects = comparisonSelectsFor(form);
    for (const select of selects) {
      const key = comparisonSelectionKey(form, select);
      const savedJobId = selectedComparisonJobs.get(key);
      if (
        savedJobId &&
        Array.from(select.options).some((option) => option.value === savedJobId)
      ) {
        select.value = savedJobId;
      } else if (savedJobId) {
        selectedComparisonJobs.delete(key);
      }
    }
    const prompt = form.querySelector("[data-comparison-translation-prompt]");
    const savedPrompt = selectedPrompts.get(form.id);
    if (
      prompt &&
      savedPrompt &&
      Array.from(prompt.options).some((option) => option.value === savedPrompt)
    ) {
      prompt.value = savedPrompt;
    }
    const submit = form.querySelector("[data-translate-comparison]");
    if (submit) {
      submit.disabled = (
        !selects.length ||
        selects.some((select) => !select.value) ||
        !prompt?.value
      );
    }
  };

  const initialize = (root = document) => {
    for (const form of formsIn(root)) {
      const prompt = form.querySelector("[data-translation-prompt]");
      const savedPrompt = selectedPrompts.get(form.id);
      if (
        prompt &&
        savedPrompt &&
        Array.from(prompt.options).some((option) => option.value === savedPrompt)
      ) {
        prompt.value = savedPrompt;
      }
      syncForm(form);
    }
    for (const form of comparisonFormsIn(root)) {
      syncComparisonForm(form);
    }
    for (const form of stopFormsIn(root)) {
      syncStopForm(form);
    }
    for (const form of retryFormsIn(root)) {
      syncRetryForm(form);
    }
  };

  document.addEventListener("change", (event) => {
    const checkbox = event.target.closest?.("[data-translation-job-checkbox]");
    if (checkbox) {
      if (checkbox.checked) {
        selectedJobIds.add(checkbox.value);
      } else {
        selectedJobIds.delete(checkbox.value);
      }
      const form = document.getElementById(checkbox.getAttribute("form"));
      if (form) {
        syncForm(form);
      }
      return;
    }

    const stopCheckbox = event.target.closest?.("[data-stop-job-checkbox]");
    if (stopCheckbox) {
      if (stopCheckbox.checked) {
        selectedStopJobIds.add(stopCheckbox.value);
      } else {
        selectedStopJobIds.delete(stopCheckbox.value);
      }
      const form = document.getElementById(stopCheckbox.getAttribute("form"));
      if (form) {
        syncStopForm(form);
      }
      return;
    }

    const retryCheckbox = event.target.closest?.("[data-retry-job-checkbox]");
    if (retryCheckbox) {
      if (retryCheckbox.checked) {
        selectedRetryJobIds.add(retryCheckbox.value);
      } else {
        selectedRetryJobIds.delete(retryCheckbox.value);
      }
      const form = document.getElementById(retryCheckbox.getAttribute("form"));
      if (form) {
        syncRetryForm(form);
      }
      return;
    }

    const prompt = event.target.closest?.("[data-translation-prompt]");
    if (prompt) {
      const form = prompt.closest("[data-job-translation-form]");
      if (form) {
        selectedPrompts.set(form.id, prompt.value);
        syncForm(form);
      }
      return;
    }

    const comparisonSelect = event.target.closest?.(
      "[data-comparison-translation-source]",
    );
    if (comparisonSelect) {
      const form = comparisonSelect.closest(
        "[data-comparison-translation-form]",
      );
      if (form) {
        selectedComparisonJobs.set(
          comparisonSelectionKey(form, comparisonSelect),
          comparisonSelect.value,
        );
        syncComparisonForm(form);
      }
      return;
    }

    const comparisonPrompt = event.target.closest?.(
      "[data-comparison-translation-prompt]",
    );
    if (comparisonPrompt) {
      const form = comparisonPrompt.closest(
        "[data-comparison-translation-form]",
      );
      if (form) {
        selectedPrompts.set(form.id, comparisonPrompt.value);
        syncComparisonForm(form);
      }
    }
  });

  document.addEventListener("click", (event) => {
    const selectAllRetries = event.target.closest?.("[data-select-retry-jobs]");
    const clearRetries = event.target.closest?.("[data-clear-retry-jobs]");
    const retryControl = selectAllRetries || clearRetries;
    if (retryControl) {
      const form = retryControl.closest("[data-job-retry-form]");
      if (!form) {
        return;
      }
      for (const jobId of retryCandidateIdsFor(form)) {
        if (selectAllRetries) {
          selectedRetryJobIds.add(jobId);
        } else {
          selectedRetryJobIds.delete(jobId);
        }
      }
      syncRetryForm(form);
      return;
    }

    const selectAllStops = event.target.closest?.("[data-select-stop-jobs]");
    const clearStops = event.target.closest?.("[data-clear-stop-jobs]");
    const stopControl = selectAllStops || clearStops;
    if (stopControl) {
      const form = stopControl.closest("[data-job-stop-form]");
      if (!form) {
        return;
      }
      for (const jobId of stopCandidateIdsFor(form)) {
        if (selectAllStops) {
          selectedStopJobIds.add(jobId);
        } else {
          selectedStopJobIds.delete(jobId);
        }
      }
      syncStopForm(form);
      return;
    }

    const selectAll = event.target.closest?.("[data-select-translation-jobs]");
    const clear = event.target.closest?.("[data-clear-translation-jobs]");
    const control = selectAll || clear;
    if (!control) {
      return;
    }
    const form = control.closest("[data-job-translation-form]");
    if (!form) {
      return;
    }
    for (const checkbox of checkboxesFor(form)) {
      checkbox.checked = Boolean(selectAll);
      if (selectAll) {
        selectedJobIds.add(checkbox.value);
      } else {
        selectedJobIds.delete(checkbox.value);
      }
    }
    syncForm(form);
  });

  window.initializeJobSelection = initialize;
  document.addEventListener("DOMContentLoaded", () => initialize());
})();
