# 중립 명명·웹 UI 재구성·GPU 관측 작업 정리

- 작업일: 2026-08-17
- 대상 프로젝트: `stt-to-subtitle`
- 프로젝트 버전: `3.1.0`

## 1. 작업 목적

이번 작업은 다음 세 가지 목표를 기준으로 진행했다.

1. 실제 지원 장치 범위와 맞지 않던 `macos` 계열 이름을 역할 중심의 중립적인 STT 이름으로 변경한다.
2. 한 화면에 섞여 있던 대시보드와 미디어 파일 목록을 분리하고, 주요 기능 사이의 이동 구조를 명확하게 만든다.
3. NVIDIA GPU 사용량을 확인할 수 있는 Prometheus/Grafana 구성을 메인 애플리케이션과 독립된 사이드 프로젝트로 제공한다.

## 2. 결과 요약

- 호스트 STT API, 실행 스크립트, 환경 파일, 런타임 생성기, 테스트에서 `macos` 이름을 제거했다.
- `/`, `/media`, `/jobs`, `/settings`의 책임을 분리하고 반응형 사이드 메뉴를 적용했다.
- 메인 대시보드는 작업 현황, 서비스 연결 상태, 최근 작업만 보여주는 개요 화면으로 단순화했다.
- 미디어 탐색과 새 작업 생성은 `/media`로 이동했다.
- GPU 관측 스택을 `gpu-observability/` 아래 독립 Compose 프로젝트로 추가했다.
- 메인 웹 앱은 `GPU_DASHBOARD_URL`로 외부 Grafana 링크만 노출하므로 관측 스택과 직접 결합되지 않는다.
- Windows 테스트에서 드러난 SQLite 연결 수명과 경로 표현 문제를 함께 보완했다.

## 3. STT 명칭 중립화

### 3.1 파일 및 엔트리 포인트 변경

| 기존 이름 | 변경한 이름 |
| --- | --- |
| `src/stt_to_subtitle/macos_api.py` | `src/stt_to_subtitle/stt_api.py` |
| `stt-macos-api` | `stt-api` |
| `scripts/run-macos-stt.sh` | `scripts/run-stt-api.sh` |
| `scripts/create_macos_runtime.py` | `scripts/create_stt_runtime.py` |
| `scripts/macos-runtime/` | `scripts/stt-runtime/` |
| `.env.macos.example` | `.env.stt.example` |
| `tests/test_macos_api.py` | `tests/test_stt_api.py` |
| `tests/test_create_macos_runtime.py` | `tests/test_create_stt_runtime.py` |
| `MacOSAPISettings` | `STTAPISettings` |

Python 콘솔 엔트리 포인트는 다음과 같다.

```toml
stt-api = "stt_to_subtitle.stt_api:main"
```

ASGI 모듈 경로도 `stt_to_subtitle.stt_api:app`으로 변경했다.

### 3.2 환경변수와 로컬 경로 변경

| 기존 이름 또는 경로 | 변경한 이름 또는 경로 |
| --- | --- |
| `MACOS_STT_PYTHON` | `STT_API_PYTHON` |
| `MACOS_STT_BOOTSTRAP_PYTHON` | `STT_BOOTSTRAP_PYTHON` |
| `MACOS_STT_ENV` | `STT_API_ENV` |
| `.venv-macos` | `.venv-stt` |
| `var/macos-cache` | `var/model-cache` |
| `var/macos-stt` | `var/stt` |
| `.macos-runtime` | `.stt-runtime` |

기존 로컬 배포를 갱신할 때에는 환경변수 이름을 바꾸고, 필요한 상태 또는 모델 캐시를 새 경로로 직접 이동해야 한다. 상태 DB와 모델 캐시는 자동으로 이전하지 않는다.

### 3.3 실행 방식 보완

