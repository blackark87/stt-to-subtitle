(() => {
  const selectedJobIds = new Set();
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
