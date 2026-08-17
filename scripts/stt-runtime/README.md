# STT API Runtime

이 폴더는 Git 작업 트리 밖에서 전사 API를 실행하기 위한 독립 실행 환경입니다. 애플리케이션 소스, Python 가상환경, 환경 변수, 모델 캐시와 작업 DB가 모두 이 폴더 안에 유지됩니다. 기본 예시는 Apple Silicon의 MPS를 사용하지만 `STT_DEVICE` 설정에 따라 지원되는 다른 장치에서도 같은 API를 실행할 수 있습니다.

## 최초 설치

Python 3.11과 FFmpeg, libsndfile, PortAudio를 설치한 다음 이 폴더에서 설치 스크립트를 실행합니다. Apple Silicon에서는 다음 Homebrew 명령을 사용할 수 있습니다.

```bash
brew install python@3.11 ffmpeg libsndfile portaudio
./setup.sh
```

생성된 `.env`에서 `HF_TOKEN`을 실제 Hugging Face read 토큰으로 교체합니다. 신뢰할 수 있는 LAN에서 인증 없이 사용할 때는 `STT_API_TOKEN`을 비워 둡니다.

전사 청크 진행 로그는 기본적으로 실제 완료 청크 10개마다 출력됩니다. 긴 영상에서 로그를 줄이려면 `.env`의 `STT_CHUNK_PROGRESS_EVERY=10`을 `100`으로 변경합니다. 사용할 수 있는 값은 `10`과 `100`뿐이며, 변경 후 서버를 재시작해야 합니다.

웹 서비스에서 보내는 전사 청크의 기본 크기는 60초입니다. 소음 때문에 Pyannote가 발화로 잘못 잡은 구간은 Torchaudio 음성 감지기로 한 번 더 검사하며, 웹 작업 화면의 **소음 오인식 필터 사용**이 기본으로 켜져 있습니다. 필터가 너무 조용한 실제 발화를 제거한다면 해당 체크를 끄십시오. 필터 강도는 실행 폴더의 `.env`에 있는 `STT_NOISE_FILTER_TRIGGER_LEVEL=7.0`으로 조정하며, 높은 값일수록 더 엄격합니다. 설정을 바꾼 뒤에는 서버를 재시작해야 합니다.

```bash
./run.sh
```

상태 확인:

```bash
curl http://127.0.0.1:8100/healthz
curl http://127.0.0.1:8100/readyz
```

## 폴더 이동 및 복사

설치 전의 실행 폴더는 원하는 위치로 복사할 수 있습니다. Python 가상환경에는 생성 당시의 절대 경로가 포함될 수 있으므로, 이미 `setup.sh`를 실행한 폴더를 이동하거나 다른 호스트로 복사했다면 `.venv-stt`를 그대로 신뢰하지 말고 대상 위치에서 `setup.sh`를 다시 실행하십시오.

`.env`에는 Hugging Face 토큰이 들어 있으므로 다른 장비로 복사할 때 노출되지 않도록 주의합니다. `var/model-cache`를 함께 복사하면 모델 다운로드를 줄일 수 있지만 용량이 클 수 있습니다.

## 구성

- `.venv-stt/` — Python 3.11 가상환경
- `.env` — 로컬 환경 변수와 토큰
- `var/model-cache/` — Hugging Face, Pyannote, Torch 모델 캐시
- `var/stt/` — 작업 DB와 전사 결과
- `src/stt_to_subtitle/` — 실행 시 직접 로드되는 애플리케이션 소스

`run.sh`는 이 폴더의 `src/`를 `PYTHONPATH`로 직접 사용합니다. 소스를 수정했다면 서버만 재시작하면 변경이 반영되며 editable install은 필요하지 않습니다. 실행 폴더는 Git 저장소가 아니므로 여기서 생성되는 파일은 원본 저장소의 `git status`에 나타나지 않습니다.
