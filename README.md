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
                                  │
                                  └──▶ 외부 OpenAI 호환 번역 API
```

Compose 프로젝트에는 두 컨테이너가 있습니다.

- `web`: 미디어 탐색, 작업 상태, FFmpeg 추출, 번역, SRT·ASS 렌더링
- `stt`: Kotoba, WhisperX, 하이브리드 전사를 제공하는 CUDA FastAPI 서버

두 서비스는 Compose 내부 네트워크의 `http://stt:8100`으로 연결됩니다.
전사 API와 웹 포트는 호스트에 직접 게시하지 않습니다. 웹 서비스만 기존
Traefik 외부 네트워크에 연결되고 HTTPS 라우터를 통해 제공됩니다.

Kotoba와 WhisperX는 요구하는 PyTorch·Pyannote 버전이 다르므로 STT 이미지
안에서도 각각 `/opt/venvs/kotoba`와 `/opt/venvs/whisperx`에 설치됩니다.
HTTP 서버는 Kotoba 환경에서 실행하고 WhisperX 요청만 격리된 Python 작업
프로세스로 처리합니다.

## 요구 사항

- Linux/amd64 호스트
- Docker Engine과 Compose 플러그인
- Docker provider가 활성화된 Traefik과 외부 Docker 네트워크
- NVIDIA GPU, 호환 드라이버, NVIDIA Container Toolkit
- Pyannote 모델 이용 조건을 승인한 Hugging Face read 토큰
- 일본어→한국어 모델을 제공하는 OpenAI 호환 API

별도의 CUDA Toolkit이나 호스트 Python 환경은 필요하지 않습니다. macOS의
Docker 가상 머신은 Metal/MPS 장치를 PyTorch 컨테이너에 전달하지 않으므로
Mac MPS 전사는 아래의 네이티브 실행 경로를 사용합니다.

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

상태와 모델 캐시 디렉터리를 만들고 쓰기 권한을 확인합니다.

```bash
mkdir -p ./var/web-state ./var/stt-state ./var/model-cache
test -w ./var/web-state
test -w ./var/stt-state
test -w ./var/model-cache
```

이미지를 로컬에서 빌드하고 두 서비스를 함께 실행합니다.

```bash
./scripts/compose.sh --env-file .env.compose config
./scripts/compose.sh --env-file .env.compose build
./scripts/compose.sh --env-file .env.compose up -d
./scripts/compose.sh --env-file .env.compose ps
```

`scripts/compose.sh`는 현재 실행 계정의 UID/GID를 두 컨테이너에 주입하며
root 실행은 거부합니다. 따라서 `.env.compose`에 UID/GID를 설정할 필요가
없고, 마운트 경로를 소유한 일반 사용자로 실행해야 합니다.

STT 이미지는 두 ML 환경을 모두 설치하므로 최초 빌드 시간이 길고 이미지가
클 수 있습니다. 모델 가중치는 이미지에 포함하지 않으며 최초 전사 요청 때
`${MODEL_CACHE_PATH}`로 내려받습니다.

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
유지됩니다.

## 환경 설정

### 공통 및 웹

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `MEDIA_PATH` | `./media` | 입력 영상과 생성 자막 |
| `WEB_STATE_PATH` | `./var/web-state` | 작업 DB, WAV, JSON 체크포인트 |
| `TRAEFIK_HOST` | 필수 | 웹 HTTPS 라우터의 DNS 호스트명 |
| `TRAEFIK_NETWORK` | `proxy` | Traefik이 연결된 외부 Docker 네트워크 |
| `TRAEFIK_ENTRYPOINT` | `websecure` | Traefik HTTPS entrypoint |
| `TRAEFIK_CERT_RESOLVER` | `letsencrypt` | Traefik 인증서 resolver |
| `WEB_ADMIN_PASSWORD` | 빈 값 | 웹 로그인 비밀번호 |
| `WEB_SESSION_SECRET` | 빈 값 | 로그인 사용 시 필요한 32자 이상 세션 키 |
| `WEB_SECURE_COOKIE` | `true` | HTTPS에서만 세션 쿠키 전송 |

`WEB_ADMIN_PASSWORD`를 설정하면 `WEB_SESSION_SECRET`도 반드시 32자
이상으로 설정해야 합니다. 다음과 같이 생성할 수 있습니다.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

### CUDA 전사

