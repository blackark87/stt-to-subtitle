# STT to Subtitle

영상에서 일본어 음성을 전사하고 OpenAI 호환 번역 서버로 한국어를 생성한 뒤,
화자별 SRT·ASS 자막을 저장하는 로컬 웹 애플리케이션입니다.

NVIDIA GPU 배포에서는 정적 웹, Backend API와 CUDA 전사 Runtime을 하나의
Docker Compose 프로젝트로 빌드하고 실행합니다. 애플리케이션 이미지를 외부
레지스트리에서 받거나 게시하지 않습니다.

## 구성

```text
브라우저 ──▶ Traefik(HTTPS) ──▶ web:8080 (Nginx, 정적 HTML/CSS/JS)
                                  │ HTTP /api
                                  ▼
                                backend:8080 (FastAPI + Uvicorn)
                                  ├─ FFmpeg 오디오 추출
                                  ├─ 단계별 작업·번역·자막 렌더링
                                  ├──▶ Runtime 풀 (HTTP + 진행 상태 SSE)
                                  │      ├─ runtime:8100 (기본)
                                  │      └─ 외부 GPU Runtime 1..N
                                  ├──▶ 1차(초벌) OpenAI 호환 API 0..N
                                  ├──▶ 2차(검증) OpenAI 호환 API 0..N
                                  └──▶ 선택형 외부 모델 API
```

Compose 프로젝트에는 세 실행 컨테이너가 있습니다.

- `web`: Nginx로 최소 정적 화면과 자산을 제공하고 `/api`만 프록시
- `backend`: 작업 API, 상태 저장, FFmpeg 추출, 번역, SRT·ASS 렌더링
- `runtime`: WhisperJAV, Kotoba, WhisperX, 하이브리드 전사를 제공하는 CUDA
  FastAPI 서버

Web, Backend와 Runtime은 하나의 Compose `app` 네트워크를 공유하며 Backend는
`http://runtime:8100`으로 Runtime에 연결됩니다. Backend와 Runtime 포트는
호스트에 직접 게시하지 않습니다. Web만 이 네트워크에 더해 기존 Traefik 외부
네트워크에도 연결되고 HTTPS 라우터를 통해 제공됩니다.
Backend는 Runtime이나 GPU 관측 서비스의 기동 상태와 무관하게 단독으로 빌드하고
시작할 수 있습니다. 연결되지 않은 Runtime과 GPU 메트릭은 각각 사용 불가 상태로
표시되지만 Backend 상태 확인에는 영향을 주지 않습니다.
기본 Runtime을 포함한 Compose 구성이 기본값이며, 외부 호스트의 Runtime은
Web UI에서 주소를 추가해 같은 전사 작업 풀로 확장할 수 있습니다. 번역 LLM
서버는 전사 Runtime 풀과 분리됩니다. 1차(초벌)와 2차(검증) 번역도 서로 다른
서버 레지스트리와 모델 설정을 가집니다. 각 그룹에서 기본 서버와 추가 서버를
독립적으로 켜고 끄며 이름·주소·토큰·동시 요청 수·일괄 우선 서버를 설정합니다.
Thinking 사용 여부도 서버별로 저장합니다. 미사용 서버에는
`reasoning_effort: none`을 보내고, 사용 서버에는 이 강제 비활성 값을 보내지 않아
해당 모델 서버의 thinking 설정을 따릅니다.
Backend가 두 번역 서버 레지스트리를 직접 소유하고 각 OpenAI 호환 API를
호출합니다. 번역 요청에는 구간 ID·원문·시작/종료 시각·신뢰할 수 없는 화자
힌트만 허용 목록으로 전달합니다. 2차에는 1차 번역을, 외부 교정에는 1차와 2차
번역을 함께 전달합니다. 전사 Runtime·GPU·워커·STT 모델 정보는 전달하지
않습니다.
전사, 1차 번역, 2차 번역, 외부 모델 검토는 각각 별도 작업으로만 요청합니다.
앞 단계가 성공해야 다음 단계를 만들 수 있지만 완료 후 다음 단계가 자동으로
시작되지는 않습니다.
기본 Runtime과 같은 메모리를 쓰는 Ollama 호스트는
`TRANSLATION_STT_HARD_BREAKER_HOSTS`에 등록할 수 있습니다. 이 호스트들은
기본 Runtime 전사 중 번역 라우팅에서 제외되며, 전사 시작 전에 진행 중 요청을
비우고 상주 LLM이 모두 언로드됐는지 확인합니다. 확인에 실패하면 메모리 경합을
감수하지 않고 해당 Runtime의 전사 시작을 차단합니다.
호스트에서 Ollama를 실행하는 경우 호스트 방화벽은 `APP_NETWORK_SUBNET`에서
Ollama 포트로 들어오는 연결을 허용해야 합니다. 세 애플리케이션 컨테이너는
같은 고정 대역을 사용하므로 별도 Runtime 네트워크 규칙은 필요하지 않습니다.

Kotoba, WhisperX, WhisperJAV는 요구하는 PyTorch·모델 의존성이 다르므로
STT 이미지 안에서도 각각 `/opt/venvs/kotoba`, `/opt/venvs/whisperx`,
`/opt/venvs/whisperjav`에 설치됩니다. HTTP 서버는 Kotoba 환경에서 실행하고
WhisperX와 WhisperJAV 요청은 격리된 Python 작업 프로세스로 처리합니다.

