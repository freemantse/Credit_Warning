#!/bin/bash
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Starting Credit Warning System..."
echo ""

# Start FastAPI backend
cd "$ROOT"
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
cd "$ROOT"
npm run dev &
FRONTEND_PID=$!
echo "  Frontend → http://localhost:3000"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "echo ''; echo 'Shutting down...'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null" EXIT INT TERM
wait
