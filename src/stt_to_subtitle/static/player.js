(() => {
  const stopPlayer = (video, messageElement, message) => {
    if (video.dataset.playbackStopped === "true") {
      return;
    }
    video.dataset.playbackStopped = "true";
    video.pause();
    video.querySelectorAll("source, track").forEach((element) => {
      element.removeAttribute("src");
      element.remove();
    });
    video.removeAttribute("src");
    video.load();
    video.hidden = true;
    if (messageElement) {
      messageElement.textContent = message;
      messageElement.hidden = false;
    }
  };

  const initialize = (root = document) => {
    for (const video of root.querySelectorAll("[data-result-player]")) {
      if (video.dataset.playerInitialized === "true") {
        continue;
      }
      video.dataset.playerInitialized = "true";
      const messageElement = video.parentElement?.querySelector(
        "[data-playback-message]"
      );
      const mimeType = video.dataset.videoType || "application/octet-stream";
      const support = video.canPlayType(mimeType);
      if (support !== "maybe" && support !== "probably") {
        stopPlayer(
          video,
          messageElement,
          `이 브라우저가 ${mimeType} 형식을 지원하지 않아 스트리밍을 시작하지 않았습니다.`
        );
        continue;
      }

      const source = document.createElement("source");
      source.src = video.dataset.videoSrc;
      source.type = mimeType;
      const track = document.createElement("track");
      track.kind = "subtitles";
      track.src = video.dataset.subtitleSrc;
      track.srclang = "ko";
      track.label = "한국어";
      track.default = true;

      const stopAfterDecodeError = () => {
        stopPlayer(
          video,
          messageElement,
          "브라우저가 이 영상의 코덱을 재생할 수 없어 스트리밍을 중단했습니다. 자동으로 재시도하지 않습니다."
        );
      };
      source.addEventListener("error", stopAfterDecodeError, { once: true });
      video.addEventListener("error", stopAfterDecodeError, { once: true });
      video.append(source, track);
      video.load();
    }
  };

  window.initializeResultPlayers = initialize;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initialize());
  } else {
    initialize();
  }
})();