WhisperJAV 앙상블 코드는 `src/stt_to_subtitle/vendor/whisperjav/`에 상류
커밋을 고정해 포함되어 있으며, 워커 프로세스 안에서 직접 실행됩니다.
이전에는 워커가 상류 CLI를 다시 실행하고 그 CLI가 패스마다 자식 프로세스를
띄워 인터프리터가 4단으로 중첩됐고, 두 패스가 같은 오디오를 각각 디코드하고
같은 씬 분할을 두 번 계산했습니다. 지금은 씬 분할을 한 번만 수행해 두 패스가
공유하고, 파일 디코드도 잡당 1회입니다. 상류 출처와 로컬 수정 내역은
`src/stt_to_subtitle/vendor/whisperjav/VENDOR.md`에 있습니다.

## 요구 사항

- Linux/amd64 호스트
- Docker Engine과 Compose 플러그인
- Docker provider가 활성화된 Traefik과 외부 Docker 네트워크
- NVIDIA GPU, 호환 드라이버, NVIDIA Container Toolkit
- Pyannote 모델 이용 조건을 승인한 Hugging Face read 토큰
- 일본어→한국어 모델을 제공하는 OpenAI 호환 API

별도의 CUDA Toolkit이나 호스트 Python 환경은 필요하지 않습니다. Apple
Silicon의 Docker 가상 머신은 Metal/MPS 장치를 PyTorch 컨테이너에 전달하지
않으므로 MPS 전사는 아래의 호스트 실행 경로를 사용합니다.

## 처음 실행

저장소를 받은 뒤 통합 환경 파일을 준비합니다.

```bash
cp .env.compose.example .env.compose
chmod 600 .env.compose
```

`.env.compose`에서 최소한 다음 값을 설정합니다.

- `HF_TOKEN`: Pyannote 접근 권한이 있는 Hugging Face read 토큰
- `MEDIA_PATH`: 영상과 자막을 읽고 쓸 호스트 디렉터리
- `TRAEFIK_HOST`: 웹 애플리케이션에 사용할 DNS 호스트명
- `TRANSLATION_BUILTIN_BASE_URL`: 1차·2차 그룹에 각각 생성되는 기본 서버의 OpenAI 호환 API 루트
- `TRANSLATION_BUILTIN_DRAFT_ENABLED`: 1차 그룹의 기본 서버 사용 여부
- `TRANSLATION_BUILTIN_REVIEW_ENABLED`: 2차 그룹의 기본 서버 사용 여부
- `TRANSLATION_STATE_PATH`: 번역 서버와 서버별 모델 선택을 보존할 상태 디렉터리
- `TRANSLATION_STT_HARD_BREAKER_HOSTS`: 기본 Runtime과 메모리를 공유하는
  Ollama 호스트 목록(쉼표 구분, 선택 사항)

각 번역 서버의 모델은 설정 화면에서 연결 확인 후 해당 서버가 제공하는
모델 목록 중 하나를 선택합니다.

Compose는 경로 오타로 빈 호스트 디렉터리를 만들지 않습니다. 미디어, 상태,
작업 공간과 모델 캐시 디렉터리를 먼저 만들고 쓰기 권한을 확인합니다.

```bash
mkdir -p ./media \
  /var/lib/homelab/stt-to-subtitle/web-state \
  /var/lib/homelab/stt-to-subtitle/translation-state \
  /var/lib/homelab/stt-to-subtitle/stt-state \
  /data/work/stt-to-subtitle/web-jobs \
  /data/work/stt-to-subtitle/stt-incoming \
  /data/models/stt-to-subtitle
test -w ./media
test -w /var/lib/homelab/stt-to-subtitle/web-state
test -w /var/lib/homelab/stt-to-subtitle/translation-state
test -w /var/lib/homelab/stt-to-subtitle/stt-state
test -w /data/work/stt-to-subtitle/web-jobs
test -w /data/work/stt-to-subtitle/stt-incoming
test -w /data/models/stt-to-subtitle
```

고정 STT 실행 환경 이미지는 Python, CUDA 라이브러리와 서로 격리된 Kotoba,
WhisperX, WhisperJAV 환경을 포함합니다. 애플리케이션 `runtime` 이미지는 이 기반
이미지 위에 Runtime 의존 모듈만 추가합니다. 모델 가중치는 두 이미지에
포함하지 않습니다. 기반 런타임 이미지를 준비하려면 다음 명령을 사용합니다.

```bash
./scripts/compose.sh --env-file .env.compose --profile build build runtime-base
```

이후 애플리케이션 이미지를 빌드하고 세 서비스를 함께 실행합니다.

```bash
./scripts/compose.sh --env-file .env.compose config
./scripts/compose.sh --env-file .env.compose build
./scripts/compose.sh --env-file .env.compose up -d
./scripts/compose.sh --env-file .env.compose ps
```

Backend 이미지만 변경한 경우에는 다른 이미지를 빌드하거나 다른 서비스를 먼저
실행할 필요가 없습니다.

```bash
./scripts/compose.sh --env-file .env.compose build backend
./scripts/compose.sh --env-file .env.compose up -d backend
```

`scripts/compose.sh`는 현재 실행 계정의 UID/GID를 Runtime에 주입하며
root 실행은 거부합니다. 따라서 `.env.compose`에 UID/GID를 설정할 필요가
없고, 마운트 경로를 소유한 일반 사용자로 실행해야 합니다.