- 호스트 실행기는 `caffeinate`가 설치되어 있을 때만 이를 사용한다.
- `caffeinate`가 없는 호스트에서는 Python 프로세스를 직접 실행한다.
- 기본 부트스트랩 인터프리터는 특정 설치 경로가 아닌 `python3.11` 명령으로 찾는다.
- MPS는 계속 지원하지만 명칭은 운영체제가 아니라 STT 서비스 역할을 기준으로 한다.

## 4. 웹 UI/UX 재구성

### 4.1 정보 구조

| 경로 | 책임 |
| --- | --- |
| `/` | 작업 통계, 전사·번역 서비스 연결 상태, 최근 작업, GPU 대시보드 링크 |
| `/media` | 미디어 파일·폴더 탐색, 포스터 확인, 새 전사·번역 작업 생성 |
| `/jobs` | 전체 작업 이력, 상태 필터, 후속 작업 관리 |
| `/settings` | 전사·번역 서버와 프롬프트 설정 |

기존 `/`의 `folder` 쿼리 사용 요청은 `/media`로 리디렉션해 이전 링크의 동작을 보존한다. 작업 생성 성공·실패 안내도 미디어 화면에서 이어지도록 조정했다.

### 4.2 내비게이션과 반응형 구성

- 데스크톱에서는 좌측 사이드바로 네 개 주요 화면을 항상 구분한다.
- 현재 경로에는 활성 상태와 `aria-current="page"`를 적용한다.
- 내용 없이 공간을 차지하던 데스크톱 상단 바를 제거했다.
- 760px 이하에서는 사이드바를 화면 하단의 동일 폭 4분할 내비게이션으로 전환한다.
- 모바일 본문에는 하단 내비게이션과 safe area를 고려한 여백을 확보한다.
- 로그인 화면에는 애플리케이션 셸을 표시하지 않는다.
- 대시보드의 최근 작업 표는 개요 용도로 간소화하고 일괄 번역 컨트롤을 숨긴다.

### 4.3 주요 구현 파일

- `src/stt_to_subtitle/templates/base.html`: 공통 앱 셸과 내비게이션
- `src/stt_to_subtitle/templates/dashboard.html`: 개요 대시보드
- `src/stt_to_subtitle/templates/media.html`: 미디어 탐색 및 작업 생성
- `src/stt_to_subtitle/templates/jobs.html`: 작업 이력 화면
- `src/stt_to_subtitle/static/app.css`: 사이드바, 카드, 반응형 레이아웃
- `src/stt_to_subtitle/web_app.py`: 대시보드·미디어 컨텍스트와 라우트 분리
- `src/stt_to_subtitle/web_config.py`: GPU 대시보드 URL 설정 및 검증

## 5. GPU 관측 사이드 프로젝트

### 5.1 분리 원칙

`gpu-observability/`는 메인 Compose 프로젝트와 별도의 프로젝트 이름, 네트워크, 데이터 볼륨을 사용한다. 향후 이 디렉터리만 별도 저장소로 옮겨도 동작하도록 메인 애플리케이션 소스에 의존하지 않는다.

메인 Docker 빌드 컨텍스트에서도 제외했으며, 메인 앱과의 연결은 선택적인 URL 링크뿐이다.

```dotenv
GPU_DASHBOARD_URL=http://127.0.0.1:3000/d/gpu-overview
```

빈 값이면 메인 대시보드에 설정 안내만 표시한다. URL은 HTTP 또는 HTTPS만 허용한다.

### 5.2 포함 구성

| 구성 요소 | 기본 이미지 | 역할 |
| --- | --- | --- |
| NVIDIA DCGM Exporter | `nvcr.io/nvidia/k8s/dcgm-exporter:4.6.0-4.8.3-distroless` | NVIDIA GPU 메트릭 노출 |
| Prometheus | `prom/prometheus:v3.13.1` | 메트릭 수집과 30일 보존, 경보 평가 |
| Grafana | `grafana/grafana:13.1.0` | 자동 프로비저닝된 GPU 대시보드 제공 |

기본 포트는 보안을 위해 `127.0.0.1`에만 바인딩한다. Grafana 익명 접근과 사용자 가입은 비활성화했다.

