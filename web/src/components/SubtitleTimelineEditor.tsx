"use client";

import type { PointerEvent as ReactPointerEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "@/components/Icon";

export interface SubtitleTimelineCue {
  id: string | null;
  start: number;
  end: number;
  speaker: string;
  sourceText: string;
  text: string;
}

interface DraftCue extends SubtitleTimelineCue {
  key: string;
}

interface SubtitleTimelineEditorProps {
  cues: SubtitleTimelineCue[];
  currentTime: number;
  duration: number;
  busy: boolean;
  onSeek: (seconds: number) => void;
  onSave: (cues: SubtitleTimelineCue[]) => Promise<void>;
  onClose: () => void;
}

type DragMode = "move" | "start" | "end";

const MIN_CUE_SECONDS = 0.1;
const ZOOM_LEVELS = [8, 16, 32, 64] as const;

function roundTime(value: number): number {
  return Math.round(Math.max(0, value) * 1000) / 1000;
}

function timecode(seconds: number): string {
  const safe = Math.max(0, seconds);
  const total = Math.floor(safe);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ms = Math.floor((safe - total) * 1000);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

function cueKey(cue: SubtitleTimelineCue, index: number): string {
  return cue.id ?? `new-${index}-${cue.start}-${cue.end}`;
}

function comparable(cues: SubtitleTimelineCue[]): string {
  return JSON.stringify(cues.map((cue) => ({
    ...cue,
    start: roundTime(cue.start),
    end: roundTime(cue.end),
  })));
}

export function SubtitleTimelineEditor({
  cues,
  currentTime,
  duration,
  busy,
  onSeek,
  onSave,
  onClose,
}: SubtitleTimelineEditorProps) {
  const [initialCues] = useState<DraftCue[]>(() => (
    cues.map((cue, index) => ({ ...cue, key: cueKey(cue, index) }))
  ));
  const [draft, setDraft] = useState<DraftCue[]>(() => initialCues);
  const [selectedKey, setSelectedKey] = useState<string | null>(draft[0]?.key ?? null);
  const [pixelsPerSecond, setPixelsPerSecond] = useState<number>(16);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [draggingKey, setDraggingKey] = useState<string | null>(null);
  const timelineScrollRef = useRef<HTMLDivElement | null>(null);
  const nextCueNumber = useRef(1);
  const dragState = useRef<{
    key: string;
    mode: DragMode;
    originX: number;
    start: number;
    end: number;
  } | null>(null);
  const dragMoved = useRef(false);

  const selected = draft.find((cue) => cue.key === selectedKey) ?? null;
  const totalDuration = Math.max(
    duration,
    ...draft.map((cue) => cue.end),
    currentTime,
    1,
  );
  const timelineWidth = Math.max(720, Math.ceil(totalDuration * pixelsPerSecond) + 48);
  const tickSeconds = pixelsPerSecond >= 64
    ? 1
    : pixelsPerSecond >= 32
      ? 5
      : pixelsPerSecond >= 16 ? 10 : 30;
  const ticks = useMemo(() => {
    const values: number[] = [];
    for (let value = 0; value <= totalDuration; value += tickSeconds) values.push(value);
    return values;
  }, [tickSeconds, totalDuration]);
  const cueLanes = useMemo(() => {
    const laneEnds: number[] = [];
    const byKey = new Map<string, number>();
    for (const cue of [...draft].sort((a, b) => a.start - b.start || a.end - b.end)) {
      let lane = laneEnds.findIndex((end) => end <= cue.start);
      if (lane < 0) lane = laneEnds.length;
      laneEnds[lane] = cue.end;
      byKey.set(cue.key, lane);
    }
    return { byKey, count: Math.max(1, laneEnds.length) };
  }, [draft]);
  const initialComparable = comparable(initialCues);
  const dirty = comparable(draft) !== initialComparable;

  useEffect(() => {
    if (!dirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  const updateCue = (key: string, fields: Partial<DraftCue>) => {
    setDraft((current) => current.map((cue) => (
      cue.key === key ? { ...cue, ...fields } : cue
    )));
    setValidationError(null);
  };

  const addCue = () => {
    const boundedDuration = Number.isFinite(duration) && duration > 0 ? duration : Infinity;
    let start = Math.min(roundTime(currentTime), Math.max(0, boundedDuration - MIN_CUE_SECONDS));
    const end = Math.min(roundTime(start + 2), boundedDuration);
    if (end - start < MIN_CUE_SECONDS) {
      start = Math.max(0, roundTime(end - 2));
    }
    const key = `new-${Date.now()}-${nextCueNumber.current}`;
    nextCueNumber.current += 1;
    const cue: DraftCue = {
      key,
      id: null,
      start,
      end: Math.max(roundTime(start + MIN_CUE_SECONDS), end),
      speaker: selected?.speaker || "UNKNOWN",
      sourceText: "",
      text: "",
    };
    setDraft((current) => [...current, cue].sort((a, b) => a.start - b.start));
    setSelectedKey(key);
    setValidationError(null);
  };

  const removeSelected = () => {
    if (!selected || draft.length <= 1) return;
    const index = draft.findIndex((cue) => cue.key === selected.key);
    const remaining = draft.filter((cue) => cue.key !== selected.key);
    setDraft(remaining);
    setSelectedKey(remaining[Math.min(index, remaining.length - 1)]?.key ?? null);
    setValidationError(null);
  };

  const nudgeCue = (cue: DraftCue, delta: number) => {
    const cueDuration = cue.end - cue.start;
    const maxStart = Number.isFinite(duration) && duration > 0
      ? Math.max(0, duration - cueDuration)
      : Infinity;
    const start = roundTime(Math.min(maxStart, Math.max(0, cue.start + delta)));
    updateCue(cue.key, { start, end: roundTime(start + cueDuration) });
  };

  const beginDrag = (
    event: ReactPointerEvent<HTMLButtonElement>,
    cue: DraftCue,
    mode: DragMode,
  ) => {
    event.preventDefault();
    event.stopPropagation();
    setSelectedKey(cue.key);
    setDraggingKey(cue.key);
    dragMoved.current = false;
    dragState.current = {
      key: cue.key,
      mode,
      originX: event.clientX,
      start: cue.start,
      end: cue.end,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const continueDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const active = dragState.current;
    if (!active) return;
    if (Math.abs(event.clientX - active.originX) < 3) return;
    dragMoved.current = true;
    const delta = (event.clientX - active.originX) / pixelsPerSecond;
    if (active.mode === "start") {
      updateCue(active.key, {
        start: roundTime(Math.min(active.end - MIN_CUE_SECONDS, Math.max(0, active.start + delta))),
      });
      return;
    }
    if (active.mode === "end") {
      const maxEnd = Number.isFinite(duration) && duration > 0 ? duration : Infinity;
      updateCue(active.key, {
        end: roundTime(Math.min(maxEnd, Math.max(active.start + MIN_CUE_SECONDS, active.end + delta))),
      });
      return;
    }
    const cueDuration = active.end - active.start;
    const maxStart = Number.isFinite(duration) && duration > 0
      ? Math.max(0, duration - cueDuration)
      : Infinity;
    const start = roundTime(Math.min(maxStart, Math.max(0, active.start + delta)));
    updateCue(active.key, { start, end: roundTime(start + cueDuration) });
  };

  const finishDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragState.current = null;
    setDraggingKey(null);
    window.setTimeout(() => { dragMoved.current = false; }, 0);
  };

  const seek = (seconds: number) => {
    onSeek(roundTime(Math.min(totalDuration, Math.max(0, seconds))));
  };

  const save = async () => {
    const invalidIndex = draft.findIndex((cue) => (
      !cue.sourceText.trim()
      || !cue.text.trim()
      || !Number.isFinite(cue.start)
      || !Number.isFinite(cue.end)
      || cue.start < 0
      || cue.end <= cue.start
    ));
    if (invalidIndex >= 0) {
      setSelectedKey(draft[invalidIndex]?.key ?? null);
      setValidationError(`${invalidIndex + 1}번 세그먼트의 시간·원문·번역을 확인하십시오.`);
      return;
    }
    setValidationError(null);
    await onSave(draft.map((cue) => ({
      id: cue.id,
      start: roundTime(cue.start),
      end: roundTime(cue.end),
      speaker: cue.speaker,
      sourceText: cue.sourceText,
      text: cue.text,
    })));
  };

  const close = () => {
    if (dirty && !window.confirm("저장하지 않은 타임라인 변경을 버리고 닫을까요?")) return;
    onClose();
  };

  return (
    <section className="card subtitle-editor-card" aria-labelledby="subtitle-editor-title">
      <div className="card-head subtitle-editor-head">
        <div>
          <h2 id="subtitle-editor-title">자막 타임라인 편집기</h2>
          <span className="sub">재생 헤드에 맞춰 세그먼트를 이동·트림·추가하고 새 불변 수정본으로 저장합니다.</span>
        </div>
        <div className="btns">
          {dirty ? <span className="b wait">저장하지 않은 변경</span> : <span className="b line">변경 없음</span>}
          <button type="button" className="btn sec" disabled={busy} onClick={close}>닫기</button>
          <button type="button" className="btn" disabled={busy || !dirty} onClick={() => void save()}>
            <Icon name="check" size={15} />수정본 저장
          </button>
        </div>
      </div>

      <div className="subtitle-editor-toolbar" aria-label="타임라인 도구">
        <output className="code subtitle-playhead-time" aria-label="현재 재생 위치">{timecode(currentTime)}</output>
        <button type="button" className="btn sec" disabled={busy} onClick={addCue}>
          <Icon name="plus" size={15} />재생 위치에 세그먼트 추가
        </button>
        <label className="subtitle-zoom-control">
          <span>확대</span>
          <select
            className="ctl"
            value={pixelsPerSecond}
            onChange={(event) => setPixelsPerSecond(Number(event.target.value))}
          >
            {ZOOM_LEVELS.map((value) => <option key={value} value={value}>{value}px/초</option>)}
          </select>
        </label>
        <button
          type="button"
          className="btn sec"
          disabled={busy || !dirty}
          onClick={() => {
            setDraft(initialCues.map((cue) => ({ ...cue })));
            setSelectedKey(initialCues[0]?.key ?? null);
            setValidationError(null);
          }}
        >
          되돌리기
        </button>
      </div>

      {validationError ? <p className="notice error subtitle-editor-error" role="alert">{validationError}</p> : null}

      <div className="subtitle-editor-workspace">
        <div className="subtitle-timeline-pane">
          <div className="subtitle-timeline-scroll" ref={timelineScrollRef}>
            <div
              className="subtitle-timeline"
              style={{ width: timelineWidth }}
              onClick={(event) => {
                if (event.target !== event.currentTarget) return;
                const rect = event.currentTarget.getBoundingClientRect();
                seek((event.clientX - rect.left) / pixelsPerSecond);
              }}
              role="presentation"
            >
              <div className="subtitle-ruler" aria-hidden="true">
                {ticks.map((tick) => (
                  <span key={tick} style={{ left: tick * pixelsPerSecond }}>
                    <i />{timecode(tick).slice(0, 8)}
                  </span>
                ))}
              </div>
              <div
                className="subtitle-playhead"
                style={{ left: currentTime * pixelsPerSecond }}
                aria-hidden="true"
              />
              <div
                className="subtitle-track"
                style={{ height: cueLanes.count * 52 + 16 }}
                aria-label="자막 세그먼트 타임라인"
              >
                {draft.map((cue, index) => {
                  const selectedCue = cue.key === selectedKey;
                  const width = Math.max(96, (cue.end - cue.start) * pixelsPerSecond);
                  return (
                    <div
                      key={cue.key}
                      className={`subtitle-clip${selectedCue ? " is-selected" : ""}${draggingKey === cue.key ? " is-dragging" : ""}`}
                      style={{
                        left: cue.start * pixelsPerSecond,
                        top: (cueLanes.byKey.get(cue.key) ?? 0) * 52 + 8,
                        width,
                      }}
                    >
                      <button
                        type="button"
                        className="subtitle-clip-handle is-start"
                        aria-label={`${index + 1}번 세그먼트 시작 시간 조절`}
                        onPointerDown={(event) => beginDrag(event, cue, "start")}
                        onPointerMove={continueDrag}
                        onPointerUp={finishDrag}
                        onPointerCancel={finishDrag}
                      />
                      <button
                        type="button"
                        className="subtitle-clip-body"
                        aria-pressed={selectedCue}
                        aria-label={`${index + 1}번 세그먼트, ${timecode(cue.start)}부터 ${timecode(cue.end)}까지`}
                        title={cue.text || "새 자막"}
                        onClick={() => {
                          if (dragMoved.current) {
                            dragMoved.current = false;
                            return;
                          }
                          setSelectedKey(cue.key);
                          seek(cue.start);
                        }}
                        onPointerDown={(event) => beginDrag(event, cue, "move")}
                        onPointerMove={continueDrag}
                        onPointerUp={finishDrag}
                        onPointerCancel={finishDrag}
                        onKeyDown={(event) => {
                          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
                          event.preventDefault();
                          const amount = event.shiftKey ? 1 : 0.1;
                          nudgeCue(cue, event.key === "ArrowLeft" ? -amount : amount);
                        }}
                      >
                        <span>{index + 1}</span>
                        <strong>{cue.text || "새 자막"}</strong>
                      </button>
                      <button
                        type="button"
                        className="subtitle-clip-handle is-end"
                        aria-label={`${index + 1}번 세그먼트 종료 시간 조절`}
                        onPointerDown={(event) => beginDrag(event, cue, "end")}
                        onPointerMove={continueDrag}
                        onPointerUp={finishDrag}
                        onPointerCancel={finishDrag}
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
          <p className="subtitle-editor-help">
            클립 가운데를 드래그하면 이동하고 양쪽 손잡이를 드래그하면 길이가 바뀝니다. 키보드는 선택한 클립에서 ←/→ 0.1초, Shift와 함께 1초 이동합니다.
          </p>
        </div>

        <aside className="subtitle-inspector" aria-label="선택한 세그먼트 편집">
          {selected ? (
            <>
              <div className="subtitle-inspector-title">
                <div>
                  <strong>{draft.findIndex((cue) => cue.key === selected.key) + 1}번 세그먼트</strong>
                  <span className="code">{selected.id ?? "새 세그먼트"}</span>
                </div>
                <button
                  type="button"
                  className="btn dgr sm"
                  disabled={busy || draft.length <= 1}
                  title={draft.length <= 1 ? "마지막 세그먼트는 삭제할 수 없습니다." : undefined}
                  onClick={removeSelected}
                >
                  <Icon name="trash" size={14} />삭제
                </button>
              </div>
              <div className="subtitle-time-fields">
                <label>
                  <span>시작(초)</span>
                  <input
                    className="ctl code"
                    type="number"
                    min="0"
                    step="0.05"
                    value={selected.start}
                    onChange={(event) => updateCue(selected.key, { start: Number(event.target.value) })}
                  />
                </label>
                <label>
                  <span>종료(초)</span>
                  <input
                    className="ctl code"
                    type="number"
                    min="0.1"
                    step="0.05"
                    value={selected.end}
                    onChange={(event) => updateCue(selected.key, { end: Number(event.target.value) })}
                  />
                </label>
              </div>
              <label>
                <span>화자</span>
                <input
                  className="ctl"
                  maxLength={200}
                  value={selected.speaker}
                  onChange={(event) => updateCue(selected.key, { speaker: event.target.value })}
                />
              </label>
              <label>
                <span>원문</span>
                <textarea
                  className="ctl"
                  rows={3}
                  maxLength={10_000}
                  lang="ja"
                  value={selected.sourceText}
                  onChange={(event) => updateCue(selected.key, { sourceText: event.target.value })}
                />
              </label>
              <label>
                <span>한국어 자막</span>
                <textarea
                  className="ctl"
                  rows={4}
                  maxLength={10_000}
                  value={selected.text}
                  onChange={(event) => updateCue(selected.key, { text: event.target.value })}
                />
              </label>
              <button type="button" className="btn sec" onClick={() => seek(selected.start)}>
                <Icon name="play" size={14} />이 세그먼트부터 재생
              </button>
            </>
          ) : <div className="empty-state"><strong>편집할 세그먼트를 선택하십시오</strong></div>}
        </aside>
      </div>
    </section>
  );
}
