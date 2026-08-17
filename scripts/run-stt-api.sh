#!/bin/sh
set -eu

: "${HF_TOKEN:?HF_TOKEN must be set}"

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin=${STT_API_PYTHON:-"$project_dir/.venv-stt/bin/python"}

if [ ! -x "$python_bin" ]; then
    echo "STT API Python was not found: $python_bin" >&2
    echo "Create .venv-stt and install requirements-kotoba.txt and requirements-api.txt first." >&2
    exit 1
fi

export PYTORCH_ENABLE_MPS_FALLBACK=${PYTORCH_ENABLE_MPS_FALLBACK:-1}
export HF_HOME=${HF_HOME:-"$project_dir/var/model-cache/huggingface"}
export PYANNOTE_CACHE=${PYANNOTE_CACHE:-"$project_dir/var/model-cache/pyannote"}
export TORCH_HOME=${TORCH_HOME:-"$project_dir/var/model-cache/torch"}
export STT_STATE_DIR=${STT_STATE_DIR:-"$project_dir/var/stt"}

cd "$project_dir"
if command -v caffeinate >/dev/null 2>&1; then
    exec caffeinate -ims "$python_bin" -m stt_to_subtitle.stt_api
fi
exec "$python_bin" -m stt_to_subtitle.stt_api
