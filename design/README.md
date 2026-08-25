# 대시보드 개편 초안

`src/` 는 건드리지 않습니다. 여기서 실제 Jinja 템플릿과 CSS로 만들어 두고,
검토가 끝난 것만 `src/stt_to_subtitle/` 로 옮깁니다.

## 보는 법

```bash
python3 design/preview/render.py     # design/preview/out/*.html 생성
```

앱을 띄우지 않고 `design/preview/out/dashboard.html` 을 브라우저로 열면 됩니다.
샘플 데이터는 `design/preview/fixtures.py` 에 있습니다.

3D 파이프라인 화면(`/webgpu`)은 정적 렌더 대상이 아니라 앱을 띄워야 합니다.
레퍼런스와 설계 근거는 `HANDOFF.md` §1 에 있습니다 — **고치기 전에 먼저 읽으세요.**
깊이를 만드는 것은 그림자이지 발광이 아니고, 미디어 트리는 카테고리마다 깊이가 다릅니다.
`localhost` 로 접속해야 WebGPU 가 열립니다 (secure context). 자세한 것은 `HANDOFF.md`.

## 파일이 어디로 갈 것인가

| 초안 | 대상 |
|---|---|
| `static/app.css` | `src/stt_to_subtitle/static/app.css` (전체 교체 후보) |
| `templates/base.html` | `src/stt_to_subtitle/templates/base.html` |
| `templates/dashboard.html` | `src/stt_to_subtitle/templates/dashboard.html` |
| `templates/_gpu_panel.html` | `src/stt_to_subtitle/templates/_gpu_stats.html` 대체 |
| `templates/_actor.html` | 신규 |
| `templates/_icons.html` | 현재 파일 그대로 복사본 (변경 없음) |

`preview/` 는 옮기지 않습니다. 검토용입니다.

## 지금 초안에 반영된 결정

- **용어** — `실행 중` / `멈춤`(중단·일시중지·실패) / `최근 완료`. "마감" 같은 표현은 쓰지 않습니다.
- **멈춤 섹션** — 중단·실패가 대시보드 전면에 나오고, 사유와 복구 버튼이 같은 줄에 있습니다.
- **파이프라인 슬롯** — 단계마다 실행기가 하나씩(`orchestrator.py` 의 `max_workers=1`)이라는
  실제 구조를 그대로 보여줍니다. 병렬로 도는 것처럼 보이게 하지 않습니다.
- **GPU** — 사용률은 시간에 따라 변하는 값이라 스파크라인, 메모리는 총량 대비 비율이라 미터.
  온도·전력은 순간값이라 한계값만 옆에 적습니다.
- **배우** — 이니셜 대신 `.actors/<이름>.jpg` 를 씁니다. 사진이 없으면 실루엣으로 대체합니다
  (`_actor.html`). 배우 UI는 NFO에 `actor` 가 있을 때만 나타납니다.
- **색** — 토큰 이름은 그대로 두고 값만 바꿉니다. 다크 모드는 시스템 기본 +
  `data-theme="light|dark"` 강제 지정 두 경로를 모두 정의해 두었습니다.
  다크 상태색은 대기(주황)와 실패(빨강)가 구분되지 않아 (ΔE 11.1, 기준 15) 다시 잡았습니다.

## 아직 백엔드가 만들어 주지 않는 값

`render.py` 를 돌리면 아래 목록이 같이 출력됩니다. 화면을 먼저 확정한 뒤
필요한 것만 구현하면 됩니다.

| 값 | 필요한 작업 |
|---|---|
| `pipeline_slots` | `orchestrator` 의 executor 점유 상태를 읽어 노출 |
| `job.eta_seconds` | 청크 처리 속도 × 남은 청크 |
| `job.queue_position` | 대기 큐 순번 |
| `job.actor` | `web_config` 의 NFO `actor` + `.actors/<이름>.jpg` 라우트 |
| `actor_progress` | 배우별 자막 보유율 집계 (AV/japan 에만 성립) |
| 미디어 구역별 집계 | AV/japan 밖 카테고리의 자막 보유율. 없어서 3D 창고에서 회색으로 둡니다 |
| `gpu.utilization_history` | Prometheus range query (지금은 순간값만 읽음) |
| `stage_totals` | 단계별 소요 시간 누계 |

## 아직 안 만든 화면

작업 목록(필터 재편·단계별 일괄 동작), 미디어(배우 필터), 작업 상세(기록 정리),
설정(색 커스터마이즈). 대시보드가 확정되면 같은 방식으로 이어서 만듭니다.

필터 재편의 근거 — 어느 칸이 도달 불가이고 왜 그런지 — 는 캔버스 시안의
"작업 필터 재설계" 아트보드에 정리돼 있습니다.
