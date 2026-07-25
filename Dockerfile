# syntax=docker/dockerfile:1

FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        git \
        libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --prefix=/install -r requirements.txt \
    && python -m pip install --prefix=/install --no-deps .

FROM python:3.11-slim-bookworm AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/cache/huggingface \
    XDG_CACHE_HOME=/cache \
    TORCH_HOME=/cache/torch \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    TOKENIZERS_PARALLELISM=false

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        ffmpeg \
        libgomp1 \
        libportaudio2 \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home app \
    && mkdir -p /cache/huggingface /cache/torch /output \
    && chown -R app:app /cache /output

COPY --from=builder /install /usr/local

USER app
WORKDIR /app

ENTRYPOINT ["stt-transcribe"]
CMD ["--help"]
