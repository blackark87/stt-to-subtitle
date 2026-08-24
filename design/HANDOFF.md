# 대시보드 개편 — 인수인계

작성 2026-08-25 · 브랜치 `agent/whisperjav-comparison-workflow`

`src/` 는 이 작업으로 바뀐 것이 없다. 전부 `design/` 안에서만 만들었다.

---

## 1. 지금 하던 일 (중단 지점)

`/webgpu` 3D 화면이 **동작은 하는데 볼품이 없다.** 사용자 확인 결과 WebGPU 백엔드로
정상 기동한다(상단 배지 `WebGPU`). 문제는 렌더링 품질이다 — 후처리가 하나도 없어서
회색 상자를 격자에 올려둔 수준이다.

**직전까지 한 것**: bloom 후처리를 붙이려고 의존성만 받아 둠

```
design/static/vendor/three.tsl.min.js    23 KB   (TSL 노드 재수출)
design/static/vendor/BloomNode.js        16 KB   (three/tsl, three/webgpu, three/addons 를 import)
```

**아직 안 한 것**: 위 두 파일을 import map 에 연결하고 실제로 적용하는 일. 필요한 import map:

```json
{ "imports": {
  "three":         "/static/vendor/three.webgpu.min.js",
  "three/webgpu":  "/static/vendor/three.webgpu.min.js",
  "three/tsl":     "/static/vendor/three.tsl.min.js",
  "three/addons/tsl/display/BloomNode.js": "/static/vendor/BloomNode.js"
} }
```

### 화려함을 위해 남은 작업 (효과 큰 순서)

1. **Bloom** — emissive 요소(진행률 링, GPU 바, 실패 슬래브)가 지금은 그냥 밝은 색일 뿐
   빛나지 않는다. 가장 큰 차이를 만든다.
2. **톤 매핑 + 색 관리** — `renderer.toneMapping = THREE.ACESFilmicToneMapping`,
   `outputColorSpace = SRGBColorSpace`.
3. **환경광(env map)** — 절차적 그라디언트 큐브맵. 금속 재질에 반사가 생겨야 입체가 산다.
4. **바닥 반사** — 어두운 광택 바닥에 스테이션이 비치게.
5. **형태 개선** — 맨 `BoxGeometry`/`CylinderGeometry` 대신 베벨. 스테이션을 끊어진 원기둥이
   아니라 **하나로 이어진 발광 레일**로 보는 것도 검토할 것.
6. **입자** — additive 블렌딩 + 트레일.
7. **카메라** — 미세한 드리프트, 선택 시 부드러운 이징.

---

## 2. 실행

```bash
~/.pyenv/versions/3.12.13/bin/python -m uvicorn design.app:app --host 0.0.0.0 --port 8099
```

| 경로 | 내용 |
|---|---|
| `/option-B` | 2D 대시보드 (검토·수정 반영본) |
| `/webgpu` | 3D 파이프라인 |
| `/static/...` | CSS, 벤더 JS |

FastAPI·uvicorn 은 `~/.pyenv/versions/3.12.13` 에 설치되어 있다. 시스템 python3 에는 pip 가
없고 `python3-venv` 도 깨져 있다.

**WebGPU 는 secure context 에서만 열린다.** LAN IP + 평문 http 로 접속하면
`navigator.gpu` 가 `undefined` 라 WebGL2 로 폴백한다. `localhost` 는 secure context 이므로
SSH 터널이 가장 간단하다:

```bash
ssh -N -L 8099:127.0.0.1:8099 blackark87@192.168.1.13
```

정적 렌더도 가능하다 (앱 없이 브라우저로 열기용):

```bash
python3 design/preview/render.py   # → design/preview/out/*.html
```

---

## 3. 폴더

```
design/
  app.py                    FastAPI 미리보기 서버
  README.md                 폴더 설명·매핑표
  HANDOFF.md                이 문서
  static/app.css            src/.../static/app.css 전체 교체 후보
  static/vendor/            Three.js 0.185.1 (CDN 미사용)
  templates/base.html       ← src/.../templates/base.html
           dashboard.html   ← src/.../templates/dashboard.html
           _gpu_panel.html  ← _gpu_stats.html 대체
           _actor.html      신규 (배우 아바타)
           webgpu.html      3D 화면 (신규, 대응 파일 없음)
           _icons.html      현재 파일 그대로 복사
  preview/render.py         정적 렌더
          fixtures.py       샘플 데이터 + 백엔드 미구현 목록
```