`runtime-base` 이미지는 세 ML 환경을 모두 설치하므로 최초 빌드 시간이 길고
이미지가 클 수 있습니다. 일반 `build`는 이 고정 이미지를 재사용하고
애플리케이션 코드만 설치합니다. 요구사항 파일이나 Python/CUDA 기반 환경을
변경할 때만 `STT_RUNTIME_BASE_IMAGE` 태그를 올리고 `runtime-base`를 다시
빌드합니다. 모델 가중치는 이미지에 포함하지 않으며 최초 전사 요청 때
`${MODEL_CACHE_PATH}`로 내려받습니다.
`STT_RUNTIME_CACHE_PATH`는 외부에서 준비한 BuildKit 로컬 캐시를 읽는 경로이며,
Compose 빌드 자체는 이 캐시를 갱신하지 않습니다.

상태를 확인합니다.

```bash
curl https://stt.example.com/healthz
./scripts/compose.sh --env-file .env.compose exec runtime \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8100/readyz').read().decode())"
```

브라우저에서 `https://TRAEFIK_HOST`를 열면 최소 정적 화면에서 상태와 최근
작업을 확인할 수 있습니다. 상세 UI는 후속 프런트엔드 마이그레이션 범위입니다.
Compose의 전사 API 주소는 내부 서비스 `http://runtime:8100`으로 고정되므로
환경 파일에서 설정하지 않습니다. 설정은 Backend 상태 디렉터리의
`jobs.sqlite3`에 저장되며 컨테이너를 다시 만들어도
유지됩니다. 번역 작업은 사용자가 완료된 앞 단계에서 다음 단계를 명시적으로
요청했을 때만 해당 서버를 호출하며, 주기적인 상태 확인은 수행하지 않습니다.
연결 실패 시 해당 작업을 중단하고 뒤의 대기 작업은 보존합니다. 번역 PC와
모델을 준비한 뒤 작업 목록에서 번역 작업만 선택해 재시도할 수 있습니다.

현재 정적 화면은 서비스 분리를 검증하기 위한 최소 UI입니다. 기존 Jinja2
화면의 시각 설계와 기능을 그대로 이식하지 않으며, 후속 프런트엔드 작업은
`service-split-plan.md`의 API 계약을 기준으로 진행합니다.

## 외부 Runtime 추가

외부 GPU 호스트에서는 Runtime 전용 Compose만 실행합니다.

```bash
cp .env.runtime.example .env.runtime
mkdir -p /var/lib/stt-to-subtitle/runtime-state \
  /data/work/stt-to-subtitle/runtime-incoming \
  /data/models/stt-to-subtitle
./scripts/compose.sh -f compose.runtime.yaml --env-file .env.runtime \
  --profile build build runtime-base
./scripts/compose.sh -f compose.runtime.yaml --env-file .env.runtime build runtime
./scripts/compose.sh -f compose.runtime.yaml --env-file .env.runtime up -d runtime
```

방화벽에서는 Backend 호스트가 접근할 Runtime 포트만 허용합니다. Web UI의
**전사 Runtime**에서 `http://runtime-host:8100` 주소와 동일한 API 토큰을
등록하면 준비 상태 확인 후 새 전사 작업부터 사용됩니다. 진행 중인 작업은
접수한 Runtime에 고정되며 Backend 재시작 후에도 같은 원격 작업에 재연결합니다.
Runtime 비활성화와 삭제는 해당 Runtime의 진행 작업이 없을 때만 허용됩니다.
같은 화면에서 GPU별 Kotoba·WhisperX 배치 크기도 독립적으로 설정할 수 있습니다.

Backend는 Runtime의 작업별 SSE 스트림으로 장시간 전사 진행 상태를 받습니다.
최소 정적 화면은 `/api/v1/jobs`를 10초마다 조회합니다. 오디오 추출이 끝나면 WAV
헤더의 재생 시간과 모델 청크 길이로 전체 전사 청크를 먼저 추정하며, 실제 생성
청크가 추정치를 넘으면 진행률의 전체 수를 자동 보정합니다.

## 환경 설정

### 공통 및 웹

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `MEDIA_PATH` | `./media` | 입력 영상과 생성 자막 |
| `BACKEND_STATE_PATH` | `/var/lib/homelab/stt-to-subtitle/web-state` | 작업 DB와 영속 상태 |
| `BACKEND_WORK_PATH` | `/data/work/stt-to-subtitle/web-jobs` | 작업 WAV와 재생성 가능한 JSON 체크포인트 |
| `BACKEND_PUID` | `1026` | Backend 프로세스 UID |
| `BACKEND_PGID` | `100` | Backend 프로세스 GID |
| `APP_NETWORK_SUBNET` | `172.24.0.0/16` | Web·Backend·Runtime 공용 Docker 네트워크 대역 |
| `BACKEND_APP_IPV4` | `172.24.0.2` | Backend 고정 주소 |
| `WEB_APP_IPV4` | `172.24.0.3` | Web 고정 주소 |
| `RUNTIME_APP_IPV4` | `172.24.0.4` | Runtime 고정 주소 |
| `TRAEFIK_HOST` | 필수 | 웹 HTTPS 라우터의 DNS 호스트명 |
| `TRAEFIK_NETWORK` | `proxy` | Traefik이 연결된 외부 Docker 네트워크 |
| `TRAEFIK_ENTRYPOINT` | `websecure` | Traefik HTTPS entrypoint |
| `TRAEFIK_CERT_RESOLVER` | `letsencrypt` | Traefik 인증서 resolver |
| `BACKEND_FORWARDED_ALLOW_IPS` | `*` | Nginx를 통해 전달되는 proxy header의 신뢰 범위 |
| `BACKEND_AUDIO_WORKERS` | `1` | 동시에 실행할 오디오 추출 작업 수, 자막 렌더는 별도 실행기에서 병행 |
| `GPU_PROMETHEUS_URL` | 빈 값 | STT 대시보드가 직접 조회할 Prometheus URL |
| `GPU_PROMETHEUS_TOKEN` | 빈 값 | 외부 Prometheus 프록시가 요구할 때만 사용하는 Bearer 토큰 |
| `GPU_METRICS_REFRESH_SECONDS` | `10` | STT 화면의 GPU 메트릭 갱신 및 서버 캐시 간격 |
| `GPU_METRICS_TIMEOUT_SECONDS` | `3` | Prometheus 조회 제한 시간 |
| `STT_BASE_URL` | `http://runtime:8100` | 선택적인 기본 전사 Runtime API 주소 |