| 변수 | 기본값 | 용도 |
| --- | --- | --- |
| `STT_STATE_PATH` | `./var/stt-state` | 전사 작업 DB와 결과 |
| `MODEL_CACHE_PATH` | `./var/model-cache` | Hugging Face·PyTorch·WhisperX 캐시 |
| `STT_DEVICE` | `cuda` | `cuda` 또는 `cuda:<index>` |
| `STT_DIARIZATION_DEVICE` | `cuda` | 화자 분리 장치, VRAM 절약 시 `cpu` |
| `STT_BATCH_SIZE` | `1` | 모델 배치 크기 |
| `STT_CHUNK_PROGRESS_EVERY` | `10` | 진행 보고 간격, `10` 또는 `100` |
| `WHISPERX_MODEL` | `large-v3` | WhisperX 모델 |
| `WHISPERX_LANGUAGE` | `ja` | WhisperX 언어 |
| `WHISPERX_COMPUTE_TYPE` | `float16` | WhisperX 연산 형식 |
| `NVIDIA_VISIBLE_DEVICES` | `all` | 컨테이너에 보이는 GPU |

요청한 CUDA 장치가 없거나 GPU 인덱스가 범위를 벗어나면 CPU로 자동
전환하지 않고 STT 준비 상태가 실패합니다. VRAM이 부족한 경우 먼저
`STT_DIARIZATION_DEVICE=cpu`를 사용합니다.

`STT_DEBUG_ARTIFACTS=true`는 단계별 JSON에 민감한 전사문을 저장할 수
있으므로 기본적으로 꺼져 있습니다. 토큰은 이미지, 로그, 결과 메타데이터에
저장하지 않습니다.

## 전사 백엔드

웹의 작업 생성 화면에서 다음 백엔드를 요청별로 선택합니다.

- `kotoba`: 기본값. Kotoba Whisper와 Pyannote를 사용합니다.
- `whisperx`: WhisperX VAD, alignment, diarization을 사용합니다.
- `hybrid`: 두 모델의 전체 결과를 비교해 구조적으로 실패한 WhisperX
  구간을 Kotoba 결과로 교체합니다.

WhisperX와 하이브리드는 자체 VAD를 사용하므로 소음 필터를 끈 요청을
거부합니다. 단독 WhisperX 요청은 상주 중인 Kotoba 모델을 해제한 뒤
격리된 작업 프로세스를 실행합니다. 하이브리드는 Kotoba와 WhisperX가
동시에 GPU 메모리를 사용할 수 있으므로 더 많은 VRAM이 필요합니다.

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

최종 결과는 원본 영상 옆의 `<이름>.ko.srt`와 `<이름>.ko.ass`입니다.
실제 다른 화자의 동시 발화는 유지하고 같은 화자의 겹친 행은 새 발화로
교체합니다. 번역 세그먼트 ID는 렌더링까지 변경하지 않습니다. 기존 자막은
사용자가 강제 덮어쓰기를 선택한 경우에만 교체합니다.

마운트 용도는 다음과 같습니다.

- `${MEDIA_PATH}:/media:rw`: 영상 조회 및 원본 옆 자막 저장
- `${WEB_STATE_PATH}:/var/lib/stt:rw`: 작업 DB, WAV, 전사·번역 JSON
- `${STT_STATE_PATH}:/var/lib/stt:rw`: STT 작업 DB와 결과
- `${MODEL_CACHE_PATH}:/var/cache/stt:rw`: 모델 캐시

기존 배포의 `jobs.sqlite3`와 `jobs/` 디렉터리를 새
`WEB_STATE_PATH`로 옮기거나 그 기존 경로를 직접 지정하면 작업 기록과
산출물을 계속 사용할 수 있습니다. 데이터베이스 스키마와 파일명은 변경하지
않습니다.

## macOS MPS 전사 API

Apple Silicon에서는 전사 API를 macOS 호스트 프로세스로 실행할 수 있습니다.
이 경로는 CUDA Compose 배포와 별개이며 Metal/MPS를 사용하는 경우에만
필요합니다.

```bash
brew install python@3.11 ffmpeg libsndfile portaudio
cp .env.macos.example .env.macos
# .env.macos의 HF_TOKEN을 설정
./scripts/run-macos-stt.sh
```

독립 실행 폴더가 필요하면 다음 도구를 사용합니다.

```bash
python3 scripts/create_macos_runtime.py /path/to/runtime
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

자주 사용하는 명령은 다음과 같습니다.

```bash
./scripts/compose.sh --env-file .env.compose logs --tail=200 web stt
./scripts/compose.sh --env-file .env.compose restart web stt
./scripts/compose.sh --env-file .env.compose down
```

`down`은 바인드 마운트된 상태·캐시·미디어를 삭제하지 않습니다.

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
