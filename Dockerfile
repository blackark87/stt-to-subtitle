# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
ARG STT_RUNTIME_IMAGE=stt-to-subtitle-stt-runtime:py311-cuda-v2

FROM ${PYTHON_IMAGE} AS app-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,id=stt-app-pip,target=/root/.cache/pip \
    python -m pip wheel --no-deps --wheel-dir /wheel .

FROM ${STT_RUNTIME_IMAGE} AS runtime

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
    WHISPERJAV_PYTHON=/opt/venvs/whisperjav/bin/python \
    STT_STATE_DIR=/var/lib/stt \
    STT_DEVICE=cuda \
    STT_DIARIZATION_DEVICE=cuda

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home app \
    && mkdir -p /var/cache/stt /var/lib/stt \
    && chown -R app:app /var/cache/stt /var/lib/stt

COPY --from=app-builder /wheel /tmp/stt-wheel
RUN /opt/venvs/kotoba/bin/python -m pip install --no-cache-dir --no-deps \
        /tmp/stt-wheel/*.whl \
    && /opt/venvs/whisperx/bin/python -m pip install --no-cache-dir --no-deps \
        /tmp/stt-wheel/*.whl \
    && /opt/venvs/whisperjav/bin/python -m pip install --no-cache-dir --no-deps \
        /tmp/stt-wheel/*.whl \
    && rm -rf /tmp/stt-wheel

USER app
WORKDIR /app
EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/readyz', timeout=3)"]

ENTRYPOINT ["stt-api"]
