#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# mw-backend  ·  Start server (+ optional Cloudflare Tunnel)
# Usage:
#   ./start.sh            — server only (local testing)
#   ./start.sh --tunnel   — server + Cloudflare Tunnel
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

# Resolve venv bin path (bin on Unix/macOS, Scripts on Windows)
if [ -d "venv/Scripts" ]; then
  VENV_BIN=venv/Scripts
else
  VENV_BIN=venv/bin
fi

# Start the Flask server in background
echo ""
echo "→ Starting mw-backend on port $PORT..."
./$VENV_BIN/python server.py &
SERVER_PID=$!
echo "  PID: $SERVER_PID"

# Optional Cloudflare Tunnel
if [ "$1" = "--tunnel" ]; then
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