### CUDA 전사

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `STT_STATE_PATH` | `/var/lib/homelab/stt-to-subtitle/stt-state` | 전사 작업 DB와 결과 |
| `STT_WORK_PATH` | `/data/work/stt-to-subtitle/stt-incoming` | 전사용 입력 WAV 작업 공간 |
| `MODEL_CACHE_PATH` | `/data/models/stt-to-subtitle` | Hugging Face·PyTorch·WhisperX·WhisperJAV 캐시 |
| `STT_RUNTIME_BASE_IMAGE` | `stt-to-subtitle-runtime-base:py311-cuda-v4` | Runtime이 재사용하는 고정 ML 기반 이미지 |
| `STT_RUNTIME_ID` | `builtin` | Runtime이 상태 API에 보고하는 고유 식별자 |
| `STT_RUNTIME_NAME` | `기본 Runtime` | Runtime 표시 이름 |
| `STT_API_TOKEN` | 빈 값 | Backend와 Runtime이 공유하는 선택적 Bearer 토큰 |
| `STT_RUNTIME_CACHE_PATH` | `/data/cache/buildkit/stt-runtime-py311-cuda-v4-20260825` | 외부에서 준비한 고비용 런타임 빌드 캐시 |
| `STT_DEVICE` | `cuda` | `cuda` 또는 `cuda:<index>` |
| `STT_DIARIZATION_DEVICE` | `cuda` | 화자 분리 장치, VRAM 절약 시 `cpu` |
| `STT_BATCH_SIZE` | `8` | Runtime별 재정의가 없을 때 쓰는 Kotoba 배치 크기 |
| `STT_THREADS` | `8` | Torch와 WhisperX 워커의 CPU 스레드 수 |
| `WHISPERX_BATCH_SIZE` | `8` | `whisperx`·`hybrid` 백엔드 배치 크기, 요청별 `batch_size`(1~64)로 재정의 가능 |
| `STT_CHUNK_PROGRESS_EVERY` | `10` | 청크 진행 로그 묶음 기준, `10` 또는 `100` (SSE 변경 알림은 매 변경 시 전송) |
| `STT_MODEL_IDLE_TIMEOUT_SECONDS` | `900` | 상주 중인 Kotoba 모델을 이만큼 유휴 상태가 지속되면 VRAM에서 내림. `0`은 큐가 비는 즉시, 음수는 계속 상주 |
| `WHISPERX_MODEL` | `large-v3` | WhisperX 모델 |
| `WHISPERX_LANGUAGE` | `ja` | WhisperX 언어 |
| `WHISPERX_COMPUTE_TYPE` | `float16` | WhisperX 연산 형식 |
| `NVIDIA_VISIBLE_DEVICES` | `all` | 컨테이너에 보이는 GPU |

요청한 CUDA 장치가 없거나 GPU 인덱스가 범위를 벗어나면 CPU로 자동
전환하지 않고 STT 준비 상태가 실패합니다. VRAM이 부족한 경우 먼저
`STT_DIARIZATION_DEVICE=cpu`를 사용합니다.

Web UI의 **설정 → 전사 서버**에서 기본 Runtime과 외부 Runtime마다 Kotoba와
WhisperX 배치 크기(1~64)를 따로 지정할 수 있습니다. 비워 두면 각 Runtime의
`STT_BATCH_SIZE`와 `WHISPERX_BATCH_SIZE`를 사용합니다. 설정값은 Runtime에
배정된 새 작업부터 요청 옵션으로 전달됩니다. Kotoba 배치가 현재 상주 모델과
다르면 다음 Kotoba 작업을 시작하기 전에 모델을 새 배치 크기로 다시 적재합니다.
하이브리드는 `batch_size`를 WhisperX에, `kotoba_batch_size`를 구조 복구용
Kotoba에 사용합니다. WhisperJAV는 별도의 배치 의미와 정확도 특성이 있어 이
공통 설정을 적용하지 않습니다. WhisperX 전사 중 CUDA 메모리가 부족하면 워커가
배치 크기를 절반씩 줄여 자동으로 재시도하고, 실제로 사용한 값을 결과의
`runtime.effective_batch_size`에 기록합니다.

`STT_DEBUG_ARTIFACTS=true`는 단계별 JSON에 민감한 전사문을 저장할 수
있으므로 기본적으로 꺼져 있습니다. 토큰은 이미지, 로그, 결과 메타데이터에
저장하지 않습니다.

## GPU 관측 사이드 프로젝트