---

## 4. 코드에서 확인한 사실 (추측 아님)

### 파이프라인은 전부 직렬이다
`orchestrator.py:236-256` — `_stt_executor`, `_translation_executor`, `_render_executor` 가
모두 `max_workers=1`. `_audio_executor` 만 `settings.audio_workers`(기본 1).
**파일끼리 병렬로 도는 일은 없다.** 배우별·폴더별 묶음은 보기 방식일 뿐 처리 순서를 못 바꾼다.

### 작업 필터에 도달 불가 칸이 있다
`web_app.py:97` `JOB_STAGE_FILTERS`.

| 칸 | 문제 |
|---|---|
| 전사 · 완료 | `transcription_completed` 는 `operation="transcribe"` 전용 (`orchestrator.py:2121`). 전체 작업은 `transcribed` 로 가므로 영원히 안 들어옴 |
| 번역 · 완료 | `translated` 는 렌더 직전 수 초짜리. 목록이 거의 항상 빔 |
| 완료의 `rendering` | 같은 이유로 순간 상태 |
| 추출의 `audio_completed` | `operation="extract"` 전용 (`orchestrator.py:1939`) |
| (없음) | `translation_paused` 가 부모 "번역" 에만 있고 하위 칸이 없어 어느 칸에도 안 잡힘 |

제안: 상태가 아니라 **무엇을 기다리는가**로 묶는다 — 실행 중 / 대기 / 멈춤 / 완료.
완료는 "어디까지 갔나"(자막·전사·오디오 완료)로 쪼개 죽은 칸을 되살린다.

### `blocked` 하나에 세 가지가 섞여 있다
- 사용자 중단 — `_mark_job_stopped` (`orchestrator.py:1911`)
- 일반 오류 — `orchestrator.py:1876`
- 서비스 재시작 복구 — `job_store.recover_interrupted`

구분은 `error` 문자열뿐이다. **일시중지/중단/실패 3분류는 데이터가 못 받쳐준다.**
신뢰 가능한 건 `translation_paused` 하나. 제대로 하려면 `stop_reason` 컬럼이 필요하다.

### `hybrid` 는 모델 이름이 아니다
`hybrid_stt.py` — WhisperX 가 1차 디코딩, Kotoba 가 재디코딩해서 단어를 교체한다
(`replaced_with_kotoba`). 그러므로 "로드된 모델: 하이브리드" 는 존재하지 않는 것을
표시하는 것이다. 실제 상주 모델(WhisperX / Kotoba)을 각각 보여야 한다.

### GPU 지표는 여섯 개만 수집한다
`gpu_monitoring.py:18` DCGM 쿼리:
`GPU_UTIL, FB_USED, FB_FREE, FB_TOTAL, GPU_TEMP, POWER_USAGE`.
**전력 한계도 온도 임계도 안 가져온다.** 한계값을 쓰려면
`DCGM_FI_DEV_POWER_MGMT_LIMIT`, `DCGM_FI_DEV_SLOWDOWN_TEMP` 를 쿼리에 추가해야 한다.

### 남은 시간을 계산할 근거가 없다
`job_store` 의 시간 필드는 `created_at`, `status_updated_at` 둘뿐. 단계별 시작/종료도
청크별 소요도 기록하지 않는다. `chunks_completed / (now - status_updated_at)` 로 추정은
가능하나 번역은 청크마다 비용이 달라 오차가 크다. **경과 시간만 쓴다.**

### `_render` 는 파일 두 개를 쓰고 끝난다
`orchestrator.py` `_render` — SRT/ASS 를 쓰고 이벤트를 남긴다. 1초도 안 걸린다.
파이프라인 슬롯에서 뺐다(스테이션 3개).

### 배우 정보는 이미 읽고 있다
`web_config.py:785-805` `_read_nfo` 가 NFO 의 `<actor><name>` 을 파싱한다.
`.actors` 는 `IGNORED_DIRECTORY_NAMES`(`web_config.py:35`)에 있어 탐색에서만 숨는다 —
포스터(`/media/posters/...`)처럼 전용 라우트를 하나 만들면 `.actors/<이름>.jpg` 를 쓸 수 있다.

---

