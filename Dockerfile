# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

FROM ${PYTHON_IMAGE} AS app-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,id=stt-app-pip,target=/root/.cache/pip \
    python -m pip wheel --no-deps --wheel-dir /wheel .

FROM ${PYTHON_IMAGE} AS kotoba-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venvs/kotoba

WORKDIR /build
COPY requirements-kotoba.txt requirements-api.txt ./
RUN --mount=type=cache,id=stt-kotoba-pip,target=/root/.cache/pip \
    /opt/venvs/kotoba/bin/python -m pip install \
        --extra-index-url https://download.pytorch.org/whl/cu121 \
        -r requirements-kotoba.txt \
        -r requirements-api.txt \
    && /opt/venvs/kotoba/bin/python -m pip install --no-deps \
        "git+https://github.com/huggingface/diarizers.git@f3c8ae500f55ad2b02b719fce1495ea2794ca9fe"

FROM ${PYTHON_IMAGE} AS whisperx-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venvs/whisperx

WORKDIR /build
COPY requirements-whisperx-cuda.txt ./
RUN --mount=type=cache,id=stt-whisperx-pip,target=/root/.cache/pip \
    /opt/venvs/whisperx/bin/python -m pip install \
        -r requirements-whisperx-cuda.txt

FROM ${PYTHON_IMAGE} AS runtime

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venvs/kotoba/bin:$PATH \
    HF_HOME=/data/stt-to-subtitle/model/huggingface \
    PYANNOTE_CACHE=/data/stt-to-subtitle/model/pyannote \
    TORCH_HOME=/data/stt-to-subtitle/model/torch \
    WHISPERX_CACHE_DIR=/data/stt-to-subtitle/model/whisperx \
    WHISPERX_PYTHON=/opt/venvs/whisperx/bin/python \
    STT_STATE_DIR=/data/stt-to-subtitle/stt-state \
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
    && mkdir -p /data/stt-to-subtitle/model \
        /data/stt-to-subtitle/stt-state \
    && chown -R app:app /data/stt-to-subtitle

COPY --from=kotoba-builder /opt/venvs/kotoba /opt/venvs/kotoba
COPY --from=whisperx-builder /opt/venvs/whisperx /opt/venvs/whisperx
COPY --from=app-builder /wheel /tmp/stt-wheel
RUN /opt/venvs/kotoba/bin/python -m pip install --no-cache-dir --no-deps \
        /tmp/stt-wheel/*.whl \
    && /opt/venvs/whisperx/bin/python -m pip install --no-cache-dir --no-deps \
        /tmp/stt-wheel/*.whl \
    && rm -rf /tmp/stt-wheel

USER app
WORKDIR /app
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/readyz', timeout=3)"]

ENTRYPOINT ["stt-api"]
