document.addEventListener("DOMContentLoaded", () => {
  for (const target of document.querySelectorAll("[data-poll-url]")) {
    const seconds = Number(target.dataset.pollSeconds || "5");
    const interval = window.setInterval(async () => {
      try {
        const response = await window.fetch(target.dataset.pollUrl, {
          credentials: "same-origin",
          headers: { Accept: "text/html" },
        });
        if (response.ok) {
          target.innerHTML = await response.text();
          if (target.querySelector("[data-poll-stop]")) {
            window.clearInterval(interval);
          }
        }
      } catch (_) {
        // Keep the last rendered state; the next interval is a fresh attempt.
      }
    }, Math.max(1, seconds) * 1000);
  }
});