### 5.3 대시보드와 경보

Grafana 대시보드는 다음 항목을 제공한다.

- GPU 사용률
- GPU 메모리 사용률
- 온도
- 전력 사용량
- GPU별 필터

Prometheus 경보 규칙은 다음 상태를 감지한다.

- DCGM Exporter가 2분 이상 응답하지 않음
- GPU 온도가 10분 동안 85°C 초과
- GPU 메모리 사용률이 10분 동안 95% 초과
- NVIDIA XID 오류 발생

### 5.4 별도 저장소로 분리할 때

1. `gpu-observability/` 디렉터리 내용을 새 저장소 루트로 복사한다.
2. `.env.example`을 `.env`로 복사하고 강한 `GRAFANA_ADMIN_PASSWORD`를 설정한다.
3. Linux 호스트에 NVIDIA 드라이버, Docker, NVIDIA Container Toolkit을 설치한다.
4. `docker compose config`로 구성을 검증한다.
5. `docker compose up -d`로 실행한다.
6. 메인 앱의 `GPU_DASHBOARD_URL`에 Grafana 대시보드 주소를 지정한다.

자세한 실행 절차는 `gpu-observability/README.md`에 정리되어 있다.

## 6. 안정성 및 테스트 보완

### 6.1 SQLite 연결 수명

`job_store.py`와 `transcription_store.py`의 연결 생성기를 컨텍스트 관리자로 변경했다. 트랜잭션 성공·실패와 관계없이 연결을 명시적으로 닫아 Windows에서 임시 DB 파일이 잠긴 채 남는 문제를 방지한다.

### 6.2 Windows 호환성

- FFmpeg와 전사 파이프라인 테스트의 경로 기대값을 `Path` 기반 플랫폼 표현으로 변경했다.
- 심볼릭 링크 권한이 없는 Windows 환경에서는 해당 보안 테스트만 명시적으로 건너뛴다.
- 테스트용 SQLite 연결도 픽스처 종료 전에 닫는다.

## 7. 검증 결과

| 검증 | 결과 |
| --- | --- |
| 전체 `unittest` 제품군 | 212개 통과, 1개 건너뜀 |
| Python 소스 컴파일 | 통과 |
| `git diff --check` | 통과 |
| 메인 `compose.yaml` 구성 렌더링 | 통과 |
| `gpu-observability/compose.yaml` 구성 렌더링 | 통과 |
| Grafana 대시보드 JSON 구문 검사 | 통과 |
| 인증 로그인 템플릿 렌더링 | HTTP 200, 앱 셸 미노출 확인 |
| 운영 코드·설정의 잔존 `macos` 접두사 검색 | 이 마이그레이션 문서의 기존 이름 표기를 제외하고 0건 |

검증 당시 Docker 데몬이 실행 중이지 않아 GPU 관측 컨테이너의 실제 기동과 실시간 메트릭 수집은 확인하지 않았다. NVIDIA GPU가 연결된 Linux 호스트에서 별도 런타임 검증이 필요하다.

## 8. 운영 시 확인 사항

- 기존 `.env.macos` 사본은 `.env.stt` 등 중립적인 이름으로 바꾸고 새 환경변수 명칭을 적용한다.
- 기존 `var/macos-stt` 상태를 유지해야 한다면 서비스를 중지한 뒤 `var/stt`로 복사하고 파일 권한을 확인한다.
- 모델을 다시 받지 않으려면 기존 캐시를 `var/model-cache` 구조로 이전한다.
- `HF_TOKEN`과 Grafana 관리자 비밀번호를 저장소에 커밋하지 않는다.
- GPU 관측 프로젝트의 실제 노출 주소는 방화벽과 리버스 프록시 정책을 검토한 뒤 변경한다.
- 새 Pull Request를 만들 때에는 저장소 정책에 따라 `pyproject.toml` 버전을 한 번만 증가시킨다.
