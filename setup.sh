#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# mw-backend  ·  First-time setup
# Run once on your old PC, then use start.sh to run the server.
# ─────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║   mw-backend  ·  First-time Setup   ║"
echo "╚══════════════════════════════════════╝"
echo ""

# 1. Python check
if ! command -v python3 &>/dev/null; then
  echo "✗ Python 3 not found. Install from https://python.org"
  exit 1
fi
echo "✓ Python $(python3 --version)"

# 2. Virtual environment
if [ ! -d "venv" ]; then
  echo "→ Creating virtual environment..."
  python3 -m venv venv
fi
echo "✓ Virtual environment ready"

# 3. Install dependencies
echo "→ Installing dependencies..."
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt
echo "✓ Dependencies installed"

# 4. .env file
if [ ! -f ".env" ]; then
  echo ""
  echo "→ Creating .env file..."
  cat > .env <<EOF
# ─── mw-backend environment ───────────────────────────────────
PORT=5050

# Allowed frontend URLs (comma-separated, no spaces)
# Update this to your GitHub Pages URL once deployed, e.g.:
# https://yourusername.github.io/gallery-wall-planner
FRONTEND_URL=https://yourusername.github.io/gallery-wall-planner
PORTFOLIO_URL=https://michaelwegter.com

# SECRET_KEY is auto-generated and stored in data/.secret_key
# You can override it here if you want:
# SECRET_KEY=your-secret-here
EOF
  echo "✓ .env created — edit it to add your frontend URLs"
else
  echo "✓ .env already exists (not overwritten)"
fi

# 5. Cloudflare Tunnel install check
echo ""
echo "─────────────────────────────────────────"
if command -v cloudflared &>/dev/null; then
  echo "✓ cloudflared already installed ($(cloudflared --version 2>&1 | head -1))"
else
  echo "⚠  cloudflared not found."
  echo "   Install it to expose this server to the internet:"
  echo ""
  if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "   brew install cloudflare/cloudflare/cloudflared"
  else
    echo "   curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared"
    echo "   chmod +x /usr/local/bin/cloudflared"
  fi
  echo ""
  echo "   Then run:  cloudflared tunnel login"
  echo "              cloudflared tunnel create mw-backend"
  echo "   See DEPLOYMENT.md for full instructions."
fi
echo "─────────────────────────────────────────"

echo ""
echo "✅ Setup complete!  Run ./start.sh to start the server."
echo ""
