#!/bin/sh
set -eu

runtime_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_candidate=${STT_BOOTSTRAP_PYTHON:-python3.11}
python_bin=$(command -v "$python_candidate" 2>/dev/null || true)

if [ -z "$python_bin" ]; then
    echo "Python 3.11 was not found: $python_candidate" >&2
    echo "Install Python 3.11 or set STT_BOOTSTRAP_PYTHON" >&2
    echo "to a Python 3.11 executable." >&2
    exit 1
fi

if [ ! -x "$runtime_dir/.venv-stt/bin/python" ]; then
    "$python_bin" -m venv "$runtime_dir/.venv-stt"
fi

venv_python="$runtime_dir/.venv-stt/bin/python"
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install \
    -r "$runtime_dir/requirements-kotoba.txt" \
    -r "$runtime_dir/requirements-api.txt"
"$venv_python" -m pip install --no-deps \
    "git+https://github.com/huggingface/diarizers.git@f3c8ae500f55ad2b02b719fce1495ea2794ca9fe"

mkdir -p \
    "$runtime_dir/var/model-cache/huggingface" \
    "$runtime_dir/var/model-cache/pyannote" \
    "$runtime_dir/var/model-cache/torch" \
    "$runtime_dir/var/stt"

if [ ! -f "$runtime_dir/.env" ]; then
    cp "$runtime_dir/.env.example" "$runtime_dir/.env"
    chmod 600 "$runtime_dir/.env"
    echo "Created $runtime_dir/.env"
fi

echo "STT API runtime is installed in $runtime_dir"
echo "Set HF_TOKEN in $runtime_dir/.env, then run ./run.sh"