## 5. 지어냈다가 뺀 값

`fixtures.py` 상단 주석에도 적어 두었다.

- 남은 시간 "약 12분" — 근거 없음. 경과 시간으로 교체
- GPU 전력 한계 320W / 온도 한계 83°C — 수집하지 않는 값
- "하이브리드" 라는 상주 모델 — 존재하지 않음
- 자막 단계 슬롯 — 순간에 끝나서 슬롯이 될 수 없음

---

## 6. 백엔드가 아직 안 주는 값

`render.py` 실행 시 매번 출력된다.

| 값 | 필요한 작업 |
|---|---|
| `pipeline_slots` | orchestrator executor 점유 상태 노출 |
| `job.queue_position` | 대기 큐 순번 |
| `job.actor` | NFO actor + `.actors` 라우트 |
| `actor_progress` | 배우별 자막 보유율 집계 |
| `gpu.utilization_history` | Prometheus range query (현재는 순간값만) |
| `job.stop_reason` | 중단 사유 구분 (위 `blocked` 항목) |

---

## 7. 확정된 설계 결정

- **용어** — 실행 중 / 멈춤 / 최근 완료. "마감" 같은 표현은 쓰지 않는다.
- **멈춤 섹션** — 중단·실패가 대시보드 전면에. 사유와 복구 버튼이 같은 줄에.
- **단계별 일괄 동작** — 그룹 헤더가 곧 동작 막대. 선택 없이 그 단계 전체에 적용.
- **GPU** — 사용률은 시간에 따라 변하므로 스파크라인, 메모리는 총량 대비라 미터.
- **배우** — 이니셜 금지. `.actors/<이름>.jpg`, 없으면 실루엣. NFO 에 actor 가 있을 때만 노출.
- **색** — 토큰 이름 유지, 값만 교체. 다크는 시스템 기본 + `data-theme` 강제 두 경로.
  다크 상태색은 대기(주황)와 실패(빨강)가 ΔE 11.1 로 구분 불가라(기준 15) 다시 잡았다:
  `--ok #40A35C  --accent #6C7AE5  --warn-strong #BB8B00  --bad #C4453C` (검증 통과)
- **폰트** — 고정폭(IBM Plex Mono)은 ASCII 전용 `.code` 에만. 한글이 섞이는 자리는
  본문 서체 + `tabular-nums`(`.m`). 섞으면 한 문자열 안에서 서체가 갈려 깨져 보인다.

---

## 8. 아직 안 만든 화면

작업 목록(필터 재편 + 단계별 일괄 동작), 미디어(배우 필터), 작업 상세(기록 정리),
설정(색 커스터마이즈). 디자인은 확정됐고 코드로만 옮기면 된다.

시안(아트보드): https://claude.ai/code/artifact/199bcfe8-18d0-4b9d-9755-159b14b08007

---

## 9. 검증 방법과 한계

- Jinja 렌더 → 헤드리스 크롬 스크린샷 → 육안 확인까지 가능하다.
  크롬은 `~/.cache/puppeteer/chrome/linux-152.0.7977.42/chrome-linux64/chrome`.
- **이 머신에서는 3D 씬을 볼 수 없다.** RTX 3080 이 있어도 헤드리스 크롬이 GPU 를 못 잡고
  swiftshader 로 떨어지며, 그 상태에서 렌더 루프가 프레임을 내지 않는다.
  2D 화면은 검증 가능하고 실제로 이 방식으로 레이아웃 깨짐(가로 오버플로 22px,
  세로 잘림 361px)을 잡았다.
- **한글 폰트가 이 머신에 없다.** 스크린샷의 한글이 네모로 나오는 것은 환경 문제이지
  디자인 문제가 아니다.

---

## 10. 이 커밋에 함께 들어간 기존 변경

작업 시작 시점부터 워킹 트리에 커밋되지 않은 채 있던 것들이다. 이번 개편과 무관하며
내가 만들거나 수정하지 않았다.

- `web_app.py` — `retry_selected_jobs` 라우트, `retriable_job_ids` 컨텍스트
- `_jobs_table.html`, `job-selection.js` — 선택 재시도 UI
- `orchestrator.py`, `dashboard.html`, `app.css` — 위에 딸린 변경
- `tests/test_web_app.py`, `tests/test_orchestrator.py` — 해당 테스트
