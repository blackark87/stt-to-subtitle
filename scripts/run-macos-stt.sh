#!/bin/sh
set -eu

: "${HF_TOKEN:?HF_TOKEN must be set}"

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin=${MACOS_STT_PYTHON:-"$project_dir/.venv-macos/bin/python"}

if [ ! -x "$python_bin" ]; then
    echo "macOS STT Python was not found: $python_bin" >&2
    echo "Create .venv-macos and install requirements-kotoba.txt and requirements-api.txt first." >&2
    exit 1
fi

export PYTORCH_ENABLE_MPS_FALLBACK=${PYTORCH_ENABLE_MPS_FALLBACK:-1}
export HF_HOME=${HF_HOME:-"$project_dir/var/macos-cache/huggingface"}
export PYANNOTE_CACHE=${PYANNOTE_CACHE:-"$project_dir/var/macos-cache/pyannote"}
export TORCH_HOME=${TORCH_HOME:-"$project_dir/var/macos-cache/torch"}
export STT_STATE_DIR=${STT_STATE_DIR:-"$project_dir/var/macos-stt"}

cd "$project_dir"
exec /usr/bin/caffeinate -ims "$python_bin" -m stt_to_subtitle.macos_api