`gpu-observability/`는 메인 Compose와 분리된 독립 프로젝트입니다. NVIDIA
DCGM Exporter, Prometheus와 Grafana를 함께 실행합니다. Backend는 Grafana로
이동하지 않고 Prometheus의 현재 GPU 사용률, 메모리, 온도와 전력을 조회해
API로 제공합니다.

GPU 관측은 Backend의 선택 기능입니다. Backend 이미지 빌드와 기동은
`gpu-observability`의 컨테이너나 네트워크를 검사하지 않습니다. 관측 스택의
Prometheus 포트를 Docker host gateway에 게시하고 `.env.compose`에 URL만
설정합니다.

```bash
GPU_PROMETHEUS_URL=http://host.docker.internal:9090
./scripts/compose.sh --env-file .env.compose up -d backend
```

`GPU_PROMETHEUS_URL`을 비워 두어도 `build backend`와 Backend 기동은 그대로
동작합니다. 실행 중 설정된 Prometheus가 응답하지 않아도 Backend는 계속
서비스하며 GPU 카드에 수집 실패만 표시합니다.
`GPU_PROMETHEUS_TOKEN`은 별도 리버스 프록시를 통해 Prometheus에 접속할 때만
필요합니다. 자세한 게시 주소와 Grafana 계정 설명은
`gpu-observability/README.md`를 참고하십시오.

## 전사 백엔드

웹의 작업 생성 화면에서 다음 백엔드를 요청별로 선택합니다.

- `auto`: JAV 번역 프롬프트면 WhisperJAV, 그 외에는 하이브리드를 선택합니다.
  명시적으로 선택한 백엔드는 자동 선택보다 우선합니다.
- `whisperjav`: anime-whisper/WhisperSeg 1차 패스와 일본어 Qwen3-ASR/TEN
  2차 패스를 직렬 실행해 병합하고, 최종 결과를 Qwen 강제 정렬한 뒤
  Pyannote 화자를 배정합니다.
- `kotoba`: Kotoba Whisper와 Pyannote를 사용합니다.
- `whisperx`: WhisperX VAD, alignment, diarization을 사용합니다.
- `hybrid`: WhisperX를 먼저 실행하고, 구조적으로 실패한 구간이 있을 때만
  Kotoba를 추가로 실행해 해당 구간을 교체합니다.

WhisperJAV, WhisperX와 하이브리드는 자체 VAD를 사용하므로 소음 필터를 끈
요청을 거부합니다. WhisperJAV는 두 ASR 패스, 강제 정렬, 화자 배정을 서로
분리된 단계로 직렬 실행해 모델의 동시 GPU 상주를 피합니다. 두 번째 패스가
실패해도 잡을 중단하지 않고 첫 번째 패스 결과로 진행하며, 이때 결과의
`quality.ensemble_status`가 `degraded`가 됩니다. 단계별 소요 시간은
`runtime.stage_elapsed`에 기록됩니다. 하이브리드도
Kotoba를 WhisperX가 끝난 뒤에 적재하므로 두 모델이 동시에 GPU에 상주하지
않습니다.

하이브리드에서 WhisperX 결과에 구조적 문제가 없으면 Kotoba 패스를 아예
실행하지 않습니다. 이때 결과에는 `runtime.kotoba_skipped=true`,
`runtime.rescue.skipped=true`, `runtime.rescue.elapsed_seconds=0.0`이
기록되고 최종 세그먼트는 WhisperX 결과와 동일합니다.

교체할 구간이 있을 때 Kotoba가 다루는 범위는 `hybrid_rescue.rescue_scope`로
정합니다. 기본값 `windows`는 패딩을 적용한 교체 구간만 잘라 디코딩하므로
문제 구간이 짧을수록 빠릅니다. 구간 단위로 화자 분리를 다시 수행한 뒤
정규화 경계에서 전역 라벨과 대응시키며, 전체 파일 재디코딩이 필요한 경우에는
`full`을 명시할 수 있습니다. `windows`로 실행하면 결과에
`runtime.rescue.scope="windows"`와
`noise_filter.rescue.window_count`, `noise_filter.rescue.decoded_seconds`가
기록됩니다.

전사 비교는 새 비교부터 WhisperJAV, 하이브리드, WhisperX, Kotoba 네 엔진을
실행합니다. 기존 3엔진 비교 이력은 저장 당시 엔진 구성 그대로 표시됩니다.
WhisperJAV의 모델과 상류 코드 리비전은 이미지·결과 메타데이터에 고정되며,
상용 사용 전에는 각 상류 모델의 라이선스를 별도로 확인해야 합니다. 번역은
기존 OpenAI 호환 번역 모델 설정을 그대로 사용하며 Qwen으로 바뀌지 않습니다.
현재 WhisperJAV 설정에서 바꿀 수 있는 값은 두 ASR 패스의 그룹 길이이며 ASR과
강제 정렬 모델은 고정된 recipe입니다. Ollama의 Gemma 같은 텍스트 전용 번역
모델은 음성 인코더나 강제 정렬기를 제공하지 않으므로 Qwen ASR을 대신할 수
없습니다. 이런 모델은 별도 전사문 교정 단계에는 쓸 수 있지만, 정답 전사본이
없는 경우 그럴듯한 오교정을 만들 수 있어 기본 전사 경로에는 포함하지 않습니다.

WhisperX는 alignment 이후 word 시각, 화자, score를 보존하고 화자 변경을
기준으로 자막 세그먼트를 재구성합니다. 하이브리드는 반복 폭주, 잘못된
문자, 과도하게 긴 word alignment, 시각 fallback 같은 구조적 문제만 자동
복구합니다. 자동 교체 근거가 부족하면 원문을 유지하고
`quality.hybrid.needs_review=true`로 표시합니다.

