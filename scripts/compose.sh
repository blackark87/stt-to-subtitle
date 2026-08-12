#!/bin/sh
set -eu

runtime_uid=$(id -u)
runtime_gid=$(id -g)

if [ "$runtime_uid" -eq 0 ]; then
    echo "Run Compose as the non-root account that owns the mounted files." >&2
    exit 1
fi

export PUID="$runtime_uid"
export PGID="$runtime_gid"

exec docker compose "$@"
