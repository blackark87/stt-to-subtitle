#!/bin/sh
set -eu

runtime_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_candidate=${MACOS_STT_BOOTSTRAP_PYTHON:-/opt/homebrew/bin/python3.11}
python_bin=$(command -v "$python_candidate" 2>/dev/null || true)

if [ -z "$python_bin" ]; then
    echo "Python 3.11 was not found: $python_candidate" >&2
    echo "Install it with 'brew install python@3.11' or set" >&2
    echo "MACOS_STT_BOOTSTRAP_PYTHON to a Python 3.11 executable." >&2
    exit 1
fi

if [ ! -x "$runtime_dir/.venv-macos/bin/python" ]; then
    "$python_bin" -m venv "$runtime_dir/.venv-macos"
fi

venv_python="$runtime_dir/.venv-macos/bin/python"
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install \
    -r "$runtime_dir/requirements-kotoba.txt" \
    -r "$runtime_dir/requirements-api.txt"
"$venv_python" -m pip install --no-deps \
    "git+https://github.com/huggingface/diarizers.git@f3c8ae500f55ad2b02b719fce1495ea2794ca9fe"

mkdir -p \
    "$runtime_dir/var/macos-cache/huggingface" \
    "$runtime_dir/var/macos-cache/pyannote" \
    "$runtime_dir/var/macos-cache/torch" \
    "$runtime_dir/var/macos-stt"

if [ ! -f "$runtime_dir/.env" ]; then
    cp "$runtime_dir/.env.example" "$runtime_dir/.env"
    chmod 600 "$runtime_dir/.env"
    echo "Created $runtime_dir/.env"
fi

echo "macOS STT runtime is installed in $runtime_dir"
echo "Set HF_TOKEN in $runtime_dir/.env, then run ./run.sh"
