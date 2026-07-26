(() => {
  const overlay = document.querySelector("[data-folder-loading]");
  if (!overlay) {
    return;
  }

  const hideOverlay = () => {
    overlay.hidden = true;
    document.body.classList.remove("is-folder-loading");
  };

  for (const link of document.querySelectorAll("[data-folder-link]")) {
    link.addEventListener("click", (event) => {
      if (
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey ||
        link.target === "_blank"
      ) {
        return;
      }
      event.preventDefault();
      overlay.hidden = false;
      document.body.classList.add("is-folder-loading");
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          window.location.assign(link.href);
        });
      });
    });
  }

  window.addEventListener("pageshow", hideOverlay);
})();
