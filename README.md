# STT to Subtitle

영상에서 일본어 음성을 전사하고 OpenAI 호환 번역 서버로 한국어를 생성한 뒤,
화자별 SRT·ASS 자막을 저장하는 로컬 웹 애플리케이션입니다.

NVIDIA GPU 배포에서는 웹 오케스트레이터와 CUDA 전사 API를 하나의 Docker
Compose 프로젝트로 빌드하고 실행합니다. 애플리케이션 이미지를 외부
레지스트리에서 받거나 게시하지 않습니다.

## 구성

```text
브라우저 ──▶ Traefik(HTTPS) ──▶ web:8080
                                  │
                                  ├─ FFmpeg 오디오 추출
                                  ├─ 작업·번역·자막 렌더링
                                  │
                                  ├──▶ stt:8100
                                  │      Kotoba + Pyannote
                                  │      WhisperX + Pyannote
                                  │      WhisperJAV + Qwen alignment + Pyannote
                                  │
                                  └──▶ 외부 OpenAI 호환 번역 API
```

Compose 프로젝트에는 두 컨테이너가 있습니다.

- `web`: 미디어 탐색, 작업 상태, FFmpeg 추출, 번역, SRT·ASS 렌더링
- `stt`: WhisperJAV, Kotoba, WhisperX, 하이브리드 전사를 제공하는 CUDA
  FastAPI 서버

두 서비스는 Compose 내부 네트워크의 `http://stt:8100`으로 연결됩니다.
전사 API와 웹 포트는 호스트에 직접 게시하지 않습니다. 웹 서비스만 기존
Traefik 외부 네트워크에 연결되고 HTTPS 라우터를 통해 제공됩니다.

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
- `OPENAI_COMPATIBLE_BASE_URL`: 번역 API 루트
- `OPENAI_COMPATIBLE_MODEL`: 번역 모델 ID

Compose는 경로 오타로 빈 호스트 디렉터리를 만들지 않습니다. 미디어, 상태,
작업 공간과 모델 캐시 디렉터리를 먼저 만들고 쓰기 권한을 확인합니다.

```bash
mkdir -p ./media \
  /var/lib/homelab/stt-to-subtitle/web-state \
  /var/lib/homelab/stt-to-subtitle/stt-state \
  /data/work/stt-to-subtitle/web-jobs \
  /data/work/stt-to-subtitle/stt-incoming \
  /data/models/stt-to-subtitle
test -w ./media
test -w /var/lib/homelab/stt-to-subtitle/web-state
test -w /var/lib/homelab/stt-to-subtitle/stt-state
test -w /data/work/stt-to-subtitle/web-jobs
test -w /data/work/stt-to-subtitle/stt-incoming
test -w /data/models/stt-to-subtitle
```

고정 STT 실행 환경 이미지는 Python, CUDA 라이브러리와 서로 격리된 Kotoba,
WhisperX, WhisperJAV 환경을 포함합니다. 애플리케이션 `stt` 이미지는 이 기반
이미지 위에 애플리케이션 wheel만 설치합니다. 모델 가중치는 두 이미지에
포함하지 않습니다. 기반 런타임 이미지를 준비하려면 다음 명령을 사용합니다.

```bash
./scripts/compose.sh --env-file .env.compose --profile build build stt-runtime
```

이후 애플리케이션 이미지를 빌드하고 두 서비스를 함께 실행합니다.

```bash
./scripts/compose.sh --env-file .env.compose config
./scripts/compose.sh --env-file .env.compose build
./scripts/compose.sh --env-file .env.compose up -d
./scripts/compose.sh --env-file .env.compose ps
```

`scripts/compose.sh`는 현재 실행 계정의 UID/GID를 두 컨테이너에 주입하며
root 실행은 거부합니다. 따라서 `.env.compose`에 UID/GID를 설정할 필요가
없고, 마운트 경로를 소유한 일반 사용자로 실행해야 합니다.