전사 API 계약은 다음과 같습니다.

- `POST /v1/transcriptions`: WAV와 JSON options 제출
- `GET /v1/transcriptions/{job_id}`: 상태 조회
- `GET /v1/transcriptions/{job_id}/result`: 완료 결과
- `GET /healthz`, `GET /readyz`: 생존·준비 상태

## 처리 흐름과 결과

```text
전사 작업:         영상 → WhisperJAV | Hybrid(WhisperX + Kotoba) | WhisperX → 전사본
1차 번역 작업:    완료된 전사본 → 1차 번역 generation → 내부 SRT/ASS 보존
2차 번역 작업:    전사본 + 1차 generation → 2차 generation → SRT/ASS 자동 게시
외부 모델 검토:   전사본 + 1차·2차 generation → 교정 generation → 수동 게시 대기
```

각 행은 독립적으로 생성·대기·실행·재시도되는 작업입니다. 단계 요청에는 앞 작업,
전사 revision, 입력 번역 generation, 입력 자막 generation 식별자를 고정하여 이후
설정이나 게시 자막이 바뀌어도 실행 입력이 달라지지 않습니다. Kotoba 단독 실행은
전사 비교 기능에서만 사용합니다. 동일한 부모 작업과 입력 generation에서는 같은
번역 단계를 두 번 생성할 수 없으며, 작업 상세는 이미 생성된 다음 단계로 이동하는
링크를 표시합니다. 실패한 단계는 새 작업을 추가하지 않고 기존 작업을 재시도합니다.
1차와 2차는 별도 작업 lane이지만 활성 라우트의 호스트가 겹치면 같은 호스트에서
동시에 실행하지 않습니다. 실행 가능한 1차 작업을 먼저 배정하며, 1차를
일시정지하면 해당 lane을 양보하므로 2차를 먼저 확인할 수 있습니다. 두 단계의
활성 호스트가 겹치지 않으면 서로 독립적으로 진행할 수 있습니다. 외부 검토는
로컬 모델 호스트를 사용하지 않는 별도 lane이므로 완료된 2차 입력이 있으면 로컬
번역 상태와 관계없이 실행합니다. 전사와 번역의 충돌도 모든 번역 서버가 아니라
`TRANSLATION_STT_HARD_BREAKER_HOSTS`에 지정한 공유 호스트에만 적용합니다. 같은
단계에서는 한 영상만 실행하고, 그 영상 안의 논리 배치만 선택 서버의 동시 요청
수만큼 병렬 처리합니다. OpenAI 호환 API에 공통 모델 수명주기 계약은 없으므로
모델 언로드와 사전 적재는 해당 모델 서버의 정책 또는 운영 절차를 따릅니다.

원격 번역 서버가 응답하지 않거나 처리 단계가 실패하면 작업은 `blocked`가
됩니다. 원인을 해결한 뒤 웹에서 수동 재시도하면 정상인 마지막 WAV·전사
JSON·번역 체크포인트부터 이어집니다.

번역은 generation별로 구분하며 논리 배치의 실행·성공·실패와 세그먼트 결과를
SQLite에 저장합니다. 부분 JSON은 DB 결과에서 다시 만들 수 있고, 프롬프트 변경
재번역과 JSON 직접 편집은 이전 결과를 보존한 새 generation으로 기록됩니다.
작업 상세의 **번역 이력**에서 generation별 JSON을 내려받을 수 있습니다.
작업 상세의 **1차/2차/3차** 스위치는 우측 자막 스크립트만 바꾸며 재생 중인
자막이나 미디어 옆 파일을 변경하지 않습니다. 기본 표시는 완료된 마지막 차수입니다.
선택한 결과를 실제 `.ko.srt/.ko.ass`로 반영하려면 별도의 **자막 파일 생성**을
눌러야 합니다. 2차 완료 결과만 기본 자막으로 자동 게시되고, 1차와 3차 결과는
사용자가 명시적으로 생성할 때까지 내부 generation으로 보존됩니다.

설정 화면의 **1차(초벌) 번역**과 **2차(검증) 번역**은 각각 모델 드롭다운과
독립 서버 목록을 가집니다. 한 그룹에 등록한 서버는 다른 그룹이나 전사 Runtime에
자동 등록되지 않습니다. 단건 번역은 해당 그룹에서 ON인 서버의 여유 슬롯으로
분산되고 실패 시 같은 그룹의 다음 서버로 전환됩니다. 여러 작업을 함께 등록한
일괄 번역은 해당 그룹에서 `일괄 작업 우선`으로 지정한 서버만 사용합니다.

프롬프트 카테고리는 장르별 **1차(초벌) 번역 프롬프트**와 **2차(검사·교정)
프롬프트**를 하나의 revision 쌍으로 보존합니다. 1차는 인접 원문과 시간·화자
힌트를 이용해 잘못 잘린 발화를 논리적으로 재구성하고, ID와 시간을 바꾸지 않은
채 보수적인 직역 초안을 만듭니다. 2차는 원문과 1차 초안을 독립적으로 대조해
문맥·뉘앙스·어휘·말투와 한국어 자막 가독성을 편집합니다. 외부 교정은 원문,
현재 2차 결과와 1차 결과를 ID별로 비교해 2차의 개선과 회귀를 판정한 뒤 필요한
최소 수정만 적용합니다. 세 단계는 한 요청에서 연속 실행되지 않습니다. 각
단계의 검증 실패 시 해당 배치에서 마지막으로 성공한 번역을 보존합니다.

