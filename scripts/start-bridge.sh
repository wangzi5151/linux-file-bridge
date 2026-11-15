#!/bin/sh
# Start one lfb bridge in the background and record its PID.
# Usage: scripts/start-bridge.sh [ENV] [PORT]
set -eu
ENV="${1:-ubuntu-main}"
PORT="${2:-}"
BASE="${LFB_STATE_DIR:-$HOME/.lfb}"
mkdir -p "$BASE/run" "$BASE/logs"
PIDFILE="$BASE/run/$ENV.pid"
LOG="$BASE/logs/$ENV.log"
if [ -f "$PIDFILE" ]; then
  PID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "bridge $ENV is already running (pid $PID)"
    exit 0
  fi
fi
if [ -n "$PORT" ]; then
  nohup lfb serve --env "$ENV" --port "$PORT" >>"$LOG" 2>&1 &
else
  nohup lfb serve --env "$ENV" >>"$LOG" 2>&1 &
fi
PID=$!
echo "$PID" >"$PIDFILE"
echo "started bridge $ENV (pid $PID), log: $LOG"