`stt-runtime` 이미지는 세 ML 환경을 모두 설치하므로 최초 빌드 시간이 길고
이미지가 클 수 있습니다. 일반 `build`는 이 고정 이미지를 재사용하고
애플리케이션 코드만 설치합니다. 요구사항 파일이나 Python/CUDA 기반 환경을
변경할 때만 `STT_RUNTIME_IMAGE` 태그를 올리고 `stt-runtime`을 다시
빌드합니다. 모델 가중치는 이미지에 포함하지 않으며 최초 전사 요청 때
`${MODEL_CACHE_PATH}`로 내려받습니다.
`STT_RUNTIME_CACHE_PATH`는 외부에서 준비한 BuildKit 로컬 캐시를 읽는 경로이며,
Compose 빌드 자체는 이 캐시를 갱신하지 않습니다.

상태를 확인합니다.

```bash
curl https://stt.example.com/healthz
./scripts/compose.sh --env-file .env.compose exec stt \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8100/readyz').read().decode())"
```

브라우저에서 `https://TRAEFIK_HOST`를 열고 **서버 설정**에서 번역
서버를 확인합니다. Compose의 전사 API 주소는 내부 서비스
`http://stt:8100`으로 고정되므로 환경 파일에서 설정하지 않습니다. 설정은
웹 상태 디렉터리의 `jobs.sqlite3`에 저장되며 컨테이너를 다시 만들어도
유지됩니다. 번역 PC를 켠 뒤 설정 화면의 **번역 시작/재개**를 눌러야 대기
번역을 시작하며, 서버를 주기적으로 자동 호출하지 않습니다.

웹 화면은 역할별로 분리됩니다. `/`는 상태 요약과 최근 작업만 보여 주는
대시보드이고, `/media`는 파일 탐색과 신규 작업 등록, `/jobs`는 상태별 작업
목록입니다. 대시보드의 2D/3D 전환 상태는 브라우저 쿠키에 저장됩니다. 3D 모드는
같은 작업·GPU 데이터를 아이소메트릭 파이프라인으로 표시하며, WebGPU가 지원되지
않으면 WebGL2로 폴백합니다. 모든 화면에서 사이드 메뉴로 각 영역을 직접 이동할
수 있습니다.

전사 상태는 고정 간격으로 조회하지 않습니다. STT 저장소 변경 Hook이 작업별
SSE 스트림으로 상태를 보내고, 웹 오케스트레이터의 변경 Hook이 다시 브라우저
SSE에 전달합니다. 번역도 배치 저장 시 같은 경로로 즉시 반영됩니다. 오디오
추출이 끝나면 WAV 헤더의 재생 시간과 모델 청크 길이로 전체 전사 청크를 먼저
추정하며, 실제 생성 청크가 추정치를 넘으면 진행률의 전체 수를 자동 보정합니다.

## 환경 설정

