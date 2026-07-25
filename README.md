# STT-TO-Subtitles

영상에서 오디오를 추출하고 로컬 오픈 웨이트 모델로 전사·번역하여 Jellyfin용 자막을 만드는 프로젝트입니다. 현재 구현 범위는 M1 Max에서의 1차 검증을 위한 **FFmpeg 오디오 추출 + Kotoba-Whisper v2.2 일본어 전사 + Pyannote 화자 분리**입니다. 번역과 SMI/SRT/VTT 생성은 아직 포함하지 않습니다.

## M1 Max 실행 제약

Docker Desktop의 일반 Linux 컨테이너는 Apple Metal/MPS를 PyTorch에 전달하지 않습니다. 따라서 이 이미지는 `linux/arm64` CPU 추론으로 실행됩니다. 32GB M1 Max에서는 Docker Desktop에 메모리 20~24GB와 CPU 8~10개를 할당하는 설정부터 시험하십시오. 전체 영상을 실행하기 전에 5~15분 구간으로 처리 시간과 메모리를 측정하는 것이 필요합니다.

## 사전 준비

1. Hugging Face에서 다음 모델의 이용 조건을 승인합니다.
   - [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
   - [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
2. 승인된 계정의 읽기 토큰을 환경 변수로 설정합니다.
3. 입력 파일과 출력 디렉터리를 준비합니다.

```bash
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx
mkdir -p media output
cp /path/to/sample.mkv media/
```

토큰은 이미지나 Git 저장소에 넣지 마십시오. 입력 미디어는 Hugging Face로 전송되지 않으며, 네트워크는 최초 모델 다운로드에만 사용됩니다.

## 이미지 빌드

```bash
make image
```

동일한 명령은 다음과 같습니다.

```bash
docker build --platform linux/arm64 -t stt-to-subtitle:kotoba-m1 .
```

의존성은 Kotoba 모델 공개 시점과 호환되는 버전으로 고정되어 있습니다. 모델 가중치는 이미지에 포함하지 않으며, 실행 시 Docker 볼륨에 캐시합니다.

## 전사 실행

먼저 5분 구간을 테스트합니다.

```bash
docker compose run --rm stt \
  /data/sample.mkv \
  --output-dir /output \
  --duration-seconds 300 \
  --num-speakers 2 \
  --threads 8
```

영상 전체를 처리할 때는 `--duration-seconds`를 제거합니다. 화자 수를 모르면 `--num-speakers` 대신 `--min-speakers 1 --max-speakers 4`를 사용할 수 있습니다. 두 번째 오디오 트랙은 `--audio-stream 1`, 특정 구간은 `--start-seconds 1800 --duration-seconds 300`으로 선택합니다.

기본 `--batch-size 1`은 메모리 우선 설정입니다. 안정성을 확인한 뒤 `--batch-size 2`를 시험할 수 있습니다. 첫 실행은 모델 다운로드 때문에 이후 실행보다 오래 걸립니다.

## 출력

`output/`에 다음 파일을 덮어씁니다.

- `sample.16k.wav`: 16kHz, 모노, 16-bit PCM 추출 오디오
- `sample.stt.json`: 모델·실행 정보, 화자별 타임스탬프 세그먼트
- `sample.stt.txt`: 검토용 타임스탬프 전사문

`--add-punctuation`은 별도 구두점 모델을 로드하고 JSON의 `speaker_transcripts`에 적용합니다. 세그먼트 본문에는 적용되지 않습니다.

## 개발 검증

모델을 내려받지 않는 단위 테스트와 문법 검사는 다음과 같이 실행합니다.

```bash
make test
make check
```

실제 품질 평가는 소음, 겹침 발화, 일본어·영어 혼합 구간을 포함한 합법적으로 사용할 수 있는 짧은 샘플로 수행하십시오.
