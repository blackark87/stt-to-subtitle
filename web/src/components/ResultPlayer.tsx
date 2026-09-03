"use client";

import type { RefObject } from "react";
import { useEffect, useRef } from "react";
import { api } from "@/lib/api";

declare global {
  interface Window {
    createVR180Renderer?: (...args: unknown[]) => unknown;
    initializeResultPlayers?: (root?: ParentNode) => void;
    destroyResultPlayers?: (root?: ParentNode) => void;
  }
}

interface ResultPlayerProps {
  sourceRel: string;
  subtitleJobId?: string;
  videoRef: RefObject<HTMLVideoElement | null>;
  onTimeUpdate: (currentTime: number) => void;
  onDurationChange?: (duration: number) => void;
}

const MIME_TYPES: Record<string, string> = {
  ".m4v": "video/mp4",
  ".mkv": "video/x-matroska",
  ".mov": "video/quicktime",
  ".mp4": "video/mp4",
  ".ogv": "video/ogg",
  ".webm": "video/webm",
};

function mediaMimeType(path: string): string {
  const normalized = path.toLowerCase();
  const extension = Object.keys(MIME_TYPES).find((candidate) =>
    normalized.endsWith(candidate),
  );
  return extension ? MIME_TYPES[extension] ?? "application/octet-stream" : "application/octet-stream";
}

function loadScript(id: string, source: string): Promise<void> {
  const existing = document.getElementById(id) as HTMLScriptElement | null;
  if (existing?.dataset.loaded === "true") return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = existing ?? document.createElement("script");
    const loaded = () => {
      script.dataset.loaded = "true";
      resolve();
    };
    const failed = () => reject(new Error(`${source} 스크립트를 불러오지 못했습니다.`));
    script.addEventListener("load", loaded, { once: true });
    script.addEventListener("error", failed, { once: true });
    if (!existing) {
      script.id = id;
      script.src = source;
      script.async = true;
      document.body.append(script);
    }
  });
}

async function ensureResultPlayer(): Promise<void> {
  if (typeof window.createVR180Renderer !== "function") {
    await loadScript("vr180-renderer-script", "/vr180-player.js");
  }
  if (typeof window.initializeResultPlayers !== "function") {
    await loadScript("result-player-controls-script", "/result-player.js");
  }
}

export function ResultPlayer({
  sourceRel,
  subtitleJobId = "",
  videoRef,
  onTimeUpdate,
  onDurationChange,
}: ResultPlayerProps) {
  const shellRef = useRef<HTMLDivElement | null>(null);
  const messageRef = useRef<HTMLParagraphElement | null>(null);

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    let cancelled = false;
    void ensureResultPlayer()
      .then(() => {
        if (!cancelled) window.initializeResultPlayers?.(shell);
      })
      .catch((reason) => {
        if (cancelled || !messageRef.current) return;
        messageRef.current.textContent = reason instanceof Error
          ? reason.message
          : String(reason);
        messageRef.current.hidden = false;
      });
    return () => {
      cancelled = true;
      window.destroyResultPlayers?.(shell);
    };
  }, [sourceRel, subtitleJobId]);

  return (
    <div
      className="result-player-shell"
      data-result-player-shell
      ref={shellRef}
      tabIndex={0}
      role="group"
      aria-label="결과 영상 플레이어"
      aria-keyshortcuts="Space ArrowLeft ArrowRight ArrowUp ArrowDown"
    >
      <div className="player-mode-toolbar" aria-label="미리보기 방식">
        <span>보기 방식</span>
        <button type="button" className="btn sec" data-player-mode="flat" aria-pressed="true">
          일반
        </button>
        <button type="button" className="btn sec" data-player-mode="vr180" aria-pressed="false">
          180° 단안 미리보기
        </button>
      </div>

      <div className="result-player-stage" data-result-player-stage>
        <video
          ref={videoRef}
          className="result-player"
          controls
          preload="metadata"
          playsInline
          crossOrigin="anonymous"
          data-result-player
          data-video-src={api.mediaFileUrl(sourceRel)}
          data-video-type={mediaMimeType(sourceRel)}
          data-subtitle-src={subtitleJobId ? api.subtitlesUrl(subtitleJobId) : ""}
          onTimeUpdate={(event) => onTimeUpdate(event.currentTarget.currentTime)}
          onLoadedMetadata={(event) => {
            const nextDuration = event.currentTarget.duration;
            if (Number.isFinite(nextDuration)) onDurationChange?.(nextDuration);
          }}
        />
        <canvas
          className="vr180-canvas"
          data-vr180-canvas
          tabIndex={0}
          aria-label="VR180 단안 영상. 마우스나 터치로 드래그해 시점을 이동할 수 있습니다."
          hidden
        />
        <div className="vr180-subtitles" data-vr180-subtitles aria-live="off" hidden />
        <p className="vr180-hint" data-vr180-hint hidden>
          드래그: 시점 이동 · 휠: 확대/축소 · Space: 재생/일시정지 · ←/→: 10초 이동 · ↑/↓: 음량
        </p>
      </div>

      <div className="vr180-controls" data-vr180-controls hidden>
        <button type="button" className="btn" data-vr-play>재생</button>
        <input className="vr180-seek" data-vr-seek type="range" min="0" max="0" defaultValue="0" step="0.1" aria-label="재생 위치" />
        <output data-vr-time>0:00 / 0:00</output>
        <button type="button" className="btn sec" data-vr-mute>음소거</button>
        <label className="vr180-volume-control">
          <span>음량</span>
          <input data-vr-volume type="range" min="0" max="1" defaultValue="1" step="0.05" aria-label="음량" />
        </label>
        <button type="button" className="btn sec" data-vr-reset>정면</button>
        <button type="button" className="btn sec" data-vr-fullscreen>전체 화면</button>
        <button type="button" className="btn sec" data-vr-headset aria-pressed="false" disabled>
          VR 헤드셋 확인 중
        </button>
        <span className="vr-xr-status" data-vr-xr-status role="status">
          WebXR 지원을 확인하고 있습니다.
        </span>
      </div>
      <p ref={messageRef} className="playback-message" data-playback-message role="status" hidden />
    </div>
  );
}
