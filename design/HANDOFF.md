# 대시보드 개편 — 인수인계

작성 2026-08-25 · 3D 화면 레퍼런스 기반 재작업 2026-08-25 · 브랜치 `claude/design-handoff-b9ebcc`

`src/` 는 이 작업으로 바뀐 것이 없다. 전부 `design/` 안에서만 만들었다.

---

## 1. `/webgpu` 3D 화면 — 레퍼런스 기반으로 다시 지음

### 레퍼런스가 무엇인지 먼저 적어 둔다

이전 인수인계에 이 항목이 없어서 후처리 체크리스트(bloom·톤매핑·env map·반사·베벨·
입자·카메라)만 보고 만들었고, 일곱 개를 다 넣고도 화면이 납작했다. **기법 목록은
레퍼런스가 아니다.**

    레퍼런스: https://twin.quantlabnote.com/guest  (three.js r149, `twin3d.js`)

거기서 실제로 뽑아낸 값이다. 추측이 아니라 소스를 읽었다.

| 항목 | 레퍼런스 | 이전 우리 화면 |
|---|---|---|
| 카메라 | **직교(Orthographic)**, 고도각 0.6rad ≈ 34°, yaw 회전, zoom | 원근 40° |
| 그림자 | **PCFSoftShadowMap 2048**, ortho ±11, radius 3.2, bias −0.0004 | 아예 없음 |
| 조명 | Hemisphere(`0xdfe8ff`/`0x8a795c`, .65) + Directional(`0xfff2dd`, 1.0) | hemi .35 + 방향광 2개, 그림자 없음 |
| 재질 | 전부 Standard, roughness .55–.9, **metalness 0–0.1** | metalness .6–.9 |
| 후처리 | **없다.** bloom·SSAO·아웃라인 전부 없음 | bloom + reflector + 비네트 |
| 안개 | 없음 | Fog(30, 82) |
| 바닥 | 지면 → 플랫폼 슬래브 → 방마다 색 러그 | 무한 그리드, 빛 안 받는 바닥 |
| 라벨 | **DOM div 를 매 프레임 투영**, 거리별 LOD | 캔버스 텍스처 스프라이트 |

**깊이는 그림자가 만든다. 발광이 아니다.** 매트한 비금속 표면에 그림자 있는 따뜻한
키 라이트 하나면 충분하고, 어두운 금속에 bloom 을 아무리 올려도 납작한 건 그대로다.
그래서 bloom·reflector·TSL 경로는 **전부 걷어냈다**. `three.tsl.min.js` 와
`BloomNode.js` 는 지우지 않고 벤더 폴더에 남겨 뒀지만 import map 에서 뺐다 —
다시 쓸 일이 있으면 §1 의 이 문단부터 다시 읽을 것.

렌더러는 `WebGPURenderer` 그대로다(상단 배지도 그대로). WebGPU 에서 그림자 맵과
직교 카메라가 도는 것은 별도 페이지로 먼저 확인하고 옮겼다.

### 지금 씬에 있는 것

**2층 구조다.** 1층은 생산 라인, 위층은 미디어 창고이고 램프로만 이어진다.

| 구역 | 내용 |
|---|---|
| 상층 창고 | `/media` 트리 전체. AV/japan 통로 9줄 + 카테고리별 소형 베이 7개 |
| 1층 라인 | 대기 큐 → 추출 → 전사 → 번역 → 반출. 방마다 기계 하나, 작업자 하나 |
| GPU 서버실 | 벽 패널(UTIL/VRAM/TEMP/PWR) + 상주 모델 타워 |
| 격리 | 멈춤·실패. 상세 3건 + 나머지는 팔레트로 |

**운반원(NPC) 둘은 하는 일이 다르다.**
- 하나는 창고에서 골라 램프로 내려와 대기 큐에 넣는다 (작업 시작).
- 하나는 큐에서 꺼내 추출→전사→번역을 거쳐 반출 선반에 쌓는다.

라인에 도는 사람이 하나인 것은 그림이 아니라 사실이다 — executor 가 단계마다
하나씩(`max_workers=1`, §4)이라 파일이 동시에 흐르지 않는다. 사람을 늘리면
없는 병렬성을 그리게 된다.

**여러 개를 골라 시작하면 NPC 를 늘리지 않는다. 짐을 키운다.**
미디어를 30개 골라 시작해도 답은 둘 다 아니다:

