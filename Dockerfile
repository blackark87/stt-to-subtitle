# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

FROM ${PYTHON_IMAGE} AS kotoba-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venvs/kotoba

WORKDIR /build
COPY requirements.txt requirements-cuda.txt ./
RUN /opt/venvs/kotoba/bin/python -m pip install -r requirements-cuda.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venvs/kotoba/bin/python -m pip install --no-deps .

FROM ${PYTHON_IMAGE} AS whisperx-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venvs/whisperx

WORKDIR /build
COPY requirements-whisperx-cuda.txt ./
RUN /opt/venvs/whisperx/bin/python -m pip install \
        -r requirements-whisperx-cuda.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venvs/whisperx/bin/python -m pip install --no-deps .

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venvs/kotoba/bin:$PATH \
    HF_HOME=/var/cache/stt/huggingface \
    PYANNOTE_CACHE=/var/cache/stt/pyannote \
    TORCH_HOME=/var/cache/stt/torch \
    WHISPERX_CACHE_DIR=/var/cache/stt/whisperx \
    WHISPERX_PYTHON=/opt/venvs/whisperx/bin/python \
    STT_STATE_DIR=/var/lib/stt \
    STT_DEVICE=cuda \
    STT_DIARIZATION_DEVICE=cuda

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        libgomp1 \
        libportaudio2 \
        ca-certificates \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home app \
    && mkdir -p /var/cache/stt /var/lib/stt \
    && chown -R app:app /var/cache/stt /var/lib/stt

COPY --from=kotoba-builder /opt/venvs/kotoba /opt/venvs/kotoba
COPY --from=whisperx-builder /opt/venvs/whisperx /opt/venvs/whisperx

USER app
WORKDIR /app
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/readyz', timeout=3)"]

ENTRYPOINT ["stt-api"]
