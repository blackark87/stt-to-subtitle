(() => {
  const overlay = document.querySelector("[data-media-loading]");
  if (!overlay) {
    return;
  }
  const message = overlay.querySelector("[data-loading-message]");
  let searchSubmitting = false;

  const showOverlay = (text) => {
    if (message) {
      message.textContent = text;
    }
    overlay.hidden = false;
    overlay.setAttribute("aria-busy", "true");
    document.body.classList.add("is-media-loading");
  };

  const hideOverlay = () => {
    searchSubmitting = false;
    overlay.hidden = true;
    overlay.setAttribute("aria-busy", "false");
    document.body.classList.remove("is-media-loading");
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
      showOverlay("폴더 내용을 불러오는 중...");
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          window.location.assign(link.href);
        });
      });
    });
  }

  const searchForm = document.querySelector("[data-media-search]");
  searchForm?.addEventListener("submit", (event) => {
    if (searchSubmitting) {
      return;
    }
    event.preventDefault();
    searchSubmitting = true;
    showOverlay("미디어 제목을 검색하는 중...");
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        searchForm.requestSubmit();
      });
    });
  });

  window.addEventListener("pageshow", hideOverlay);
})();