큰 검증 모델을 적재할 수 없는 기본 서버는 2차 그룹에서 OFF로 두면 모델 로드와
요청 자체가 발생하지 않습니다. 현재 예시 설정의 1차 모델은
`gemma-4-12b-coder-fable5-composer2.5-v1-uncensored-heretic`이며, 2차 모델은
`gemma-4-26b-a4b-it-ultra-uncensored-heretic`를 선택할 수 있습니다. 외부 모델
검토는 로컬 2단계와 분리되어 있으며 설정된 경우에도 사용자가 명시적으로 요청할
때만 실행합니다. 설정 화면에서 OpenRouter, AWS Bedrock, NVIDIA Build 프로필에
API 키 또는 credential을 저장하고 연결 점검으로 인증과 모델 목록을 확인한 뒤
사용할 모델을 선택합니다. 외부 모델 검토 결과는 별도 generation으로 보존하며
자동 게시하지 않습니다. 작업 상세에서 결과를 확인한 뒤 명시적으로 게시합니다.

렌더된 SRT/ASS도 자막 generation으로 보존합니다. 미디어 옆 `.ko.srt/.ko.ass`는
현재 게시 generation의 복사본이며, 작업 상세의 **자막 이력**에서 이전 버전을
내려받거나 두 파일을 함께 다시 게시할 수 있습니다. 게시 전에는 저장된 파일
해시를 검증합니다.

작업 상세의 자막 스크립트는 항상 완료된 마지막 번역 차수를 기본으로 표시합니다.
자막 문장을 누르면 인접 차수의 변경 내용을 펼쳐 보고 선택한 차수의 문장을 직접
수정할 수 있습니다. 수동 수정은 새 immutable translation/subtitle generation으로
저장되며 다음 차수를 자동 재실행하거나 미디어 옆 파일을 자동 교체하지 않습니다.
실제 파일 반영은 **자막 파일 생성**을 명시적으로 눌렀을 때만 수행합니다. 웹
재생용 VTT는 SRT의 화자 색상을 WebVTT cue class로 변환하므로 SRT의 `<font>`
태그를 화면 텍스트로 노출하지 않습니다.

최종 결과는 원본 영상 옆의 `<이름>.ko.srt`와 `<이름>.ko.ass`입니다.
실제 다른 화자의 동시 발화는 유지하고 같은 화자의 겹친 행은 새 발화로
교체합니다. 번역 세그먼트 ID는 렌더링까지 변경하지 않습니다. 기존 자막은
사용자가 강제 덮어쓰기를 선택한 경우에만 교체합니다.

같은 위치의 `<이름>.srt`, `<이름>.vtt`, `<이름>.ass`는 한국어 **외부
자막**으로 구분합니다. 외부 자막은 생성 자막보다 먼저 재생되며 미디어의 작업
완료 수에는 포함되지 않습니다. 작업 화면에서 생성 자막과 시간 coverage·문장
유사도를 비교할 수 있습니다. 외부 모델 검증은 설정 화면에 별도 API와 모델을
저장한 뒤 사용자가 요청한 경우에만 1회 호출하며, 같은 파일 해시·모델 입력은
저장된 결과를 재사용합니다.

작업 상세의 결과 플레이어는 일반 보기와 **180° 단안 미리보기**를 같은 화면에서
전환합니다. 데스크톱 단안 미리보기는 좌우(SBS) 원본의 왼쪽 눈 영상을 반구에
투영하며, 양안 분할 화면을 그대로 표시하지 않습니다. WebXR 헤드셋 모드에서만
좌우 눈 영상을 각각 해당 눈에 출력합니다. 헤드셋 재생에는 HTTPS와
`immersive-vr`를 지원하는 브라우저·기기가 필요합니다.

마운트 용도는 다음과 같습니다.

- `${MEDIA_PATH}:/media:rw`: 영상 조회 및 원본 옆 자막 저장
- `${BACKEND_STATE_PATH}:/var/lib/stt:rw`: 작업 DB와 영속 상태
- `${BACKEND_WORK_PATH}:/var/lib/stt-work:rw`: 작업 WAV와 재생성 가능한 체크포인트
- `${STT_STATE_PATH}:/var/lib/stt:rw`: STT 작업 DB와 결과
- `${STT_WORK_PATH}:/var/lib/stt-work:rw`: 전사용 입력 WAV 작업 공간
- `${MODEL_CACHE_PATH}:/var/cache/stt:rw`: 모델 캐시

기존 배포를 이전할 때는 서비스를 먼저 중지합니다. 웹 `jobs.sqlite3`는
`BACKEND_STATE_PATH`로, 기존 웹 `jobs/`의 내용은
`BACKEND_WORK_PATH`로 옮깁니다. 기존 배포의 `WEB_STATE_PATH`,
`WEB_WORK_PATH`, `WEB_PUID`, `WEB_PGID`, `WEB_AUDIO_WORKERS`는 Compose
이전 별칭으로만 계속 인식합니다.
STT의 `jobs.sqlite3`와 `results/`는 `STT_STATE_PATH`에 유지하고,
`incoming/`의 내용은 `STT_WORK_PATH`로 옮깁니다. 첫 시작 시 DB에 저장된
기존 `/var/lib/stt/jobs` 및 `/var/lib/stt/incoming` 경로는 새 비중첩 작업
경로로 자동 변환됩니다. 데이터베이스 스키마와 파일명은 변경하지 않습니다.

