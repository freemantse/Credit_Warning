#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "Starting Credit Warning System..."
echo ""

# Free stale ports from a previous run that didn't shut down cleanly.
# (A force-killed script never runs its trap, so its uvicorn/npm children
#  get orphaned and keep holding these ports — reap them before binding.)
free_port() {
  local pids
  pids="$(lsof -ti "tcp:$1" 2>/dev/null || true)"
  if [ -n "$pids" ]; then
    echo "  Freeing stale process on port $1 (pid: $pids)"
    kill $pids 2>/dev/null || true
    sleep 1
    # still there? force it
    pids="$(lsof -ti "tcp:$1" 2>/dev/null || true)"
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
  fi
}
free_port 8000
free_port 3000

# Cleanup runs on a clean exit / Ctrl+C / SIGTERM. Note: a SIGKILL or a
# forcibly-closed terminal cannot run this — which is why we also free the
# ports at startup above.
cleanup() {
  echo ""
  echo "Shutting down..."
  # uvicorn --reload spawns a child worker that actually holds the port,
  # so kill the children of the reloader too — not just the reloader.
  [ -n "$BACKEND_PID" ] && pkill -P "$BACKEND_PID" 2>/dev/null
  kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
  # Belt and suspenders: clear anything still bound to the ports.
  lsof -ti tcp:8000 2>/dev/null | xargs kill 2>/dev/null || true
  lsof -ti tcp:3000 2>/dev/null | xargs kill 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Start FastAPI backend
python3 -m uvicorn api.main:app --reload --port 8000 &
BACKEND_PID=$!
echo "  Backend → http://localhost:8000"

# Fail loud if the backend died on startup (missing dep, import error, port in use)
sleep 2
if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
  echo "ERROR: backend failed to start (see output above)." >&2
  exit 1
fi

# Start Next.js frontend
npm run dev &
FRONTEND_PID=$!
echo "  Frontend → http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop both servers."

wait