### 공통 및 웹

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `MEDIA_PATH` | `./media` | 입력 영상과 생성 자막 |
| `WEB_STATE_PATH` | `/var/lib/homelab/stt-to-subtitle/web-state` | 작업 DB와 영속 상태 |
| `WEB_WORK_PATH` | `/data/work/stt-to-subtitle/web-jobs` | 작업 WAV와 재생성 가능한 JSON 체크포인트 |
| `WEB_PUID` | `1026` | 웹 컨테이너 프로세스 UID |
| `WEB_PGID` | `100` | 웹 컨테이너 프로세스 GID |
| `TRAEFIK_HOST` | 필수 | 웹 HTTPS 라우터의 DNS 호스트명 |
| `TRAEFIK_NETWORK` | `proxy` | Traefik이 연결된 외부 Docker 네트워크 |
| `TRAEFIK_ENTRYPOINT` | `websecure` | Traefik HTTPS entrypoint |
| `TRAEFIK_CERT_RESOLVER` | `letsencrypt` | Traefik 인증서 resolver |
| `WEB_FORWARDED_ALLOW_IPS` | `*` | 호스트에 직접 게시되지 않은 웹 컨테이너에서 신뢰할 프록시 주소 |
| `WEB_ADMIN_PASSWORD` | 빈 값 | 웹 로그인 비밀번호 |
| `WEB_SESSION_SECRET` | 빈 값 | 로그인 사용 시 필요한 32자 이상 세션 키 |
| `WEB_SECURE_COOKIE` | `true` | HTTPS에서만 세션 쿠키 전송 |
| `WEB_AUDIO_WORKERS` | `1` | 동시에 실행할 오디오 추출 작업 수, 자막 렌더는 별도 실행기에서 병행 |
| `LM_MANUAL_START` | `true` | 번역 서버를 명시적으로 시작/재개할 때만 번역 dispatch 허용 |
| `GPU_PROMETHEUS_URL` | 빈 값 | STT 대시보드가 직접 조회할 Prometheus URL |
| `GPU_PROMETHEUS_TOKEN` | 빈 값 | 외부 Prometheus 프록시가 요구할 때만 사용하는 Bearer 토큰 |
| `GPU_METRICS_REFRESH_SECONDS` | `10` | STT 화면의 GPU 메트릭 갱신 및 서버 캐시 간격 |
| `GPU_METRICS_TIMEOUT_SECONDS` | `3` | Prometheus 조회 제한 시간 |
| `GPU_MONITORING_NETWORK` | `gpu-monitoring` | 두 Compose 프로젝트가 공유하는 내부 Docker 네트워크 |

`WEB_ADMIN_PASSWORD`를 설정하면 `WEB_SESSION_SECRET`도 반드시 32자
이상으로 설정해야 합니다. 다음과 같이 생성할 수 있습니다.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

### CUDA 전사

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `STT_STATE_PATH` | `/var/lib/homelab/stt-to-subtitle/stt-state` | 전사 작업 DB와 결과 |
| `STT_WORK_PATH` | `/data/work/stt-to-subtitle/stt-incoming` | 전사용 입력 WAV 작업 공간 |
| `MODEL_CACHE_PATH` | `/data/models/stt-to-subtitle` | Hugging Face·PyTorch·WhisperX·WhisperJAV 캐시 |
| `STT_RUNTIME_CACHE_PATH` | `/data/cache/buildkit/stt-runtime-py311-cuda-v4-20260825` | 외부에서 준비한 고비용 런타임 빌드 캐시 |
| `STT_DEVICE` | `cuda` | `cuda` 또는 `cuda:<index>` |
| `STT_DIARIZATION_DEVICE` | `cuda` | 화자 분리 장치, VRAM 절약 시 `cpu` |
| `STT_BATCH_SIZE` | `8` | Kotoba 파이프라인 배치 크기, 로드 시점에 고정 |
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

`WHISPERX_BATCH_SIZE`는 `whisperx`와 `hybrid` 백엔드에만 적용됩니다. Kotoba
파이프라인은 로드 시점에 배치 크기를 고정하므로 요청별 `batch_size`를 보내면
거부되며, `STT_BATCH_SIZE`로만 조정합니다. WhisperX 전사 중 CUDA 메모리가
부족하면 워커가 배치 크기를 절반씩 줄여 자동으로 재시도하고, 실제로 사용한
값을 결과의 `runtime.effective_batch_size`에 기록합니다.

`STT_DEBUG_ARTIFACTS=true`는 단계별 JSON에 민감한 전사문을 저장할 수
있으므로 기본적으로 꺼져 있습니다. 토큰은 이미지, 로그, 결과 메타데이터에
저장하지 않습니다.

## GPU 관측 사이드 프로젝트

`gpu-observability/`는 메인 Compose와 분리된 독립 프로젝트입니다. NVIDIA
DCGM Exporter, Prometheus와 Grafana를 함께 실행합니다. STT 웹은 Grafana로
이동하지 않고 Prometheus의 현재 GPU 사용률, 메모리, 온도와 전력을 직접
조회해 메인 대시보드에 표시합니다.

