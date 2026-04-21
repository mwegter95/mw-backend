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

# 1. Python check — try python3, python, then py (Windows launcher)
#    Use -c to verify the command actually runs (avoids Windows Store stubs)
if command -v python3 &>/dev/null && python3 -c "import sys" 2>/dev/null; then
  PYTHON=python3
elif command -v python &>/dev/null && python -c "import sys" 2>/dev/null; then
  PYTHON=python
elif command -v py &>/dev/null && py -c "import sys" 2>/dev/null; then
  PYTHON=py
else
  echo "✗ Python 3 not found. Install from https://python.org"
  exit 1
fi
echo "✓ Python $($PYTHON --version)"

# 2. Virtual environment
if [ ! -d "venv" ]; then
  echo "→ Creating virtual environment..."
  $PYTHON -m venv venv
fi
echo "✓ Virtual environment ready"

# Resolve venv bin path (bin on Unix/macOS, Scripts on Windows)
if [ -d "venv/Scripts" ]; then
  VENV_BIN=venv/Scripts
else
  VENV_BIN=venv/bin
fi

# 3. Install dependencies
echo "→ Installing dependencies..."
./$VENV_BIN/pip install --quiet --upgrade pip
./$VENV_BIN/pip install --quiet -r requirements.txt
echo "✓ Dependencies installed"

# 3b. Install Playwright browser (needed by SEO Analyzer)
echo "→ Installing Playwright Chromium browser (for SEO Analyzer)..."
./$VENV_BIN/playwright install chromium --with-deps 2>/dev/null || \
  ./$VENV_BIN/python -m playwright install chromium 2>/dev/null || \
  echo "  ⚠  Playwright install failed — re-run manually: playwright install chromium"
echo "✓ Playwright ready"

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
  elif [[ "$OSTYPE" == "msys"* || "$OSTYPE" == "cygwin"* || "$OS" == "Windows_NT" ]]; then
    echo "   # Windows — run in PowerShell:"
    echo "   Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile \"\$env:LOCALAPPDATA\\Microsoft\\WindowsApps\\cloudflared.exe\""
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
