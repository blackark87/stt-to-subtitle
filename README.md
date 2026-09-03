# STT to Subtitle

일본어 미디어를 전사하고 OpenAI 호환 모델로 한국어 번역을 만든 뒤 SRT·ASS
자막을 게시하는 로컬 웹 애플리케이션입니다. Web/Backend와 GPU 전사 모델 서버를
분리하므로 한 호스트에서 시작해 독립 GPU 노드로 확장할 수 있습니다.

## 구조

```text
Browser ──▶ Web (Next.js) ──▶ Backend (FastAPI + SQLite)
                                  ├─ FFmpeg 오디오 추출
                                  ├─ 번역·자막 렌더링
                                  ├─ Transcriber 0..N (HTTP/SSE)
                                  └─ OpenAI 호환 번역 서버 0..N

Transcriber
  ├─ Recall-Union Hybrid: WhisperX → OWSM 감사 → Kotoba rescue → stable-ts
  └─ WhisperJAV: 고정된 기존 recipe
```

`compose.yaml`은 Web과 Backend만 실행합니다. 전사 모델 서버는
`compose.transcribers.yaml`의 `hybrid` 또는 `whisperjav` 프로필로 독립 실행하며
Backend와 Docker 프로젝트·호스트·클라우드 사업자를 공유할 필요가 없습니다.
Backend는 전사 모델 서버가 하나도 없어도 정상 기동합니다.

Backend는 전사 모델 서버와 번역 서버를 직접 등록·관리합니다. 같은 GPU를 쓰는
endpoint에는 동일한 `resource_group_id`를 지정하십시오. 기본 그룹
`local-gpu`의 capacity는 1이며, 같은 그룹의 전사와 번역 요청이 동시에 GPU를
사용하지 않게 Backend가 통합 예약합니다.

## 전사 방식

신규 작업에서 선택할 수 있는 전사 방식은 두 가지입니다.

- `hybrid`: WhisperX 원문을 항상 보존합니다. OWSM이 누락 가능 구간을 찾으면
  해당 구간만 Kotoba로 다시 전사하고, 검증된 rescue를 원문 옆에 추가합니다.
  의미 기반 자동 중복 제거는 하지 않습니다. 누락 억제를 우선하므로 사용자가
  과다 전사를 삭제하는 검수 흐름에 맞습니다.
- `whisperjav`: 기존 WhisperJAV worker, 옵션, 모델 구성과 정렬 방식을 그대로
  사용합니다. Hybrid의 OWSM/Kotoba 설정은 이 경로로 전달되지 않습니다.

Hybrid 안의 WhisperX, OWSM, Kotoba 작업은 순차 subprocess로 실행됩니다. 한
작업에서 여러 ASR 모델이 동시에 GPU를 사용하지 않습니다. WhisperX word 결과가
있으면 stable-ts regroup를 적용하고, 알려진 모델 timestamp 결함은 번역 전
정규화 경계에서 보정합니다.

전사 비교 생성 UI/API는 제공하지 않습니다. 이전 비교 작업과 산출물은 삭제하지
않고 일반 테스트 작업 이력으로 계속 조회할 수 있습니다.

## 저장소 경계

호스트 경로로 관리하는 데이터는 두 종류뿐입니다.

| 변수 | 컨테이너 경로 | 내용 |
| --- | --- | --- |
| `MEDIA_PATH` | `/media` | 원본 미디어와 게시된 SRT·ASS |
| `TRANSCRIPTION_AUDIO_PATH` | `/var/lib/stt-audio` | 재사용 가능한 16 kHz 전사용 WAV |

작업 DB, 전사·번역 JSON, 내부 SRT·ASS, revision과 generation은 Compose named
volume `backend-data`에 저장됩니다. 각 Transcriber의 작업 상태와 모델 캐시는
서로 다른 named volume에 저장됩니다. 모델 가중치는 이미지에 넣지 않으며 최초
사용 시 해당 Transcriber 컨테이너가 자신의 cache volume에 받습니다.

`docker compose down -v`는 영속 volume을 삭제하므로 사용하지 마십시오.

## 처음 실행

요구 사항은 Docker Engine/Compose, Linux amd64, NVIDIA GPU를 사용할 경우
NVIDIA Container Toolkit, Pyannote 사용 조건을 승인한 Hugging Face read token,
그리고 일본어→한국어 OpenAI 호환 번역 endpoint입니다.

애플리케이션 설정을 준비합니다.