관측 스택을 먼저 실행해 `gpu-monitoring` 네트워크를 만든 뒤, 메인 프로젝트에
전용 Compose 오버레이를 함께 적용합니다.

```bash
docker compose \
  -f compose.yaml \
  -f compose.gpu-monitoring.yaml \
  up -d --build web
```

이 구성에서는 기본 `GPU_PROMETHEUS_URL`이 `http://prometheus:9090`입니다.
Prometheus와 DCGM Exporter는 호스트 포트를 공개하지 않으며 두 프로젝트는
공유 내부 네트워크로만 통신합니다. `GPU_PROMETHEUS_TOKEN`은 별도 리버스
프록시를 통해 Prometheus에 접속할 때만 필요합니다. 자세한 실행 및 Grafana
계정 설명은 `gpu-observability/README.md`를 참고하십시오.

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
queued → extracting → audio_ready
       → transcription_running → transcribed
       → translation_running → translated
       → rendering → completed
```

원격 번역 서버가 응답하지 않거나 처리 단계가 실패하면 작업은 `blocked`가
됩니다. 원인을 해결한 뒤 웹에서 수동 재시도하면 정상인 마지막 WAV·전사
JSON·번역 체크포인트부터 이어집니다.

번역은 generation별로 구분하며 논리 배치의 실행·성공·실패와 세그먼트 결과를
SQLite에 저장합니다. 부분 JSON은 DB 결과에서 다시 만들 수 있고, 프롬프트 변경
재번역과 JSON 직접 편집은 이전 결과를 보존한 새 generation으로 기록됩니다.
작업 상세의 **번역 이력**에서 generation별 JSON을 내려받을 수 있습니다.

렌더된 SRT/ASS도 자막 generation으로 보존합니다. 미디어 옆 `.ko.srt/.ko.ass`는
현재 게시 generation의 복사본이며, 작업 상세의 **자막 이력**에서 이전 버전을
내려받거나 두 파일을 함께 다시 게시할 수 있습니다. 게시 전에는 저장된 파일
해시를 검증합니다.

최종 결과는 원본 영상 옆의 `<이름>.ko.srt`와 `<이름>.ko.ass`입니다.
실제 다른 화자의 동시 발화는 유지하고 같은 화자의 겹친 행은 새 발화로
교체합니다. 번역 세그먼트 ID는 렌더링까지 변경하지 않습니다. 기존 자막은
사용자가 강제 덮어쓰기를 선택한 경우에만 교체합니다.

같은 위치의 `<이름>.srt`, `<이름>.vtt`, `<이름>.ass`는 한국어 **외부
자막**으로 구분합니다. 외부 자막은 생성 자막보다 먼저 재생되며 미디어의 작업
완료 수에는 포함되지 않습니다. 작업 화면에서 생성 자막과 시간 coverage·문장
유사도를 비교할 수 있습니다. 상용 LLM 검증은 설정 화면에 별도 API와 모델을
저장한 뒤 사용자가 요청한 경우에만 1회 호출하며, 같은 파일 해시·모델 입력은
저장된 결과를 재사용합니다.

마운트 용도는 다음과 같습니다.

- `${MEDIA_PATH}:/media:rw`: 영상 조회 및 원본 옆 자막 저장
- `${WEB_STATE_PATH}:/var/lib/stt:rw`: 작업 DB와 영속 상태
- `${WEB_WORK_PATH}:/var/lib/stt-work:rw`: 작업 WAV와 재생성 가능한 체크포인트
- `${STT_STATE_PATH}:/var/lib/stt:rw`: STT 작업 DB와 결과
- `${STT_WORK_PATH}:/var/lib/stt-work:rw`: 전사용 입력 WAV 작업 공간
- `${MODEL_CACHE_PATH}:/var/cache/stt:rw`: 모델 캐시

기존 배포를 이전할 때는 서비스를 먼저 중지합니다. 웹 `jobs.sqlite3`는
`WEB_STATE_PATH`로, 기존 웹 `jobs/`의 내용은 `WEB_WORK_PATH`로 옮깁니다.
STT의 `jobs.sqlite3`와 `results/`는 `STT_STATE_PATH`에 유지하고,
`incoming/`의 내용은 `STT_WORK_PATH`로 옮깁니다. 첫 시작 시 DB에 저장된
기존 `/var/lib/stt/jobs` 및 `/var/lib/stt/incoming` 경로는 새 비중첩 작업
경로로 자동 변환됩니다. 데이터베이스 스키마와 파일명은 변경하지 않습니다.

## 호스트 STT API (Apple Silicon MPS 예시)

Apple Silicon에서는 전사 API를 호스트 프로세스로 실행할 수 있습니다.
이 경로는 CUDA Compose 배포와 별개이며 Metal/MPS를 사용하는 경우에만
필요합니다.

```bash
brew install python@3.11 ffmpeg libsndfile portaudio
cp .env.stt.example .env.stt
# .env.stt의 HF_TOKEN을 설정
./scripts/run-stt-api.sh
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

