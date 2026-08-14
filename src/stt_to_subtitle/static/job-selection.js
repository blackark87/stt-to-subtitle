(() => {
  const selectedJobIds = new Set();
  const selectedPrompts = new Map();

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
