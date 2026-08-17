document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("submit", (event) => {
    const form = event.target.closest?.("form[data-confirm-message]");
    if (form && !window.confirm(form.dataset.confirmMessage)) {
      event.preventDefault();
    }
  });

  const targets = Array.from(document.querySelectorAll("[data-update-url]"));
  if (!targets.length) {
    return;
  }
  if (!("EventSource" in window)) {
    return;
  }

  let eventSource = null;
  let refreshing = false;
  let refreshQueued = false;

  const refreshTargets = async () => {
    if (refreshing) {
      refreshQueued = true;
      return;
    }
    refreshing = true;
    try {
      for (const target of targets) {
        if (!target.dataset.updateUrl) {
          continue;
        }
        try {
          const response = await window.fetch(target.dataset.updateUrl, {
            credentials: "same-origin",
            headers: { Accept: "text/html" },
          });
          if (!response.ok) {
            continue;
          }
          target.innerHTML = await response.text();
          window.initializeResultPlayers?.(target);
          window.initializeJobSelection?.(target);
          if (target.querySelector("[data-update-stop]")) {
            delete target.dataset.updateUrl;
          }
        } catch (_) {
          // Keep the last rendered state until the next job-change event.
        }
      }
    } finally {
      refreshing = false;
      if (targets.every((target) => !target.dataset.updateUrl)) {
        eventSource?.close();
      } else if (refreshQueued) {
        refreshQueued = false;
        await refreshTargets();
      }
    }
  };

  eventSource = new window.EventSource("/jobs/events");
  eventSource.addEventListener("ready", refreshTargets);
  eventSource.addEventListener("jobs", refreshTargets);
});