```bash
cp .env.compose.example .env.compose
chmod 600 .env.compose
mkdir -p ./media ./audio
./scripts/compose.sh --env-file .env.compose config
./scripts/compose.sh --env-file .env.compose build web backend
./scripts/compose.sh --env-file .env.compose up -d web backend
```

`.env.compose`에서 `MEDIA_PATH`, `TRANSCRIPTION_AUDIO_PATH`, `TRAEFIK_HOST`를
실제 환경에 맞게 설정하십시오. Traefik을 쓰지 않는 배포에서는 `compose.yaml`의
웹 게시 방식을 환경에 맞게 조정할 수 있습니다. `STT_BASE_URL`은 선택적인 최초
Transcriber bootstrap 주소이며 비워 둬도 됩니다.

GPU 호스트에서 전사 모델 서버를 준비합니다.

```bash
cp .env.transcribers.example .env.transcribers
chmod 600 .env.transcribers
# .env.transcribers의 HF_TOKEN 설정
./scripts/compose.sh -f compose.transcribers.yaml \
  --env-file .env.transcribers --profile hybrid config
./scripts/compose.sh -f compose.transcribers.yaml \
  --env-file .env.transcribers --profile hybrid build hybrid
./scripts/compose.sh -f compose.transcribers.yaml \
  --env-file .env.transcribers --profile hybrid up -d hybrid
```

WhisperJAV가 필요하면 `hybrid` 대신 `whisperjav` 프로필을 사용합니다. 두 서비스는
상호 `depends_on`이 없고 각자 독립적으로 기동됩니다. 한 GPU에서 둘을 동시에
처리하지 않으려면 하나만 실행하거나 Backend에서 같은 resource group에 capacity
1로 등록하십시오.

Web의 **설정 → 전사 모델 서버**에서 `http://gpu-host:8100`과 선택적 API token을
등록하고 준비 상태를 확인합니다. 번역 서버도 1차·2차 그룹별로 등록하고 같은
가속기를 공유하면 동일한 resource group을 지정합니다.

## 기존 bind 배포에서 업그레이드

6.0.0은 Backend 영속 데이터를 named volume으로 옮깁니다. 기존 DB나 산출물을
직접 이동하거나 삭제하지 마십시오. 먼저 새 `TRANSCRIPTION_AUDIO_PATH`를 정합니다.
기존 작업 경로를 그대로 지정하면 대용량 WAV를 복사하지 않고 재사용할 수 있습니다.

```bash
TRANSCRIPTION_AUDIO_PATH=/data/work/stt-to-subtitle/web-jobs
```

작업을 중지하고 Backend를 내린 다음 읽기 전용 원본에서 일회성 이관을 실행합니다.
이관 도구는 SQLite online backup과 `quick_check`, 일반 파일 SHA-256 검증을
수행하며 다른 내용의 대상 파일을 덮어쓰지 않습니다. 실패해도 원본 bind 데이터는
변경하지 않으며 같은 명령을 다시 실행할 수 있습니다.

```bash
./scripts/compose.sh --env-file .env.compose stop backend
./scripts/compose.sh -f compose.yaml -f compose.migrate.yaml \
  --env-file .env.compose --profile migrate run --rm storage-migrate
./scripts/compose.sh --env-file .env.compose up -d backend web
```

기본 legacy 경로가 아닌 경우 이관 명령에 사용되는 `.env.compose`에만
`BACKEND_STATE_PATH`, `BACKEND_WORK_PATH`, `TRANSLATION_STATE_PATH`를 지정합니다.
정상 기동 후에도 legacy 원본은 사용자가 별도로 백업을 확인하고 정리할 때까지
그대로 남겨 두십시오.

## 번역 피드백과 프롬프트 개선

작업 상세에서 번역 문장을 수정하면 새 immutable manual generation이 생성되고,
원문·모델 번역·사용자 수정·인접 문맥이 피드백으로 기록됩니다.

- 1차 번역 결과의 수정은 translation prompt 자료가 됩니다.
- 2차·외부 검토·최종 결과의 수정은 review prompt 자료가 됩니다.
- 각 피드백은 설정 화면에서 개선 표본 포함 여부를 바꿀 수 있습니다.
- 개선안은 사용자가 명시적으로 요청할 때만 생성됩니다.

한 카테고리와 단계에서 현재 prompt revision에 속한 최소 20개 cue와 3개 작업이
필요합니다. 최신 유효 피드백 최대 200개를 작업 단위로 80/20 train/holdout에
나눕니다. 현재 prompt와 후보 prompt의 holdout 점수·회귀 목록을 확인한 뒤
**승인하고 활성화**해야 새 immutable revision이 적용됩니다. 기준 revision이 이미
바뀐 후보는 승인할 수 없습니다. endpoint credential은 피드백, 제안 기록이나
결과 메타데이터에 저장하지 않습니다.