소스 변경만으로는 `stt-runtime`을 다시 빌드하지 않습니다. ML 요구사항이나
기반 런타임을 변경한 경우에만 새 `STT_RUNTIME_IMAGE` 태그를 지정하고 다음을
실행합니다.

```bash
./scripts/compose.sh --env-file .env.compose --profile build build stt-runtime
```

자주 사용하는 명령은 다음과 같습니다.

```bash
./scripts/compose.sh --env-file .env.compose logs --tail=200 web stt
./scripts/compose.sh --env-file .env.compose restart web stt
./scripts/compose.sh --env-file .env.compose down
```

`down`은 바인드 마운트된 상태·캐시·미디어를 삭제하지 않습니다.

업그레이드 전 JobStore migration은 원본을 수정하지 않는 임시 SQLite backup에서
검증할 수 있습니다. 결과에는 적용 예정 migration과 quick/FK check만 포함되며
서버 설정·토큰·원본 경로는 출력하지 않습니다.

```bash
stt-check-migrations /path/to/jobs.sqlite3
```

Compose 운영 DB는 웹 entrypoint를 덮어써 같은 UID/GID와 볼륨에서 검사합니다.
`--entrypoint`를 생략하면 `stt-web`이 잘못 전달된 명령 인자를 거부합니다.

```bash
docker compose run --rm --entrypoint stt-check-migrations web \
  /var/lib/stt/jobs.sqlite3
```

checkpoint가 끝난 WAL 없는 DB는 immutable read-only로 열어 원본 디렉터리에
`-wal`/`-shm` 파일을 만들지 않습니다. 실행 중 WAL이 있으면 기존 sidecar를
통해 읽으며, WAL만 있고 shared-memory 파일이 없으면 원본 변경 대신 검사를
중단합니다.

운영 스냅샷은 `/api/operations/metrics` JSON과
`/api/operations/metrics/prometheus` Prometheus text로 제공합니다. 웹 인증을
켠 구성에서는 두 경로에도 인증이 필요합니다.

웹 로그인을 사용하지 않는 구성은 신뢰할 수 있는 사설망 또는 VPN에서만
실행하십시오. 인터넷에 직접 노출할 때는 HTTPS reverse proxy,
`WEB_ADMIN_PASSWORD`, `WEB_SESSION_SECRET`, `WEB_SECURE_COOKIE=true`를
설정합니다. STT 포트는 호스트에 게시하지 않습니다.

## 개발 검증

모델을 다운로드하지 않는 검증 명령은 다음과 같습니다.

```bash
make test
make check
./scripts/compose.sh --env-file .env.compose.example config
```

실제 품질 테스트에는 합법적으로 사용할 수 있는 짧은 미디어만 사용하고,
민감한 전사문이나 모델 캐시를 커밋하지 마십시오.