## 호스트 전사 Runtime (Apple Silicon MPS 예시)

Apple Silicon에서는 전사 Runtime을 호스트 프로세스로 실행할 수 있습니다.
이 경로는 CUDA Compose 배포와 별개이며 Metal/MPS를 사용하는 경우에만
필요합니다.

```bash
brew install python@3.11 ffmpeg libsndfile portaudio
cp .env.stt.example .env.stt
# .env.stt의 HF_TOKEN을 설정
./scripts/run-stt-runtime.sh
```

독립 실행 폴더가 필요하면 다음 도구를 사용합니다.

```bash
python3 scripts/create_stt_runtime.py /path/to/runtime
cd /path/to/runtime
./setup.sh
./run.sh
```

## 운영

소스가 변경되면 이미지를 다시 로컬 빌드해 재생성합니다.

```bash
./scripts/compose.sh --env-file .env.compose build
./scripts/compose.sh --env-file .env.compose up -d
```

소스 변경만으로는 `runtime-base`를 다시 빌드하지 않습니다. ML 요구사항이나
기반 런타임을 변경한 경우에만 새 `STT_RUNTIME_BASE_IMAGE` 태그를 지정하고 다음을
실행합니다.

```bash
./scripts/compose.sh --env-file .env.compose --profile build build runtime-base
```

자주 사용하는 명령은 다음과 같습니다.

```bash
./scripts/compose.sh --env-file .env.compose logs --tail=200 web backend runtime
./scripts/compose.sh --env-file .env.compose restart web backend runtime
./scripts/compose.sh --env-file .env.compose down
```

`down`은 바인드 마운트된 상태·캐시·미디어를 삭제하지 않습니다.

업그레이드 전 JobStore migration은 원본을 수정하지 않는 임시 SQLite backup에서
검증할 수 있습니다. 결과에는 적용 예정 migration과 quick/FK check만 포함되며
서버 설정·토큰·원본 경로는 출력하지 않습니다.

```bash
stt-check-migrations /path/to/jobs.sqlite3
```

Compose 운영 DB는 Backend entrypoint를 덮어써 같은 UID/GID와 볼륨에서
검사합니다.

```bash
docker compose run --rm --entrypoint python backend \
  -m stt_to_subtitle.migration_check \
  /var/lib/stt/jobs.sqlite3
```

checkpoint가 끝난 WAL 없는 DB는 immutable read-only로 열어 원본 디렉터리에
`-wal`/`-shm` 파일을 만들지 않습니다. 실행 중 WAL이 있으면 기존 sidecar를
통해 읽으며, WAL만 있고 shared-memory 파일이 없으면 원본 변경 대신 검사를
중단합니다.

운영 스냅샷은 `/api/v1/operations/metrics` JSON과
`/api/v1/operations/metrics/prometheus` Prometheus text로 제공합니다.
전사·번역 단계 소요 시간은
`/api/v1/operations/metrics/media-durations`에서 기본 최근 30일을 영상 길이별로
조회할 수 있습니다. 실제 영상 길이는 가장 가까운 15분 단위로 정규화합니다.
Web UI에서는 왼쪽 메뉴의 **통계**(`/metrics`)에서 같은 집계를 확인할 수 있습니다.
예를 들어 약 14분과 16분은 15분 구간, 약 28분과 32분은 30분 구간에
집계됩니다. `window_days`로 조회 기간을 조정할 수 있습니다.

전사는 영상 길이 구간과 `runtime_id`별로 분리합니다. 번역은 Runtime 구분 없이
영상 길이 구간별 전체 소요 시간과 초벌(`draft`)·검증(`review`) 요청 활성 시간을
각각 제공합니다. 병렬 번역 요청이 겹친 시간은 한 번만 계산합니다. 표본 수,
최소, 평균, P50, P95, 최대를 함께 제공하며 완료, 실패, 중지, 일시정지, 차단 및
Runtime 전환은 서로 다른 `outcome`으로 집계합니다.

Prometheus는 새 단계 종료부터 누적
`stt_to_subtitle_stage_duration_seconds_count`와
`stt_to_subtitle_stage_duration_seconds_sum`을 수집합니다. 예를 들어 영상 길이
구간 및 Runtime별 전사 완료 평균은 다음 PromQL로 계산합니다.

```promql
sum by (media_duration_bucket_minutes, runtime_id) (
  stt_to_subtitle_stage_duration_seconds_sum{
    phase="transcription", outcome="completed"
  }
)
/
sum by (media_duration_bucket_minutes, runtime_id) (
  stt_to_subtitle_stage_duration_seconds_count{
    phase="transcription", outcome="completed"
  }
)
```

초벌·검증 번역의 영상 길이 구간별 평균 활성 시간은
`stt_to_subtitle_translation_pass_active_seconds_sum`을
`stt_to_subtitle_translation_pass_active_seconds_count`로 나누어 계산합니다.

애플리케이션 인증은 두지 않으므로 신뢰할 수 있는 내부망에서만 실행합니다.
Web만 HTTPS reverse proxy에 연결하고 Backend와 Runtime 포트는 호스트에
게시하지 않습니다.

## 개발 검증

모델을 다운로드하지 않는 검증 명령은 다음과 같습니다.

```bash
make test
make check
./scripts/compose.sh --env-file .env.compose.example config
```

실제 품질 테스트에는 합법적으로 사용할 수 있는 짧은 미디어만 사용하고,
민감한 전사문이나 모델 캐시를 커밋하지 마십시오.
