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
        ["translate", "full"].includes(submit.value) &&
        !promptCategory?.value;
      submit.disabled = selected === 0 || missingServers || missingPrompt;
    }
  };

  for (const checkbox of checkboxes) {
    checkbox.addEventListener("change", update);
  }
  promptCategory?.addEventListener("change", update);

  picker.querySelector("[data-select-all]")?.addEventListener("click", () => {
    // 이미 자막이 있는 항목은 담지 않는다. 다시 번역하려면 직접 고른다.
    for (const checkbox of checkboxes) {
      checkbox.checked = checkbox.hasAttribute("data-auto-select");
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
