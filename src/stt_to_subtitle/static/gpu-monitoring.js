document.addEventListener("DOMContentLoaded", () => {
  const target = document.querySelector("[data-gpu-update-url]");
  if (!target) {
    return;
  }

  const refreshMilliseconds = Math.max(
    5000,
    Number(target.dataset.gpuRefreshMilliseconds || 10000),
  );
  let timeoutId = null;
  let refreshing = false;

  const schedule = () => {
    window.clearTimeout(timeoutId);
    timeoutId = window.setTimeout(refresh, refreshMilliseconds);
  };

  const refresh = async () => {
    if (refreshing || document.hidden) {
      schedule();
      return;
    }
    refreshing = true;
    try {
      const response = await window.fetch(target.dataset.gpuUpdateUrl, {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (response.ok) {
        target.innerHTML = await response.text();
      }
    } catch (_) {
      // Preserve the last successful snapshot and retry on the next interval.
    } finally {
      refreshing = false;
      schedule();
    }
  };

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refresh();
    }
  });
  schedule();
});
