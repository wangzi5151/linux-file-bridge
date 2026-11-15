#!/bin/sh
# Stop a bridge started by start-bridge.sh. Uses the recorded PID only; it never
# searches process command lines, so it cannot hit the calling shell.
# Usage: scripts/stop-bridge.sh [ENV]
set -eu
ENV="${1:-ubuntu-main}"
BASE="${LFB_STATE_DIR:-$HOME/.lfb}"
PIDFILE="$BASE/run/$ENV.pid"
if [ ! -f "$PIDFILE" ]; then
  echo "no pid file for $ENV"
  exit 0
fi
PID="$(cat "$PIDFILE")"
if [ -z "$PID" ]; then
  rm -f "$PIDFILE"
  exit 0
fi
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "stopped bridge $ENV (pid $PID)"
else
  echo "bridge $ENV is not running (stale pid $PID)"
fi
rm -f "$PIDFILE"
