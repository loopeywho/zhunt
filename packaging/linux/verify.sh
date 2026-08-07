#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PACKAGE=${1:-"$ROOT/.linux-build/dist/zhunt"}
if [ ! -x "$PACKAGE/zhunt" ]; then
    echo "Usage: $0 /path/to/extracted/zhunt" >&2
    exit 1
fi

VERIFY_HOME=$(mktemp -d)
PORT=18765
SERVER_LOG="$VERIFY_HOME/server.log"
cleanup() {
    if [ -n "${SERVER_PID:-}" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$VERIFY_HOME"
}
trap cleanup EXIT INT TERM

HOME="$VERIFY_HOME" "$PACKAGE/zhunt" serve --host 127.0.0.1 --port "$PORT" >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!
READY=0
for _ in $(seq 1 30); do
    STATUS=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' "http://127.0.0.1:$PORT/v1/models" || true)
    if [ "$STATUS" = "401" ]; then
        READY=1
        break
    fi
    sleep 1
done

if [ "$READY" -ne 1 ]; then
    cat "$SERVER_LOG" >&2
    echo "Linux daemon did not return the expected unauthenticated 401." >&2
    exit 1
fi

echo "Linux x86_64 package smoke test passed: daemon started and unauthenticated request returned 401."
