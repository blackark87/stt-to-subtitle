#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file=${CUDA_STT_ENV:-"$project_dir/.env.cuda"}
python_bin=${CUDA_STT_PYTHON:-"$project_dir/.venv-cuda/bin/python"}

if [ ! -f "$env_file" ]; then
    echo "CUDA STT environment file was not found: $env_file" >&2
    echo "Copy .env.cuda.example to .env.cuda and set HF_TOKEN." >&2
    exit 1
fi
if [ ! -x "$python_bin" ]; then
    echo "CUDA STT Python was not found: $python_bin" >&2
    echo "Create .venv-cuda and install requirements-cuda.txt first." >&2
    exit 1
fi

set -a
. "$env_file"
set +a

: "${HF_TOKEN:?HF_TOKEN must be set}"

export HF_HOME=${HF_HOME:-"$project_dir/var/cuda-cache/huggingface"}
export PYANNOTE_CACHE=${PYANNOTE_CACHE:-"$project_dir/var/cuda-cache/pyannote"}
export TORCH_HOME=${TORCH_HOME:-"$project_dir/var/cuda-cache/torch"}
export STT_STATE_DIR=${STT_STATE_DIR:-"$project_dir/var/cuda-stt"}
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p "$HF_HOME" "$PYANNOTE_CACHE" "$TORCH_HOME" "$STT_STATE_DIR"

cd "$project_dir"
exec "$python_bin" -m stt_to_subtitle.macos_api
