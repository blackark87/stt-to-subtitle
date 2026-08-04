# STT to Subtitle

Docker 호스트에 마운트된 영상 파일을 선택해 일본어를 전사하고, 별도 PC의 OpenAI 호환
번역 서버에서 한국어로 번역한 뒤 영상 옆에 화자 구분 SRT·ASS를
생성하는 로컬 웹 서비스입니다.

## 구성

```text
브라우저
   │
   ▼
linux/amd64 Docker 호스트
  파일 목록 / FFmpeg / 작업 상태 / SRT·ASS 저장
   │                         │
   │ 16 kHz mono PCM WAV     │ 일본어 세그먼트 JSON
   ▼                         ▼
네이티브 전사 PC             별도 번역 서버 PC
FastAPI + Uvicorn           OpenAI 호환 API
Whisper: MPS 또는 CUDA      번역 모델
Pyannote: CPU 또는 CUDA
```

전사 모델과 번역 모델은 완전히 분리된 서비스입니다. 따라서 작업 A를
번역 서버가 처리하는 동안 전사 PC는 작업 B를 처리할 수 있습니다. 웹 오케스트레이터의
오디오와 전사는 한 파일씩 처리하고, 번역은 웹 설정의 워커 수만큼 서로
다른 파일을 병렬 처리합니다. 번역 워커 기본값은 1입니다.

Xcode 애플리케이션을 만들 필요는 없습니다. Mac, Linux 또는 Windows에서
일반 Python FastAPI/Uvicorn 서버를 네이티브로 실행합니다. Mac 패키지
빌드에는 Apple Command Line Tools가 필요할 수 있지만 GUI 앱 개발은
필요하지 않습니다.

Docker Desktop, Podman Machine, Colima 등 Mac의 Linux VM에서 실행하는 컨테이너는 PyTorch에 Metal/MPS 장치를 전달하지 않습니다. Apple Silicon용 컨테이너 이미지나 Docker 호환 도구가 존재하는 것과 MPS 장치 전달은 별개의 문제입니다. MPS 전사는 반드시 macOS 호스트 프로세스로 실행하십시오.

## 처음 배포하는 순서

세 장비에는 가능하면 DHCP 예약 또는 고정 IP를 할당합니다. 아래 포트는 인터넷이 아닌 신뢰할 수 있는 내부망에서만 접근할 수 있도록 방화벽을 설정합니다.

| 장비 | 역할 | 기본 포트 |
| --- | --- | ---: |
| Mac 또는 NVIDIA GPU PC | MPS 또는 CUDA 전사 API | `8100` |
| OpenAI 호환 번역 서버 PC | 번역 API | 제공자 설정에 따름 |
| linux/amd64 Docker 호스트 | 파일 선택 및 작업 웹 UI | `8080` |

처음 설치할 때는 다음 순서가 가장 단순합니다.

1. 별도 PC에서 번역 모델과 OpenAI 호환 LAN API 서버를 실행합니다.
2. Mac 또는 NVIDIA GPU PC에서 네이티브 MPS/CUDA 전사 API를 실행합니다.
3. Docker 호스트에서 이 저장소의 Compose 파일과 환경 파일을 준비하고 GHCR 이미지를 실행합니다.
4. 웹 오케스트레이터 컨테이너에서 전사 PC와 번역 서버의 API에 접속할 수 있는지 확인합니다.
5. 브라우저로 웹 UI를 열어 짧은 파일로 전체 흐름을 검증합니다.

## 처리 흐름

작업 상태는 다음 순서로 진행됩니다.

```text
queued → extracting → audio_ready
       → transcription_running → transcribed
       → translation_running → translated
       → rendering → completed
```

전사 PC 또는 번역 서버가 응답하지 않거나 원격 단계가 실패하면 작업은
`blocked`가 됩니다. 자동으로 계속 재시도하지 않으며, 원인을 해결한 뒤
웹 UI에서 **수동 재시도**해야 합니다. 저장된 WAV, 전사 JSON, 번역
체크포인트 중 정상인 마지막 결과부터 재개합니다.

최종 결과는 원본 영상과 같은 디렉터리의 `<원본이름>.ko.srt`와
`<원본이름>.ko.ass`입니다. 자막 본문에는 `화자 1` 같은 식별자를 붙이지
않고 번역문만 넣습니다. SRT에는 화자별 색상 태그가 들어가며, 스타일을
지원하지 않는 플레이어에서는 번역문만 표시됩니다. ASS는 한국어 영상
자막에 널리 쓰이는 고딕 계열의 `Noto Sans KR` 글꼴, 화자별 색상,
외곽선과 그림자를 안정적으로 표현하는 권장 스타일 자막입니다. 기존
SRT 또는 ASS가 있으면 기본적으로
작업을 거부하고, UI에서 강제 덮어쓰기를 명시한 경우에만 두 파일을
교체합니다.

### Jellyfin에서 한글이 네모로 보일 때

네모(□)는 UTF-8 인코딩 오류가 아니라 재생에 사용된 Jellyfin 렌더러가
한글 글리프를 가진 글꼴을 찾지 못했다는 의미입니다. 이 애플리케이션은
새 ASS 파일의 기본 서체를 `Noto Sans KR`로 지정하지만, ASS 파일 자체에는
글꼴 파일이 포함되지 않습니다. 따라서 Jellyfin 쪽에도 같은 글꼴을
fallback으로 제공해야 합니다.