- *NPC 를 30명 띄우는 것* — 처리량에 대한 거짓말이다. 화면은 "30건이 처리 중"이라고
  말하지만 실제로는 29건이 멈춰 서 있다.
- *한 사람이 30번 왕복하는 것* — 큐 삽입이 한 건씩 들어오는 것처럼 보인다.
  실제로는 한 번에 삽입되는 원자적 동작이다.

그래서 시작 담당은 **손수레 한 대에 묶음을 싣고 한 번 간다**(`BATCH`, `cart()`).
**선택은 묶음, 처리는 한 건씩** — 이 대비가 이 시스템의 실제 모습이고,
멈춤이 66건까지 쌓인 이유다. 여기를 고칠 때 NPC 수를 작업 수에 연동하지 말 것.

**모델 타워는 상주 모델 하나당 하나다.** `gpu.loaded` 를 그대로 세운다.
활성이면 랙 조명이 위로 훑고, 유휴면 어둡다. 유휴로 해제되면(커밋 `a64f1ec`)
타워도 사라져야 한다. "하이브리드"라는 타워는 없다 — §4 참고.

### 미디어 트리는 실제를 확인하고 넣었다

처음에 `media/2026-08/<배우>/…` 같은 균일한 트리로 가정했는데 **틀렸다.**
2026-08-25 운영 사이트(`stt.blackark.xyz`)에서 직접 확인한 실제 구조다:

```
/media
  ├ AV/japan          배우 디렉터리 449개 → 타이틀 폴더 → 파일   (세 겹)
  ├ AV/west, AV/unclassified
  ├ Drama, ETC, Movie, Sports
  └ Variety           타이틀 폴더 3개 + 폴더 없이 놓인 파일 12개  (섞임)
```

**카테고리마다 깊이가 다르다.** Variety 는 타이틀 폴더와 낱개 파일이 같은 자리에
있다. 균일한 트리로 가정하는 UI 를 만들면 안 된다. `fixtures.MEDIA_TREE` 에
같은 내용을 주석과 함께 넣어 뒀고, `source_rel` 샘플도 실제 모양으로 고쳤다.

자막 보유율(초록/갈색)은 `actor_progress` 가 있는 AV/japan 에만 칠한다.
나머지 구역은 집계값이 없으므로 **중립 회색으로 두고 칠하지 않는다.**

### 운영 실측 (2026-08-25)

`stt.blackark.xyz`: 진행 0 · **중단 66** · 실패 0 · 대기 3 · 완료 247.

**멈춤이 압도적이다.** §7 의 "멈춤 섹션을 전면에" 는 취향이 아니라 데이터가
시키는 것이다. 격리 구역을 한 건씩 늘어놓을 수 있는 규모가 아니라서
상세 3건 + 나머지 팔레트로 바꿨다. `JOB_STATS` 도 이 비율에 맞췄다
(라인이 도는 그림은 봐야 하므로 실행 중만 3 으로 남겨 뒀다).

### 이 화면을 고칠 때 알아야 할 것

**프레이밍은 계산으로 맞춘다** (`frameCamera()`). 직교 + 고정 고도각이라
부지의 화면 실루엣을 닫힌 식으로 구할 수 있다. zoom=1 에서 부지 전체가 들어오도록
반높이를 역산하므로 창 크기와 yaw 가 바뀌어도 잘리지 않는다. 오른쪽 HUD 패널 자리는
`setViewOffset` 으로 비우되, **좁은 창에서 패널 폭을 통째로 비우면 남는 자리가 없어
부지가 우표만 해진다** — 그래서 비우는 폭을 창 너비에 따라 늘린다. 처음에
`innerWidth >= 900` 로 껐다 켰다 했더니 800px 창에서 창고가 통째로 잘렸다.

**DOM 핀은 머리공간이 따로 필요하다.** 핀은 오브젝트 위로 `translate(-50%,-100%)`
되므로 월드 기준 fit 만으로는 위가 잘린다. `sh` 에 여유를 더해 둔 이유다.

**구역 러그 색이 플랫폼 색과 가까우면 방이 통째로 안 보인다.** GPU 서버실을
`0xa8b6c2` 로 뒀다가 회색 플랫폼에 묻혀서 방이 없는 것처럼 보였다. `0x8fa8bd` 로 바꿨다.

