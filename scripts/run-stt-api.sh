#!/bin/sh
set -eu

# Legacy operator entrypoint. New deployments use run-stt-runtime.sh.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$script_dir/run-stt-runtime.sh" "$@"
