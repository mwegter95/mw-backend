#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# mw-backend  ·  Start server (+ optional Cloudflare Tunnel)
# Usage:
#   ./start.sh              — server only (local testing)
#   ./start.sh --debug      — server with verbose DEBUG logging
#   ./start.sh --tunnel     — server + Cloudflare Tunnel
#   ./start.sh --debug --tunnel
#
# Windows (production / persistent):
#   Use run-server.ps1 instead — it runs in a standalone window,
#   prevents sleep/hibernate, blocks shutdown until stopped,
#   and auto-restarts crashed processes.
# ─────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

# Load .env if present
[ -f .env ] && export $(grep -v '^#' .env | xargs)

PORT=${PORT:-5050}

# Parse flags
DEBUG_FLAG=""
TUNNEL=0
for arg in "$@"; do
  case "$arg" in
    --debug)  DEBUG_FLAG="--debug" ;;
    --tunnel) TUNNEL=1 ;;
  esac
done

# Resolve venv bin path (bin on Unix/macOS, Scripts on Windows)
if [ -d "venv/Scripts" ]; then
  VENV_BIN=venv/Scripts
else
  VENV_BIN=venv/bin
fi

# Start the Flask server in background
echo ""
if [ -n "$DEBUG_FLAG" ]; then
  echo "→ Starting mw-backend on port $PORT  [DEBUG mode]..."
else
  echo "→ Starting mw-backend on port $PORT..."
fi
./$VENV_BIN/python server.py $DEBUG_FLAG &
SERVER_PID=$!
echo "  PID: $SERVER_PID"

# Optional Cloudflare Tunnel
if [ "$TUNNEL" = "1" ]; then
  echo ""
  echo "→ Starting Cloudflare Tunnel (named: mw-backend → api.michaelwegter.com)..."
  cloudflared tunnel run mw-backend &
  TUNNEL_PID=$!
  echo "  Tunnel PID: $TUNNEL_PID"
fi

echo ""
echo "✓ mw-backend running at http://localhost:$PORT"
echo "  Health check: curl http://localhost:$PORT/health"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Wait and clean up on exit
trap "kill $SERVER_PID $TUNNEL_PID 2>/dev/null; echo 'Stopped.'" INT TERM
wait $SERVER_PID
