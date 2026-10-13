#!/bin/bash
# launchd owns the process; exec preserves its PID and signal handling.
# Only start named, already-restored containers. Never create a volume or import projects here.
set -euo pipefail
docker=$1
context=$2
python=$3
repo=$4
port=$5
shift 5
if [ "$#" -eq 0 ]; then
    echo "No restored Redis containers configured" >&2
    exit 1
fi
ready=false
for ((attempt=0; attempt<90; attempt++)); do
    if "$docker" --context "$context" info >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 2
done
if [ "$ready" != true ]; then
    echo "Docker unavailable; launchd will retry. Check the Colima service." >&2
    exit 1
fi
"$docker" --context "$context" start "$@"
for container in "$@"; do
    ready=false
    for ((attempt=0; attempt<60; attempt++)); do
        if [ "$("$docker" --context "$context" exec "$container" redis-cli ping 2>/dev/null)" = PONG ]; then
            ready=true
            break
        fi
        sleep 1
    done
    if [ "$ready" != true ]; then
        echo "Redis unavailable in $container; launchd will retry." >&2
        exit 1
    fi
done
exec "$python" "$repo/mcp_server.py" --transport streamable-http --port "$port"
