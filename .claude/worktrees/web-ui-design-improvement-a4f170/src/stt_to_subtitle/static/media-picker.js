(() => {
  const picker = document.querySelector(".media-picker");
  if (!picker) {
    return;
  }

  const checkboxes = Array.from(
    picker.querySelectorAll(".media-card-checkbox")
  );
  const count = picker.querySelector("[data-selection-count]");
  const submits = Array.from(
    document.querySelectorAll("[data-batch-submit]")
  );
  const promptCategory = document.querySelector("[data-prompt-category]");

  const update = () => {
    const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
    for (const checkbox of checkboxes) {
      checkbox.closest(".media-card")?.classList.toggle(
        "is-selected",
        checkbox.checked
      );
    }
    if (count) {
      count.textContent = String(selected);
    }
    for (const submit of submits) {
      const missingServers =
        submit.hasAttribute("data-requires-servers") &&
        submit.dataset.serverConfigured !== "true";
      const missingPrompt =
        submit.value !== "extract" && !promptCategory?.value;
      submit.disabled = selected === 0 || missingServers || missingPrompt;
    }
  };

  for (const checkbox of checkboxes) {
    checkbox.addEventListener("change", update);
  }
  promptCategory?.addEventListener("change", update);

  picker.querySelector("[data-select-all]")?.addEventListener("click", () => {
    for (const checkbox of checkboxes) {
      checkbox.checked = true;
    }
    update();
  });

  picker
    .querySelector("[data-clear-selection]")
    ?.addEventListener("click", () => {
      for (const checkbox of checkboxes) {
        checkbox.checked = false;
      }
      update();
    });

  update();
})();
