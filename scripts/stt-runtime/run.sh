#!/bin/sh
set -eu

runtime_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
env_file=${STT_RUNTIME_ENV:-${STT_API_ENV:-"$runtime_dir/.env"}}

if [ ! -f "$env_file" ]; then
    echo "Environment file was not found: $env_file" >&2
    echo "Run ./setup.sh and set HF_TOKEN before starting the server." >&2
    exit 1
fi

set -a
. "$env_file"
set +a

export PYTHONPATH="$runtime_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$runtime_dir/scripts/run-stt-runtime.sh"