**`THREE.Points` 에 `map` 을 물리면 입자가 통째로 사라진다.** 점에는 uv 어트리뷰트가
없어 샘플링이 실패한다. 지금 씬에는 입자가 없지만 다시 넣을 거면 알아 둘 것.

**레이캐스팅은 `setViewOffset` 을 자동으로 반영한다** — 투영행렬을 그대로 쓰기 때문이다.
호버 그리드로 훑어 픽 가능한 오브젝트가 실제로 잡히는 것을 확인했다.

## 2. 실행

```bash
~/.pyenv/versions/3.12.13/bin/python -m uvicorn design.app:app --host 0.0.0.0 --port 8099
```

| 경로 | 내용 |
|---|---|
| `/option-B` | 2D 대시보드 (검토·수정 반영본) |
| `/webgpu` | 3D 파이프라인 (`?webgl` 로 WebGL2 폴백 강제) |
| `/static/...` | CSS, 벤더 JS |

FastAPI·uvicorn 은 (원래 작업하던 리눅스 머신 기준) `~/.pyenv/versions/3.12.13` 에 설치되어
있다. 그 머신의 시스템 python3 에는 pip 가 없고 `python3-venv` 도 깨져 있다.

다른 머신이라면 venv 를 따로 파면 된다. 의존성은 셋뿐이다.

```bash
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn jinja2
.venv/bin/python -m uvicorn design.app:app --host 127.0.0.1 --port 8099 --reload
```

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
  static/vendor/            Three.js (CDN 미사용)
                            three.webgpu.min.js, three.core.min.js  — 쓰는 것
                            three.tsl.min.js, BloomNode.js          — 지금은 안 씀 (§1)
                            OrbitControls.js                        — 지금은 안 씀,
                                                                      직교 리그를 직접 굴린다
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
- `media/<연월>/<배우>/…` 라는 균일한 미디어 트리 — 실제로는 카테고리마다 깊이가
  다르다. 운영 사이트에서 확인하고 `MEDIA_TREE` 로 교체했다 (§1)
- AV/japan 밖 구역의 자막 보유율 — 집계값이 없다. 색칠하지 않고 회색으로 둔다
- 라인 위를 도는 운반원을 여럿 두는 것 — 없는 병렬성을 그리게 된다

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

- **이번에는 3D 씬을 실제로 보면서 만들었다.** 이 작업을 한 머신은 macOS 이고
  브라우저에 WebGPU 가 있어(`navigator.gpu` 존재, secure context) 스크린샷으로
  매 단계 확인했다. 이전 인수인계의 "이 머신에서는 3D 씬을 볼 수 없다"는
  그 리눅스 머신 이야기다 — 헤드리스 크롬이 GPU 를 못 잡고 swiftshader 로 떨어졌다.
- 확인한 것: 그림자·직교 카메라가 WebGPU 백엔드에서 실제로 도는 것(별도 페이지로 먼저 검증),
  창 크기별 프레이밍, 픽 가능한 오브젝트가 실제로 잡히는 것(호버 그리드로 훑음),
  선택 시 상세 패널 내용.
- **프레임률은 이 환경에서 못 믿는다.** 브라우저 패널이 숨겨져 있으면 rAF 가
  0.1fps 까지 스로틀된다. 패널이 보일 때는 120fps 가 나왔지만, 실제 성능은
  대상 기기에서 다시 봐야 한다.
- **한글 폰트는 이 머신에 있다.** DOM 핀으로 라벨을 옮긴 뒤로는 캔버스 텍스처에
  한글을 그리지 않으므로 폰트 없는 머신에서도 라벨이 깨지지 않는다. 벽 패널은
  ASCII 숫자만 쓴다.

## 10. 이 커밋에 함께 들어간 기존 변경

작업 시작 시점부터 워킹 트리에 커밋되지 않은 채 있던 것들이다. 이번 개편과 무관하며
내가 만들거나 수정하지 않았다.

- `web_app.py` — `retry_selected_jobs` 라우트, `retriable_job_ids` 컨텍스트
- `_jobs_table.html`, `job-selection.js` — 선택 재시도 UI
- `orchestrator.py`, `dashboard.html`, `app.css` — 위에 딸린 변경
- `tests/test_web_app.py`, `tests/test_orchestrator.py` — 해당 테스트
