(() => {
  const formatTime = (value) => {
    if (!Number.isFinite(value) || value < 0) {
      return "0:00";
    }
    const totalSeconds = Math.floor(value);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, "0")}:${String(
        seconds
      ).padStart(2, "0")}`;
    }
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  };

  const showMessage = (messageElement, message) => {
    if (!messageElement) {
      return;
    }
    messageElement.textContent = message;
    messageElement.hidden = false;
  };

  const stopPlayer = (video, shell, messageElement, message, stopVR) => {
    if (video.dataset.playbackStopped === "true") {
      return;
    }
    video.dataset.playbackStopped = "true";
    stopVR();
    video.pause();
    video.querySelectorAll("source, track").forEach((element) => {
      element.removeAttribute("src");
      element.remove();
    });
    video.removeAttribute("src");
    video.load();
    video.hidden = true;
    shell.querySelectorAll("[data-player-mode]").forEach((button) => {
      button.disabled = true;
    });
    showMessage(messageElement, message);
  };

  const initialize = (root = document) => {
    for (const video of root.querySelectorAll("[data-result-player]")) {
      if (video.dataset.playerInitialized === "true") {
        continue;
      }
      video.dataset.playerInitialized = "true";

      const shell = video.closest("[data-result-player-shell]");
      if (!shell) {
        continue;
      }
      const stage = shell.querySelector("[data-result-player-stage]");
      const canvas = shell.querySelector("[data-vr180-canvas]");
      const vrControls = shell.querySelector("[data-vr180-controls]");
      const subtitleOverlay = shell.querySelector("[data-vr180-subtitles]");
      const hint = shell.querySelector("[data-vr180-hint]");
      const messageElement = shell.querySelector("[data-playback-message]");
      const flatButton = shell.querySelector('[data-player-mode="flat"]');
      const vrButton = shell.querySelector('[data-player-mode="vr180"]');
      const playButton = shell.querySelector("[data-vr-play]");
      const seek = shell.querySelector("[data-vr-seek]");
      const time = shell.querySelector("[data-vr-time]");
      const muteButton = shell.querySelector("[data-vr-mute]");
      const volume = shell.querySelector("[data-vr-volume]");
      const resetButton = shell.querySelector("[data-vr-reset]");
      const fullscreenButton = shell.querySelector("[data-vr-fullscreen]");
      const eyeButtons = shell.querySelectorAll("[data-vr-eye]");

      if (
        !stage ||
        !canvas ||
        !vrControls ||
        !subtitleOverlay ||
        !flatButton ||
        !vrButton
      ) {
        continue;
      }

      let mode = "flat";
      let renderer = null;
      let textTrack = null;
      let eyeMode = "dual";

      const updateModeButtons = () => {
        flatButton.setAttribute(
          "aria-pressed",
          mode === "flat" ? "true" : "false"
        );
        vrButton.setAttribute(
          "aria-pressed",
          mode === "vr180" ? "true" : "false"
        );
      };

      const updateSubtitles = () => {
        subtitleOverlay.replaceChildren();
        if (mode !== "vr180" || !textTrack?.activeCues?.length) {
          subtitleOverlay.hidden = true;
          return;
        }

        const cues = Array.from(textTrack.activeCues);
        const appendCues = (container) => {
          for (const cue of cues) {
            const row = document.createElement("div");
            row.className = "vr180-subtitle-line";
            if (typeof cue.getCueAsHTML === "function") {
              row.append(cue.getCueAsHTML());
            } else {
              row.textContent = cue.text;
            }
            container.append(row);
          }
        };
        subtitleOverlay.classList.toggle(
          "is-dual-eye",
          eyeMode === "dual"
        );
        if (eyeMode === "dual") {
          for (const eye of ["left", "right"]) {
            const eyePanel = document.createElement("div");
            eyePanel.className = `vr180-subtitle-eye is-${eye}`;
            eyePanel.setAttribute("aria-hidden", eye === "right" ? "true" : "false");
            appendCues(eyePanel);
            subtitleOverlay.append(eyePanel);
          }
        } else {
          appendCues(subtitleOverlay);
        }
        subtitleOverlay.hidden = false;
      };

      const setTrackMode = (nextMode) => {
        if (!textTrack) {
          return;
        }
        textTrack.mode = nextMode === "vr180" ? "hidden" : "showing";
        updateSubtitles();
      };

      const updatePlaybackControls = () => {
        if (playButton) {
          playButton.textContent = video.paused ? "재생" : "일시정지";
        }
        if (seek) {
          seek.max = Number.isFinite(video.duration)
            ? String(video.duration)
            : "0";
          if (!seek.matches(":active")) {
            seek.value = String(
              Math.min(video.currentTime || 0, Number(seek.max) || 0)
            );
          }
        }
        if (time) {
          time.textContent = `${formatTime(video.currentTime)} / ${formatTime(
            video.duration
          )}`;
        }
        if (muteButton) {
          muteButton.textContent = video.muted ? "음소거 해제" : "음소거";
          muteButton.setAttribute(
            "aria-pressed",
            video.muted ? "true" : "false"
          );
        }
        if (volume && !volume.matches(":active")) {
          volume.value = String(video.volume);
        }
      };

      const togglePlayback = () => {
        if (video.dataset.playbackStopped === "true") {
          return;
        }
        if (!video.paused) {
          video.pause();
          return;
        }
        video.play().catch(() => {
          showMessage(
            messageElement,
            "브라우저가 재생 요청을 허용하지 않았습니다. 다시 눌러 재생해 주세요."
          );
        });
      };

      const activateFlatMode = () => {
        mode = "flat";
        renderer?.stop();
        stage.classList.remove("is-vr180");
        canvas.hidden = true;
        vrControls.hidden = true;
        subtitleOverlay.hidden = true;
        if (hint) {
          hint.hidden = true;
        }
        video.hidden = video.dataset.playbackStopped === "true";
        video.controls = true;
        setTrackMode("flat");
        updateModeButtons();
      };

      const handleVRFailure = (message) => {
        activateFlatMode();
        vrButton.disabled = true;
        showMessage(messageElement, message);
      };

      const activateVRMode = () => {
        if (video.dataset.playbackStopped === "true") {
          return;
        }
        if (!renderer) {
          try {
            if (typeof window.createVR180Renderer !== "function") {
              throw new Error("VR 180 렌더러를 불러오지 못했습니다.");
            }
            renderer = window.createVR180Renderer({
              video,
              canvas,
              onContextLost: () => {
                handleVRFailure(
                  "WebGL 연결이 끊겨 일반 플레이어로 전환했습니다. VR 모드는 자동으로 재시도하지 않습니다."
                );
              },
            });
          } catch (error) {
            handleVRFailure(
              `VR 180 모드를 시작할 수 없습니다: ${
                error?.message || "WebGL 초기화 오류"
              }`
            );
            return;
          }
        }

        mode = "vr180";
        video.controls = false;
        video.hidden = true;
        stage.classList.add("is-vr180");
        canvas.hidden = false;
        vrControls.hidden = false;
        if (hint) {
          hint.hidden = false;
        }
        setTrackMode("vr180");
        updateModeButtons();
        updatePlaybackControls();
        renderer.setEye(eyeMode);
        renderer.start();
        canvas.focus({ preventScroll: true });
      };

      const mimeType =
        video.dataset.videoType || "application/octet-stream";
      const support = video.canPlayType(mimeType);
      if (support !== "maybe" && support !== "probably") {
        stopPlayer(
          video,
          shell,
          messageElement,
          `이 브라우저가 ${mimeType} 형식을 지원하지 않아 스트리밍을 시작하지 않았습니다.`,
          () => renderer?.stop()
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
          shell,
          messageElement,
          "브라우저가 이 영상의 코덱을 재생할 수 없어 스트리밍을 중단했습니다. 자동으로 재시도하지 않습니다.",
          () => renderer?.stop()
        );
      };
      source.addEventListener("error", stopAfterDecodeError, { once: true });
      video.addEventListener("error", stopAfterDecodeError, { once: true });
      track.addEventListener("load", () => {
        textTrack = track.track;
        textTrack.addEventListener("cuechange", updateSubtitles);
        setTrackMode(mode);
      });
      video.append(source, track);
      video.load();

      flatButton.addEventListener("click", () => {
        activateFlatMode();
        video.focus({ preventScroll: true });
      });
      vrButton.addEventListener("click", activateVRMode);
      playButton?.addEventListener("click", togglePlayback);
      shell.addEventListener("keydown", (event) => {
        if (
          event.altKey ||
          event.ctrlKey ||
          event.metaKey ||
          event.shiftKey ||
          event.target instanceof HTMLInputElement ||
          event.target instanceof HTMLSelectElement ||
          event.target instanceof HTMLTextAreaElement
        ) {
          return;
        }
        if (event.key === " " || event.code === "Space") {
          if (event.target instanceof HTMLButtonElement) {
            return;
          }
          event.preventDefault();
          if (!event.repeat) {
            togglePlayback();
          }
          return;
        }
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          const change = event.key === "ArrowLeft" ? -10 : 10;
          const maximum = Number.isFinite(video.duration)
            ? video.duration
            : Number.POSITIVE_INFINITY;
          video.currentTime = Math.min(
            maximum,
            Math.max(0, video.currentTime + change)
          );
          renderer?.requestDraw();
          updatePlaybackControls();
          return;
        }
        if (event.key === "ArrowUp" || event.key === "ArrowDown") {
          event.preventDefault();
          const change = event.key === "ArrowUp" ? 0.05 : -0.05;
          video.volume = Math.min(
            1,
            Math.max(0, Number((video.volume + change).toFixed(2)))
          );
          video.muted = video.volume === 0;
          updatePlaybackControls();
        }
      });
      seek?.addEventListener("input", () => {
        if (Number.isFinite(video.duration)) {
          video.currentTime = Number(seek.value);
          renderer?.requestDraw();
        }
      });
      muteButton?.addEventListener("click", () => {
        video.muted = !video.muted;
        updatePlaybackControls();
      });
      volume?.addEventListener("input", () => {
        video.volume = Math.min(1, Math.max(0, Number(volume.value)));
        video.muted = video.volume === 0;
        updatePlaybackControls();
      });
      resetButton?.addEventListener("click", () => {
        renderer?.reset();
        canvas.focus({ preventScroll: true });
      });
      fullscreenButton?.addEventListener("click", () => {
        if (document.fullscreenElement === shell) {
          document.exitFullscreen?.();
          return;
        }
        const fullscreenRequest = shell.requestFullscreen?.();
        fullscreenRequest?.catch(() => {
          showMessage(
            messageElement,
            "이 브라우저에서는 전체 화면을 시작할 수 없습니다."
          );
        });
      });
      for (const eyeButton of eyeButtons) {
        eyeButton.addEventListener("click", () => {
          const eye = eyeButton.dataset.vrEye;
          eyeMode = eye || "left";
          renderer?.setEye(eye);
          for (const candidate of eyeButtons) {
            candidate.setAttribute(
              "aria-pressed",
              candidate === eyeButton ? "true" : "false"
            );
          }
          updateSubtitles();
        });
      }
      for (const eventName of [
        "durationchange",
        "ended",
        "loadedmetadata",
        "pause",
        "play",
        "timeupdate",
        "volumechange",
      ]) {
        video.addEventListener(eventName, updatePlaybackControls);
      }
      document.addEventListener("fullscreenchange", () => {
        if (fullscreenButton) {
          fullscreenButton.textContent =
            document.fullscreenElement === shell
              ? "전체 화면 종료"
              : "전체 화면";
        }
        renderer?.requestDraw();
      });
      updatePlaybackControls();
    }
  };

  window.initializeResultPlayers = initialize;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initialize());
  } else {
    initialize();
  }
})();