## 처리 흐름

각 단계는 독립 작업이며 자동으로 다음 단계를 시작하지 않습니다.

```text
미디어 → 오디오 추출 → 전사
완료 전사 → 1차 번역
1차 결과 → 2차 검수
2차 결과 → 선택적 외부 검토
선택 결과 → SRT·ASS 게시
```

전사 revision, 번역 generation, 자막 generation은 불변 이력으로 보존됩니다.
실제 다른 화자의 동시 발화는 유지하고 같은 화자의 겹친 행은 새 발화로
교체합니다. 렌더링 과정에서 번역 segment ID를 변경하지 않습니다.

작업 상세의 최종 번역 차수에서는 **타임라인 편집**을 열어 영상 미리보기, 선택
세그먼트 속성, 자막 타임라인을 한 작업공간에서 편집할 수 있습니다. **이 위치에 새
세그먼트 추가**는 현재 영상 재생 위치에 2초 길이 클립을 만들고, 새 클립을 선택한 뒤
화면 가운데로 이동시킵니다. 이후 원문과 한국어 자막을 입력하고 시간을 조정합니다.
클립은 이동·트림하거나 삭제할 수 있으며 원문, 번역, 화자와 시작·종료 시각을 함께
저장합니다. 기존 segment ID는 유지하고 새 세그먼트에만 새 ID를 부여합니다.

대형 작업의 상세 화면은 편집에 필요한 경량 전사·번역 데이터만 읽고 화면에 보이는
자막 행과 타임라인 클립만 렌더링합니다. 완료 작업은 자동 폴링을 중단하며, 사용자가
직접 누르는 새로고침은 계속 지원합니다. 저장할 때 전사 revision, 번역 generation,
자막 generation을 불변 이력으로 만들고, 미디어의 SRT·ASS는 **자막 파일 생성**을
다시 승인할 때까지 교체하지 않습니다.

## Transcriber HTTP 계약

- `GET /healthz`, `GET /readyz`
- `POST /v1/transcriptions`: mono 16 kHz PCM WAV와 JSON options 제출
- `GET /v1/transcriptions/{job_id}`: 상태 조회
- `GET /v1/transcriptions/{job_id}/events`: 진행 SSE
- `GET /v1/transcriptions/{job_id}/result`: 결과 조회
- `POST /v1/transcriptions/{job_id}/cancel`: 취소 요청

요청한 CUDA 장치가 없거나 필요한 격리 Python 환경이 없으면 `/readyz`가 503을
반환합니다. CPU로 자동 전환하지 않습니다.

## GPU 관측

`gpu-observability/`는 DCGM Exporter, Prometheus, Grafana를 제공하는 별도 Compose
프로젝트입니다. 메인 애플리케이션과 Transcriber는 이 프로젝트에 종속되지
않습니다. Backend에는 선택적으로 `GPU_PROMETHEUS_URL`만 지정합니다. 관측 서비스가
없거나 응답하지 않아도 전사·번역 서비스는 계속 동작합니다.

## 호스트 Transcriber

Apple Silicon MPS처럼 Docker에 가속 장치를 전달할 수 없는 환경은 호스트 실행
도구를 사용할 수 있습니다. 기존 `stt-runtime`/`stt-api` 명령은 호환 alias이며
새 공개 명칭은 `stt-transcriber`입니다.

```bash
cp .env.stt.example .env.stt
./scripts/run-stt-runtime.sh
```

## 보안

- `HF_TOKEN`과 API token은 runtime 환경으로만 전달합니다.
- credential을 이미지 build argument, Git, 로그, 산출물 메타데이터에 넣지 않습니다.
- 애플리케이션 자체 사용자 인증은 없으므로 신뢰할 수 있는 네트워크와 HTTPS reverse
  proxy 안에서 운영하십시오.
- Transcriber 포트는 Backend가 접근할 주소에만 노출하십시오.

## 개발 검증

모델 다운로드 없이 실행할 수 있는 검증입니다.

```bash
make test
make check
./scripts/compose.sh --env-file .env.compose.example config
./scripts/compose.sh -f compose.transcribers.yaml \
  --env-file .env.transcribers.example --profile hybrid config
```

실제 품질 테스트에는 합법적으로 사용할 수 있는 짧은 fixture만 사용하고 미디어,
민감한 전사문과 모델 cache는 커밋하지 마십시오.
