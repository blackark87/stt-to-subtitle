#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(dirname -- "$script_dir")
monitoring_network=${GPU_MONITORING_NETWORK:-gpu-monitoring}

if ! docker network inspect "$monitoring_network" >/dev/null 2>&1; then
    echo "GPU monitoring network '$monitoring_network' does not exist." >&2
    echo "Start gpu-observability first, then retry this command." >&2
    exit 1
fi

network_members=$(docker network inspect \
    --format '{{range .Containers}}{{println .Name}}{{end}}' \
    "$monitoring_network")
case "$network_members" in
    *prometheus*) ;;
    *)
        echo "No Prometheus container is attached to '$monitoring_network'." >&2
        echo "Start or recreate gpu-observability first, then retry." >&2
        exit 1
        ;;
esac

exec "$script_dir/compose.sh" \
    -f "$project_dir/compose.yaml" \
    -f "$project_dir/compose.gpu-monitoring.yaml" \
    "$@"