1. [Google Fonts의 Noto Sans KR](https://fonts.google.com/noto/specimen/Noto+Sans+KR)
   등에서 TTF, OTF 또는 WOFF2 파일을 받아
   Docker 호스트의 예를 들어
   `/srv/jellyfin/fallback_fonts` 폴더에 둡니다. 폴더 전체
   크기는 Jellyfin 제한인 20 MB 이하여야 하며, 웹 재생 위주라면 WOFF2가
   가장 작습니다.
2. Jellyfin을 컨테이너로 실행한다면 기존 Jellyfin Compose의
   `volumes`에 다음 두 마운트를 추가합니다. 첫 번째는 FFmpeg 자막
   burn-in용 시스템 글꼴이고 두 번째는 Web ASS 렌더러용 fallback입니다.

   ```yaml
   services:
     jellyfin:
       volumes:
         - type: bind
           source: /srv/jellyfin/fallback_fonts
           target: /usr/local/share/fonts/custom
           read_only: true
         - type: bind
           source: /srv/jellyfin/fallback_fonts
           target: /fallback_fonts
           read_only: true
   ```

3. Jellyfin 관리자 설정에서 **Fallback font folder path**를
   `/fallback_fonts`로 지정하고 Jellyfin 컨테이너를 재생성합니다.
4. Jellyfin 라이브러리를 새로 스캔하고 재생 세션도 다시 시작합니다.

위 구성은 Jellyfin의
[Fonts 설정](https://jellyfin.org/docs/general/administration/configuration/#fonts)과
[공식 컨테이너 글꼴 마운트 방식](https://jellyfin.org/docs/general/installation/container/)
을 따릅니다.

스타일이 필요 없으면 Jellyfin에서 `.ko.srt` 트랙을 선택해 비교할 수
있습니다. SRT도 네모라면 생성 파일 문제가 아니라 해당 Jellyfin
클라이언트 또는 burn-in 서버의 한글 글꼴 문제입니다. ASS만 네모라면
위 fallback 경로 또는 ASS 웹 렌더러 쪽 설정을 먼저 확인합니다.

## 1-A. MacBook: MPS 전사 API 설치 및 실행

### 준비

Hugging Face에서 다음 gated 모델의 이용 조건을 승인합니다.

- [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
- [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)

터미널에서 저장소를 받고 Python 3.11과 네이티브 의존성을 설치합니다. Homebrew가 없다면 먼저 [brew.sh](https://brew.sh/)의 설치 안내를 따릅니다. 저장소는 소스와 업데이트 용도로만 사용하고, 실제 가상환경·환경 변수·모델 캐시는 Git 작업 트리 밖의 별도 실행 폴더에 둡니다.

```bash
git clone https://github.com/blackark87/stt-to-subtitle.git
cd stt-to-subtitle

brew install python@3.11 ffmpeg libsndfile portaudio
/opt/homebrew/bin/python3.11 \
  scripts/create_macos_runtime.py \
  ../stt-to-subtitle-python
```

생성된 실행 폴더는 저장소와 독립적이며 원하는 위치에 둘 수 있습니다. 그 폴더로 이동해 가상환경과 의존성을 설치합니다.

```bash
cd ../stt-to-subtitle-python
./setup.sh
```

`setup.sh`는 실행 폴더 안에 `.venv-macos`, `.env`, `var/`를 생성합니다. `run.sh`는 복사된 `src/`의 코드를 직접 로드하므로 editable install은 필요하지 않고, 실행 폴더에서 Python 코드를 수정했다면 서버만 재시작하면 반영됩니다. `.env`에서 다음 값을 확인합니다.

```dotenv
HF_TOKEN=hf_replace_me
STT_API_TOKEN=
STT_HOST=0.0.0.0
STT_PORT=8100
STT_DEVICE=mps
STT_DIARIZATION_DEVICE=cpu
STT_CHUNK_PROGRESS_EVERY=10
STT_NOISE_FILTER_TRIGGER_LEVEL=7.0
```

`HF_TOKEN`은 gated 모델 다운로드에 필수입니다. Hugging Face 설정에서 read 권한 토큰을 발급해 `hf_replace_me`를 교체하십시오. 신뢰하는 내부망에서 서비스 간 인증이 필요 없다면 `STT_API_TOKEN`은 비워 둡니다. 값을 설정하면 작업 API에 Bearer 인증이 자동으로 활성화되며 웹 오케스트레이터에도 같은 값을 설정해야 합니다.

`STT_CHUNK_PROGRESS_EVERY`는 `10` 또는 `100`만 사용할 수 있으며 기본값은 `10`입니다. 전사 API는 Kotoba 파이프라인의 실제 전처리·추론 경계를 기준으로 청크가 생성되거나 완료될 때마다 현재 상태를 갱신합니다. 웹 작업 상세에는 `완료 / 생성 / 진행·대기` 카운터가 하나의 진행 행으로 계속 표시되며 같은 내용의 이벤트 행을 반복해서 만들지 않습니다. 상태 API의 `chunk_progress`에는 같은 값과 `report_every`가 포함됩니다. 여기서 청크는 화자 분리 후 겹침 구간을 포함한 모델 입력 단위이므로 SRT 세그먼트 수나 단순한 `영상 길이 ÷ 청크 길이`와 일치하지 않을 수 있습니다. 긴 영상에서 원격 전사 API 로그를 줄이려면 `100`으로 변경한 뒤 전사 API를 재시작하십시오.

사용자가 Kotoba 내부 처리를 조정할 필요는 없습니다. 전사 API는 먼저
Pyannote로 화자와 실제 발화 구간을 찾고, 각 화자 구간을 Kotoba Whisper로
전사한 뒤 로컬 후처리기가 Whisper의 상대 시작·종료 시각을 영상 기준
절대 시각으로 변환합니다. 고정된 Kotoba 원격 코드가 화자 구간 끝을 각
문장의 종료로 잘못 사용하던 동작도 이 단계에서 교체합니다. 여러 모델
청크로 나뉜 같은 화자 구간은 stride를 반영해 한 번만 이어 붙입니다.

웹 UI의 **청크(초)**는 자막 표시 시간이 아니라 Whisper 추론 입력 창의
크기이며 프로젝트 기본값은 `60`초입니다. 고정된 Kotoba 원격 코드가
30초보다 긴 청크를 Whisper 특징 추출기의 기본 길이로 잘라 버리지 않도록,
전사 API가 긴 입력에서는 truncation을 끄고 attention mask를 전달합니다.
따라서 60초 입력 전체가 Whisper 장문 전사 경로로 전달됩니다. 이 값은 SRT
한 줄의 표시 시간을 의미하지 않으며, 더 늘리면 메모리 사용량과 한 번의
추론 지연이 커질 수 있습니다.

**소음 오인식 필터 사용**은 기본으로 켜져 있습니다. Pyannote가 찾은 각
발화 후보를 Whisper에 전달하기 전에 Torchaudio 음성 감지기로 한 번 더
검사해 음성이 없는 것으로 판단된 구간을 제외합니다. 제거된 개수와
시각 범위는 Mac 로그와 전사 결과의 `noise_filter`에 기록되고, 웹 작업
로그에도 제거 개수가 표시됩니다. 조용한 실제 발화가 빠지는 경우에는
작업 체크를 끄거나 Mac 실행 폴더의
`STT_NOISE_FILTER_TRIGGER_LEVEL=7.0`을 낮춘 뒤 서버를 재시작하십시오.
높은 값일수록 필터가 더 엄격하며 값은 양수여야 합니다.

실행 폴더를 다른 위치나 다른 Mac으로 복사할 수 있습니다. 단, Python 가상환경에는 생성 당시의 절대 경로가 포함될 수 있으므로 폴더를 옮긴 뒤에는 대상 위치에서 `./setup.sh`를 다시 실행하십시오. `.env`에는 토큰이 있으므로 복사와 백업 시 노출되지 않도록 주의합니다.

### 실행

```bash
cd /path/to/stt-to-subtitle-python
./run.sh
```

스크립트는 `caffeinate`와 함께 Uvicorn 단일 worker를 실행합니다. 이 터미널을 닫거나 `Ctrl-C`를 누르면 전사 API도 종료됩니다. Whisper는 `mps`, Pyannote는 `cpu`로 로드되고 모델 인스턴스는 최초 작업 때 한 번만 생성됩니다. MPS를 사용할 수 없으면 `/readyz`가 503을 반환하며 CPU로 조용히 전환하지 않습니다.

MacBook 자체에서 상태를 확인합니다.

```bash
curl http://127.0.0.1:8100/healthz
curl http://127.0.0.1:8100/readyz
```

MacBook의 내부 IP는 Wi-Fi가 `en0`인 일반적인 구성에서 다음 명령으로 확인할 수 있습니다. 값이 나오지 않으면 시스템 설정의 네트워크 화면에서 현재 IP를 확인합니다.

```bash
ipconfig getifaddr en0
```

같은 LAN의 Docker 호스트나 다른 장비에서도 다음 요청이 성공해야 합니다.

```bash
curl http://MACBOOK_IP:8100/readyz
```

macOS 방화벽이 Python 또는 포트 `8100`의 수신 연결 허용 여부를 묻는다면 신뢰하는 내부망에서 허용합니다. 절전 상태에서는 처리를 받을 수 없으므로 작업 중에는 MacBook이 전원에 연결되어 있고 네트워크 접속이 유지되어야 합니다.

모델 캐시는 실행 폴더의 `var/macos-cache/`에 유지되므로 `config.yaml`, `pytorch_model.bin` 등을 매번 다시 받지 않습니다. 작업 DB와 전사 결과는 `var/macos-stt/`에 저장됩니다. 두 경로 모두 Git 저장소 밖에 있으므로 실행 중 생성되는 파일이 원본 저장소의 `git status`에 나타나지 않습니다.

주요 API:

- `GET /healthz`
- `GET /readyz`
- `POST /v1/transcriptions` — multipart WAV, `options` JSON, `Idempotency-Key`.
  `options.backend`은 `kotoba`(기본값) 또는 `whisperx`를 받습니다.
- `GET /v1/transcriptions/{id}` — `chunk_progress.created/completed/in_progress` 포함
- `GET /v1/transcriptions/{id}/result`

`STT_API_TOKEN`이 비어 있으면 작업 API는 인증 없이 동작합니다. 값이 있으면 `Authorization: Bearer <STT_API_TOKEN>`이 필요합니다.

### MacBook 업데이트

실행 중인 서버를 `Ctrl-C`로 중지합니다. 원본 Git 저장소에서 최신 코드를 받은 뒤 `--update`로 실행 폴더의 애플리케이션 파일만 갱신합니다. `.env`, `.venv-macos`, `var/`는 유지됩니다.

```bash
cd /path/to/stt-to-subtitle
git pull --ff-only
/opt/homebrew/bin/python3.11 \
  scripts/create_macos_runtime.py \
  --update \
  /path/to/stt-to-subtitle-python

cd /path/to/stt-to-subtitle-python
./setup.sh
./run.sh
```

`--update`는 내보내기 도구가 만든 `.stt-macos-runtime` 표식이 있는 폴더에서만 동작하므로 다른 디렉터리를 실수로 덮어쓰지 않습니다.

## 1-B. Linux/Windows: CUDA 전사 API 설치 및 실행

CUDA 버전은 MPS 버전과 같은 API 및 전사 정규화 코드를 사용하며 실행
장치만 다릅니다. NVIDIA 드라이버, Git, 64비트 Python 3.11이 필요합니다.
`requirements-cuda.txt`는 고정된 PyTorch 2.4.1 CUDA 12.1 휠을 사용하므로
`nvidia-smi`가 정상 동작하고 CUDA 12.1 런타임과 호환되는 드라이버가
설치되어 있어야 합니다. 별도의 CUDA Toolkit 설치는 필수가 아닙니다.

먼저 Hugging Face에서 MPS 설치 절과 같은 Pyannote 모델 이용 조건을
승인합니다. Linux에서는 다음과 같이 설치합니다.

```bash
git clone https://github.com/blackark87/stt-to-subtitle.git
cd stt-to-subtitle
python3.11 -m venv .venv-cuda
.venv-cuda/bin/python -m pip install --upgrade pip
.venv-cuda/bin/python -m pip install -r requirements-cuda.txt
cp .env.cuda.example .env.cuda
```

Windows PowerShell에서는 다음 명령을 사용합니다.

```powershell
git clone https://github.com/blackark87/stt-to-subtitle.git
Set-Location stt-to-subtitle
py -3.11 -m venv .venv-cuda
.\.venv-cuda\Scripts\python.exe -m pip install --upgrade pip
.\.venv-cuda\Scripts\python.exe -m pip install -r requirements-cuda.txt
Copy-Item .env.cuda.example .env.cuda
```

`.env.cuda`의 `HF_TOKEN`을 실제 read 토큰으로 바꿉니다. 기본값은 Whisper와
Pyannote를 모두 첫 번째 GPU에서 실행합니다.

```dotenv
STT_DEVICE=cuda
STT_DIARIZATION_DEVICE=cuda
```

GPU를 명시하려면 `cuda:0`, `cuda:1` 형식을 사용합니다. VRAM이 부족하면
`STT_DIARIZATION_DEVICE=cpu`로 바꿔 Whisper만 CUDA에서 실행할 수 있습니다.
지원 장치는 `cpu`, `mps`, `cuda`, `cuda:<index>`이며, 요청한 가속기를
사용할 수 없을 때 CPU로 자동 전환하지 않습니다.

설치된 CUDA PyTorch를 확인합니다.

```bash
.venv-cuda/bin/python -c \
  "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Windows에서는 실행 파일 경로만 다음과 같이 바꿉니다.

```powershell
.\.venv-cuda\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Linux에서 서버를 실행합니다.

```bash
./scripts/run-cuda-stt.sh
```

Windows PowerShell에서 서버를 실행합니다. 로컬 정책이 서명되지 않은
스크립트를 차단하는 경우 현재 프로세스에만 `Bypass`를 적용할 수 있습니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-cuda-stt.ps1
```

두 플랫폼 모두 다음 상태 API로 확인합니다. CUDA를 찾지 못하거나 지정한
GPU 인덱스가 범위를 벗어나면 `/readyz`는 이유와 함께 503을 반환합니다.

```bash
curl http://127.0.0.1:8100/readyz
```

### 요청별 WhisperX 백엔드

WhisperX 3.8.6은 Kotoba와 다른 PyTorch/Pyannote 버전을 요구하므로 같은
가상환경에 설치하지 않습니다. CUDA STT API는 기존 `.venv-cuda`에서 실행하고,
WhisperX 요청만 격리된 `.venv-whisperx` 작업 프로세스로 처리합니다. Linux에서
다음 환경을 추가합니다. CUDA 12.8 휠과 호환되는 NVIDIA 드라이버 및 FFmpeg가
호스트에 준비되어 있어야 합니다.

HTTP 전사 API와 포트 `8100`은 하나만 실행합니다. 두 가상환경은 의존성을
분리하기 위한 것이며 별도의 WhisperX HTTP 서버를 실행하지 않습니다.

```bash
python3.11 -m venv .venv-whisperx
.venv-whisperx/bin/python -m pip install --upgrade pip
.venv-whisperx/bin/python -m pip install -r requirements-whisperx-cuda.txt
```

Windows에서는 실행 파일 경로를 바꿉니다.

```powershell
py -3.11 -m venv .venv-whisperx
.\.venv-whisperx\Scripts\python.exe -m pip install --upgrade pip
.\.venv-whisperx\Scripts\python.exe -m pip install -r requirements-whisperx-cuda.txt
```

Windows용 `.env.cuda`에서는 다음 경로를 사용합니다.

```dotenv
WHISPERX_PYTHON=.venv-whisperx/Scripts/python.exe
```

서버를 다시 실행하면 `/readyz`의 `backends.whisperx`에서 작업 환경의 준비
상태를 확인할 수 있습니다. 서버 전체 준비 상태는 Kotoba가 정상이라면
WhisperX가 아직 설치되지 않아도 `ready`를 유지합니다.

요청마다 `options.backend`로 백엔드를 선택합니다. 값이 없으면 기존 Kotoba를
사용합니다.

```bash
curl -X POST http://127.0.0.1:8100/v1/transcriptions \
  -H 'Idempotency-Key: example-whisperx-001' \
  -F 'audio=@audio.16k.wav;type=audio/wav' \
  -F 'options={"backend":"whisperx","chunk_length_seconds":30,"noise_filter":true}'
```

WhisperX는 자체 VAD를 항상 사용하므로 `backend=whisperx`에서
`noise_filter=false`는 거부됩니다. 요청이 Kotoba에서 WhisperX로 전환되면
상주 중인 Kotoba 모델을 먼저 해제하고 CUDA 캐시를 비웁니다. WhisperX 작업은
완료 후 프로세스가 종료되어 VRAM을 반환하며, 다음 Kotoba 요청에서 Kotoba를
다시 지연 로드합니다.

웹 오케스트레이터의 전사 API 주소에는 Mac 대신 이 CUDA PC의 내부 IP와 포트 `8100`을
설정하면 됩니다. `.env.cuda`, `var/cuda-cache/`, `var/cuda-stt/`,
`.venv-cuda/`는 Git에서 제외됩니다.

## 2. OpenAI 호환 번역 서버

LM Studio처럼 OpenAI 호환 API를 제공하는 서버를 전사 API와 독립적으로
실행합니다. 서버는 최소한 `GET /v1/models`와
`POST /v1/chat/completions`를 지원해야 합니다.

1. 일본어→한국어 번역에 사용할 모델을 로드합니다.
2. LAN에서 접근 가능한 API 서버를 활성화합니다.
3. 웹 오케스트레이터 컨테이너에서 접근할 수 있도록 호스트 방화벽의 번역 API 포트를 내부망으로 제한합니다.

웹 설정에는 OpenAI 호환 API 루트(예: `http://192.168.1.30:1234/v1`)와
선택적 API 토큰을 설정합니다. 웹 설정 화면이 `/models`를 조회하므로
모델 식별자를 직접 입력할 필요 없이 목록에서 선택할 수 있습니다. 번역
요청은 `/chat/completions`의 JSON Schema structured output을 사용하며
세그먼트 ID가 정확히 보존되지 않으면 실패 처리합니다.

번역은 기본 30개 세그먼트 또는 6,000자 단위로 나뉩니다. 각 배치 후
`<원본명>_result_ko.json` 체크포인트를 저장하므로 중간 실패 후 완료한
배치는 다시 요청하지 않습니다. 재시도할 때 현재 전사에 없는 오래된
체크포인트 ID는 자동으로 제외합니다. 서버가 올바른 ID를 다른 순서로
반환하면 요청 순서로 정렬하고, 누락되거나 다른 ID를 반환한 배치는 더
작은 배치로 나눠 다시 요청합니다.

## 3. Docker 웹 오케스트레이터 설치 및 실행

Docker Engine과 Compose 플러그인을 사용할 수 있는 `linux/amd64` 호스트를 준비합니다. GHCR 이미지만 pull하여 실행하므로 Git 저장소·Python 소스·Dockerfile·빌드 도구는 실행 호스트에 필요하지 않습니다.

이 저장소를 받은 PC에서 배포에 필요한 두 파일만 Docker 호스트로 복사합니다.

```bash
ssh DOCKER_USER@DOCKER_HOST_IP \
  'mkdir -p /srv/stt-to-subtitle'
scp compose.yaml .env.nas.example \
  DOCKER_USER@DOCKER_HOST_IP:/srv/stt-to-subtitle/
```

그다음 미디어 폴더에 읽기/쓰기 가능한 계정으로 접속해 환경 파일을 준비합니다.

```bash
ssh DOCKER_USER@DOCKER_HOST_IP
cd /srv/stt-to-subtitle
mv .env.nas.example .env.nas
chmod 600 .env.nas
```

미디어 공유 폴더에 접근하는 계정의 숫자 UID/GID를 확인합니다.

```bash
id DOCKER_USER
```

`.env.nas`에서 최소한 다음 값을 실제 환경에 맞게 교체합니다.

- `PUID`, `PGID` — 미디어 폴더에 읽기/쓰기 가능한 호스트 사용자
- `MEDIA_PATH` — Docker 호스트의 실제 미디어 폴더 절대 경로
- `STATE_PATH` — 작업 DB와 중간 결과를 보관할 호스트 경로
- `NAS_IMAGE` — `ghcr.io/blackark87/stt-to-subtitle:latest`

전사·번역 서버 주소, 토큰과 번역 모델은 컨테이너를 실행한 뒤 웹의
**서버 설정** 화면에서 입력합니다. `.env.nas`의 `STT_*`와
`OPENAI_COMPATIBLE_*` 값은 최초 실행 시 사용할 선택적 기본값이므로
비워 둘 수 있습니다. 기존 `LM_STUDIO_*` 환경변수도 호환을 위해 계속
인식합니다.

예:

```dotenv
PUID=1026
PGID=100
MEDIA_PATH=/srv/media
STATE_PATH=./var/nas-state
NAS_WEB_PORT=8080
NAS_IMAGE=ghcr.io/blackark87/stt-to-subtitle:latest

NAS_ADMIN_PASSWORD=
NAS_SESSION_SECRET=
NAS_SECURE_COOKIE=false

STT_BASE_URL=
STT_API_TOKEN=

OPENAI_COMPATIBLE_BASE_URL=
OPENAI_COMPATIBLE_TOKEN=
OPENAI_COMPATIBLE_MODEL=
```

Compose는 GHCR 이미지의 기본 사용자와 관계없이 `PUID:PGID`를 컨테이너의 런타임 사용자로 적용합니다. 상태 폴더를 미리 만들어 해당 사용자가 쓸 수 있게 합니다. 배포 폴더를 `DOCKER_USER`로 만들었다면 일반적으로 추가 `chown`은 필요하지 않습니다.

```bash
mkdir -p ./var/nas-state
test -w ./var/nas-state && echo "state directory is writable"
test -w /srv/media && echo "media directory is writable"
```

내부망에서 무인증으로 사용할 때는 `NAS_ADMIN_PASSWORD`,
`NAS_SESSION_SECRET`, `STT_API_TOKEN`, `OPENAI_COMPATIBLE_TOKEN`을
모두 비워 둡니다. `NAS_ADMIN_PASSWORD`를 설정하면 웹 로그인이
활성화되며, 이 경우 `NAS_SESSION_SECRET`도 32자 이상으로 설정해야
합니다. 세션 키 생성 예:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

GHCR 패키지가 private이면 다음과 같이 `read:packages` 권한이 있는
GitHub classic PAT로 Docker 호스트에 한 번 로그인합니다. 패키지를
public으로 설정했다면 이 단계는 필요하지 않습니다. 이 로그인은 이미지
다운로드용이며, 위에서 비워 둔 웹·STT·번역 서버의 내부 서비스 인증과는
별개입니다.

```bash
echo "$GHCR_PAT" | docker login ghcr.io -u GITHUB_USER --password-stdin
```

### 웹에서 전사·번역 서버 설정

컨테이너를 실행한 뒤 `http://DOCKER_HOST_IP:8080/settings` 또는 상단의
**서버 설정**을 엽니다. 다음 값을 입력하고 저장합니다.

- 전사 API 주소와 선택적 API 토큰
- OpenAI 호환 API 주소와 선택적 API 토큰
- `/models` 조회 결과에서 사용할 번역 모델
- 동시에 서로 다른 파일을 번역할 병렬 워커 수(1~8, 기본 1)

설정은 `${STATE_PATH}`의 SQLite DB에 저장되므로 컨테이너를 다시 만들거나
재시작해도 유지됩니다. 수정한 값은 저장 직후 다음 전사·번역 요청부터
적용되며 Docker 재시작은 필요하지 않습니다. 이미 실행 중인 원격 요청은
기존 설정으로 마친 뒤 다음 요청부터 새 설정을 사용합니다. 토큰 입력을
비우면 기존 토큰을 유지하며, 저장된 토큰을 제거하려면 화면의 토큰 삭제
체크박스를 사용합니다.

`NAS_ADMIN_PASSWORD`를 사용하지 않는 구성에서는 서버 설정 화면도 인증
없이 열리므로 반드시 신뢰할 수 있는 LAN이나 VPN 안에서만 노출하십시오.

### Docker 호스트에서 연결 확인

Docker 호스트에서 두 원격 API에 접속할 수 있는지 먼저 확인합니다.

```bash
curl http://192.168.1.20:8100/readyz
curl http://192.168.1.30:1234/v1/models
```

첫 번째 주소는 웹에서 저장한 전사 API 주소, 두 번째 주소는 OpenAI 호환
번역 API 주소에 맞춰 변경합니다. 웹 설정 화면에서도 같은 `/models`
응답을 자동 조회하여 선택 목록으로 표시합니다.

Compose 구성을 검증하고 GHCR 이미지를 받아 실행합니다.

```bash
docker compose --env-file .env.nas config
docker compose --env-file .env.nas pull
docker compose --env-file .env.nas up -d
docker compose --env-file .env.nas ps
docker compose --env-file .env.nas logs -f orchestrator
```

로그 확인은 `Ctrl-C`로 빠져나와도 컨테이너를 중지하지 않습니다. 상태 API가 정상인지 Docker 호스트에서 확인합니다.

```bash
curl http://127.0.0.1:8080/healthz
```

브라우저에서 `http://DOCKER_HOST_IP:8080`을 엽니다. 관리자 비밀번호를 비워 두었다면 로그인 화면 없이 바로 대시보드가 열립니다. `MEDIA_ROOT`의 실제 디렉터리는 폴더 카드로 표시되고, 폴더를 열어 계층적으로 영상을 탐색합니다. 상위 화면에서 하위 폴더를 재귀 스캔하지 않으며, 폴더를 클릭하면 로딩 dim 화면을 먼저 표시한 뒤 일반 페이지 요청으로 들어간 폴더의 직속 항목만 조회합니다. `@eaDir`, `#recycle`, `.snapshot`, `.DS_Store`, `Thumbs.db` 같은 저장소 메타데이터 항목 및 `*-trailer.mp4` 파일은 탐색에서 제외됩니다.

영상은 폴더와 구분되는 미디어 카드로 표시됩니다. 카드에는 디렉터리 경로를 표시하지 않고 파일 크기를 GiB 단위로 표시합니다. 각 카드의 재생시간은 현재 폴더의 표시 대상만 FFprobe로 확인하며, 파일 크기와 수정 시각이 바뀌지 않은 동안 메모리에 캐시됩니다. 재생시간을 읽을 수 없는 파일은 `재생시간 알 수 없음`으로 표시됩니다. 영상 옆에 같은 이름의 `.nfo` 또는 `movie.nfo`가 있으면 `<title>`과 로컬 포스터를 함께 표시합니다. NFO가 가리키는 `<thumb aspect="poster">`/`<poster>` 이미지가 없으면 `영상이름-poster`, `영상이름`, `poster`, `folder`, `cover` 순으로 `.jpg`, `.jpeg`, `.png`, `.webp` 파일을 찾습니다. NFO가 없는 영상은 비디오 아이콘, 파일명, 파일 크기, 재생시간과 자막 상태를 표시합니다. 상태는 `대기`, `생성 중`, `생성 완료`, `확인 필요`로 구분되며 진행·완료·확인 필요 카드는 연결된 작업 상세로 이동합니다.

`대기` 카드의 체크박스로 한 개 이상의 영상이나 폴더를 선택한 뒤 필요한 경우 오디오 스트림, 시작 지점, 처리 길이, 화자 수를 지정합니다. `전사`는 오디오 추출과 전사까지만 실행하고, `번역`은 검증된 기존 전사 JSON을 재사용해 번역과 자막 생성을 실행하며, `전체`는 오디오 추출부터 자막 생성까지 전 과정을 실행합니다. 번역할 전사 JSON이 없거나 유효하지 않으면 `번역` 작업은 등록되지 않으므로 먼저 `전사`를 실행해야 합니다. 이전 버전의 `오디오 추출 완료` 작업에 전사를 요청하면 기존 job ID와 WAV를 승계하며, WAV가 사라진 경우에도 같은 job ID로 오디오부터 다시 추출합니다. 남아 있는 오디오 추출 완료 기록은 원본과 WAV를 유지한 채 UI에서 삭제할 수 있습니다. 여러 영상을 선택하면 같은 설정으로 작업이 일괄 등록되고 화면은 현재 폴더에 그대로 남습니다. 기존 `.ko.srt` 또는 `.ko.ass`가 있는 영상이 자막 생성 작업에 포함되어 있고 **강제 덮어쓰기**를 선택하지 않았다면 전체 배치가 거부됩니다. 처리 길이를 비우거나 `0`으로 지정하면 선택한 시작 지점부터 파일 끝까지 처리합니다.

진행·확인 필요 작업은 개수 제한 없이 표시하고 완료 작업은 최신순으로 20개씩 페이지를 나눕니다. 작업 상세의 전사·번역 청크는 반복 로그 대신 각각 하나의 진행 바로 갱신됩니다. 번역 중단은 현재 논리 청크와 체크포인트 저장을 마친 뒤 적용되며, 이어 하기는 저장된 미완료 구간부터 계속합니다. 병렬 번역 워커는 서로 다른 파일을 동시에 처리하고 한 파일 안의 청크 순서는 유지합니다.

완료된 작업의 **상세** 화면에는 HTML5 결과 미리보기가 표시됩니다. `일반`은 원본 영상을 그대로 재생하고, `180° 미리보기`는 YouTube의 데스크톱 180° 플레이어처럼 SBS의 좌안 영상을 WebGL 반구에 투영합니다. 미리보기 시점은 마우스·터치 드래그로 움직이고 휠로 시야각을 조절합니다. 한 화면을 좌우로 나누는 가짜 양안 모드는 제공하지 않습니다.

실제 VR 헤드셋이 연결된 WebXR 브라우저에서는 **VR 헤드셋으로 보기**가 활성화됩니다. 이 버튼은 `immersive-vr` 세션을 열어 헤드셋이 제공하는 각 `XRView`의 투영 행렬과 헤드 자세를 사용하고, 좌안에는 SBS 왼쪽 절반, 우안에는 오른쪽 절반을 각각 렌더링합니다. 자막은 DOM 오버레이가 아니라 WebGL 텍스처로 만들어 양쪽 눈에 함께 합성합니다. 헤드셋 컨트롤러의 기본 선택 동작은 재생과 일시정지를 전환합니다.

WebXR immersive 모드는 보안 컨텍스트가 필요하므로 `http://DOCKER_HOST_IP:8080`으로 접속하면 헤드셋 버튼이 비활성화됩니다. HTTPS reverse proxy를 통해 컨테이너의 HTTP 포트로 전달하고, 헤드셋이 신뢰하는 인증서가 적용된 `https://호스트명`으로 접속해야 합니다. HTTPS여도 브라우저나 연결된 장치가 `immersive-vr`을 지원하지 않으면 데스크톱 180° 미리보기만 사용할 수 있습니다. 구현은 브라우저 기본 WebGL/WebXR만 사용하므로 Node.js, 외부 CDN, 영상 재인코딩은 필요하지 않습니다.

플레이어 또는 VR 캔버스에 포커스가 있으면 `Space`는 재생/일시정지, `←`/`→`는 10초 뒤/앞 이동, `↑`/`↓`는 음량 조절로 동작합니다. 별도 음량 슬라이더와 음소거 버튼도 같은 HTML5 비디오의 오디오 상태를 제어합니다. WebGL을 사용할 수 없거나 렌더링 연결이 끊기면 일반 플레이어로 돌아가며 자동으로 VR 모드를 재시도하지 않습니다.

플레이어가 MIME 형식을 지원하는지 먼저 검사하며, 지원하지 않으면 영상 URL을 연결하지 않아 스트리밍을 시작하지 않습니다. MIME은 지원하지만 실제 코덱 해석에 실패하면 소스를 제거하고 자동 재시도하지 않습니다. 원본 영상은 긴 파일에서도 탐색할 수 있도록 HTTP Range로 스트리밍하고, 전사·번역 JSON에서 화자별 색상 WebVTT를 동적으로 생성해 한국어 자막 트랙으로 제공합니다. VR 모드에서도 같은 cue를 캔버스 위에 화자별 색상으로 표시합니다. 실제 동시 발화는 시작·종료 경계로 타임라인을 나눈 뒤 한 cue의 여러 색상 행으로 합성하므로 오래된 cue가 플레이어에 남지 않습니다. 같은 화자의 겹친 세그먼트는 새 발화가 시작될 때 교체합니다. MP4/H.264처럼 브라우저가 지원하는 컨테이너와 코덱은 바로 재생할 수 있지만, MKV·HEVC 등 브라우저가 지원하지 않는 조합은 WebGL VR 모드에서도 재생되지 않을 수 있습니다.

마운트는 다음 용도로 분리됩니다.

- `${MEDIA_PATH}:/media:rw` — 입력 파일 조회 및 원본 옆 SRT·ASS 저장
- `${STATE_PATH}:/var/lib/stt:rw` — SQLite WAL DB, 추출 WAV, 전사 JSON, 번역 JSON

작업 JSON은 `<원본명>_translate.json`(일본어 전사)과 `<원본명>_result_ko.json`(한국어 결과)으로 저장됩니다. 같은 이름의 영상이 여러 폴더에 있거나 같은 영상을 다시 처리해도 서버에서는 `${STATE_PATH}/jobs/<작업 ID>/`별로 격리되므로 서로 덮어쓰지 않습니다.

작업 상세 화면에서 두 JSON을 다운로드하는 대신 브라우저 편집기로 열 수 있습니다. 저장할 때 JSON 문법, 스키마, 세그먼트 ID와 순서를 검증하고 원자적으로 교체합니다. 전사와 번역 JSON이 모두 유효하면 `.ko.srt`와 `.ko.ass`를 즉시 다시 생성합니다. 이전 버전에서 생성된 세그먼트의 표시 시간이 비정상적으로 길면 같은 화자의 다음 발화 시작과 읽기 가능한 최대 표시 시간을 기준으로 방어적으로 줄이고 작업 로그에 보정 개수를 남깁니다. 다른 화자의 발화 시작은 실제 중첩일 수 있으므로 보정 경계로 사용하지 않습니다. 정확한 원래 종료 시각이 필요한 기존 결과는 새 Mac 후처리기로 다시 전사해야 합니다. 완료·중단·실패 상태의 작업만 편집할 수 있으며, 한 번에 편집할 수 있는 JSON은 20 MiB로 제한됩니다. 설정한 토큰과 자격 증명은 결과 메타데이터나 진행 로그에 기록하지 않습니다.

완료된 작업의 **번역부터 다시 시작** 버튼은 일본어 전사 JSON과 추출
오디오를 유지한 채 기존 번역 체크포인트만 빈 상태로 초기화합니다. 이후
번역 서버 호출과 SRT·ASS 렌더링만 다시 실행하므로 시간이 오래 걸리는
전사를 반복하지 않습니다. 새 번역이 완료될 때 기존 SRT·ASS를 같은
경로에 교체하며, 번역 서버가 꺼져 있으면 번역 단계에서 `blocked`가 되어
서비스를 실행한 뒤 수동 재시도로 이어갈 수 있습니다.

작업 생성·갱신 및 진행 로그의 실제 시각은 웹 UI와 전사 API에서 모두 KST(`+09:00`)로 표시하거나 직렬화합니다. SQLite 내부에는 시간대와 무관한 epoch 값을 유지합니다. SRT 및 전사 세그먼트 타임스탬프는 영상 시작점 기준 상대시간이므로 KST 변환 대상이 아닙니다.

무인증 모드는 세 장비가 격리된 신뢰 가능한 LAN에 있을 때만 사용하십시오.
웹 UI, 전사 API, 번역 API 포트를 인터넷이나 게스트 Wi-Fi에 직접
노출하지 말고 LAN 방화벽 또는 신뢰할 수 있는 VPN으로 제한하십시오.
HTTPS reverse proxy와 웹 로그인을 사용하는 경우
`NAS_SECURE_COOKIE=true`로 설정합니다.

### Docker 업데이트 및 운영 명령

`main`에 새 이미지가 게시된 뒤에는 애플리케이션 소스를 받을 필요 없이 이미지만 갱신합니다.

```bash
docker compose --env-file .env.nas pull
docker compose --env-file .env.nas up -d
```

Compose 설정 자체가 변경된 경우에만 PC에서 새 `compose.yaml`을 Docker 호스트의 배포 폴더로 다시 복사합니다. `.env.nas`와 `${STATE_PATH}`는 그대로 유지합니다.

자주 사용하는 운영 명령:

```bash
# 현재 상태
docker compose --env-file .env.nas ps

# 최근 로그
docker compose --env-file .env.nas logs --tail=200 orchestrator

# 재시작
docker compose --env-file .env.nas restart orchestrator

# 중지
docker compose --env-file .env.nas down
```

`down`을 실행해도 `${STATE_PATH}`와 미디어 파일은 삭제되지 않습니다. 작업 DB와 중간 결과를 초기화하려고 상태 폴더를 직접 삭제할 때는 컨테이너를 먼저 중지하고 대상 경로를 다시 확인하십시오.

## GHCR 이미지 게시 및 Docker에서 받기

[`.github/workflows/publish-ghcr.yaml`](.github/workflows/publish-ghcr.yaml)은 `Dockerfile`과 애플리케이션 소스를 GitHub Actions에서 빌드해, 테스트를 통과한 `linux/amd64` 이미지를 `ghcr.io/blackark87/stt-to-subtitle`에 게시합니다. 이 빌드 파일들은 CI에만 필요하며 Docker 호스트로 복사하지 않습니다. 다음 경우 실행됩니다.

- `main` 브랜치 push
- `v*` 태그 push
- GitHub Actions의 수동 실행

`main`의 기본 태그는 `latest`이며 브랜치, 전체 commit SHA 태그와
`pyproject.toml`의 버전에서 만든 `X.Y.Z`, `X.Y` 태그도 함께 생성됩니다.
모든 새 PR은 SemVer 규칙에 따라 프로젝트 버전을 한 번 갱신해야 합니다.
호환성을 깨는 변경은 `MAJOR`, 하위 호환 기능은 `MINOR`, 하위 호환
수정·문서·CI·운영 변경은 `PATCH`를 올립니다. 별도 registry secret은
필요하지 않고 워크플로의 `GITHUB_TOKEN`과 `packages: write` 권한을
사용합니다. 패키지가 private이면 Docker 호스트에서 `read:packages` 권한이 있는
PAT로 먼저 로그인해야 합니다.

```bash
echo "$GHCR_PAT" | docker login ghcr.io -u GITHUB_USER --password-stdin
```

`.env.nas`에는 기본 이미지가 이미 설정되어 있습니다. 다른 버전 태그를 고정할 때만 값을 변경합니다.

```dotenv
NAS_IMAGE=ghcr.io/blackark87/stt-to-subtitle:latest
```

이미지를 가져와 실행합니다. `compose.yaml`에는 `build:` 항목이 없으므로 Docker 호스트에서 로컬 빌드가 실행될 수 없습니다.

```bash
docker compose --env-file .env.nas pull
docker compose --env-file .env.nas up -d
```

GHCR 이미지 자체의 기본 UID/GID는 `1000:1000`이지만 `compose.yaml`이 `.env.nas`의 `PUID`/`PGID`로 런타임 사용자를 덮어씁니다. 따라서 Docker 호스트에서 UID/GID 때문에 이미지를 다시 빌드할 필요는 없습니다.

## 개발 검증

모델을 다운로드하지 않는 단위 테스트와 정적 검사는 다음과 같습니다.

```bash
make test
make check
docker compose --env-file .env.nas.example config
```

실제 품질 테스트에는 합법적으로 사용할 수 있는 짧은 미디어만 사용하고, 민감한 전사문이나 모델 캐시를 커밋하지 마십시오.
