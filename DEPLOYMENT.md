# mw-backend — Deployment Guide

Everything runs in two places:
- **Old PC** → runs `mw-backend` (the API server), exposed to the internet via Cloudflare Tunnel
- **GitHub Pages** → hosts the Gallery Wall frontend (free static hosting, auto-deploys on push)

---

## Step 1 — Set up mw-backend on your old PC

### First-time setup
```bash
cd ~/Projects/mw-backend
./setup.sh
```
This creates a Python virtual environment, installs dependencies, and creates a `.env` file.

### Edit `.env`
```env
PORT=5050
FRONTEND_URL=https://yourusername.github.io/your-repo-name   # your GitHub Pages URL (or custom domain if you set one up)
PORTFOLIO_URL=https://michaelwegter.com
```

### Start the server (local test)
```bash
./start.sh
# Visit http://localhost:5050/health — should return {"status":"ok"}
```

---

## Step 2 — Expose to the internet with Cloudflare Tunnel (free)

Cloudflare Tunnel lets you expose your local server to the internet **without port forwarding**, and gives you a real HTTPS URL. It works even with NAT or CGNAT.

### 2a. Create a free Cloudflare account
Go to [cloudflare.com](https://cloudflare.com) and sign up. Add `michaelwegter.com` to your account (or use whatever domain you own).

### 2b. Install cloudflared
```bash
# macOS
brew install cloudflare/cloudflare/cloudflared

# Linux
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# Windows (winget — run in PowerShell)
winget install --id Cloudflare.cloudflared
# Or download the MSI from https://github.com/cloudflare/cloudflared/releases/latest
```

### 2c. Log in and create a tunnel
```bash
cloudflared tunnel login          # opens browser, authorize with Cloudflare
cloudflared tunnel create mw-backend
```
This creates a tunnel and saves credentials to `~/.cloudflared/`.

### 2d. Configure the tunnel
Create the config file at the path for your OS:

- **Windows:** `C:\Users\<you>\.cloudflared\config.yml`
- **Linux/macOS:** `~/.cloudflared/config.yml`

```yaml
tunnel: mw-backend
credentials-file: C:\Users\mike\.cloudflared\072056c5-8973-4c18-a4df-cb9ce268b77d.json

ingress:
  - hostname: api.michaelwegter.com
    service: http://localhost:5050
  - service: http_status:404
```

Replace the credentials-file path with your actual username and tunnel ID (printed by `cloudflared tunnel create`, or found in the `.json` filename in `~/.cloudflared/`).

### 2e. Route the subdomain
```bash
cloudflared tunnel route dns mw-backend api.michaelwegter.com
```
This adds a CNAME record pointing `api.michaelwegter.com` → your tunnel.

> **Note — Squarespace domains:** If your domain is managed through Squarespace (not Cloudflare), the `cloudflared tunnel route dns` command won't work automatically. Instead, add the CNAME manually:
> Squarespace Dashboard → Settings → Domains → click `michaelwegter.com` → DNS Settings → Add Record:
> - Type: `CNAME`
> - Host: `api`
> - Data/Points to: `<tunnel-id>.cfargotunnel.com`
>
> Find `<tunnel-id>` in `~/.cloudflared/<tunnel-id>.json` or by running `cloudflared tunnel list`.

### 2f. Start with tunnel
```bash
cd ~/Projects/mw-backend
./start.sh --tunnel
```
This starts the Flask server **and** the named Cloudflare tunnel (`cloudflared tunnel run mw-backend`). The named tunnel is required — it's what routes `api.michaelwegter.com` to your local server.

Or run the tunnel separately (useful when the server is already running):
```bash
cloudflared tunnel run mw-backend
```

### 2g. Test it
```bash
curl https://api.michaelwegter.com/health
# → {"status": "ok", "version": "1.0.0"}
```

### 2h. Run on startup (optional)

**Windows — one-time setup:**
```powershell
powershell -ExecutionPolicy Bypass -File setup-startup.ps1
```
This registers a Task Scheduler task that opens a visible PowerShell window running `run-server.ps1` every time you log in. It also prevents sleep/hibernate and blocks shutdown until the server is stopped cleanly.

To remove it:
```powershell
powershell -ExecutionPolicy Bypass -File setup-startup.ps1 -Remove
```

**Linux:**
```bash
sudo cloudflared service install
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```
For the Flask server, use a systemd service or add `./start.sh --tunnel` to a startup script.

---

## Step 3 — Deploy Gallery Wall to GitHub Pages

GitHub Pages deploys automatically every time you push to `main`, using the GitHub Actions workflow already in `.github/workflows/deploy.yml`.

### 3a. Push to GitHub
The gallery wall project needs to be in a GitHub repo. If it isn't already:
```bash
cd ~/Projects/wall-gallery-project
git init
git add .
git commit -m "Initial commit"
gh repo create gallery-wall-planner --public --push
```

### 3b. Enable GitHub Pages
In your GitHub repo → **Settings → Pages**:
- Source: **GitHub Actions**

That's all. No other settings needed here.

### 3c. Set Actions Variables
In your GitHub repo → **Settings → Secrets and variables → Actions → Variables tab** → **New repository variable**:

| Name | Value |
|------|-------|
| `VITE_BASE` | `/gallery-wall-planner/` (use your actual repo name, with leading and trailing slashes) |
| `VITE_API_URL` | `https://api.michaelwegter.com` |

`VITE_BASE` tells Vite to prefix all asset paths with your repo's subdirectory so they load correctly on GitHub Pages. If you ever set up a custom domain, change this to `/`.

### 3d. Trigger a deploy
Push any change (or re-run the workflow manually via **Actions → Deploy → Run workflow**). After 1–2 minutes, your app is live at:
```
https://yourusername.github.io/gallery-wall-planner/
```

### 3e. Update .env on mw-backend
Edit `~/Projects/mw-backend/.env` to allow CORS from your Pages URL:
```env
FRONTEND_URL=https://yourusername.github.io/gallery-wall-planner
```
Then restart the server.

### 3f. Update apps.js in the portfolio
Open `~/Projects/michaelwegter.com/src/data/apps.js` and set the `href` for the Gallery Wall entry to your GitHub Pages URL:
```js
href: 'https://yourusername.github.io/gallery-wall-planner/',
```
The portfolio's `/apps/gallery-wall` route loads this URL inside a full-screen iframe, so visitors on `michaelwegter.com` never see the GitHub Pages URL.

---

## Step 4 — (Optional) Custom domain for the gallery wall

This is entirely optional. The iframe in your portfolio already hides the GitHub Pages URL from visitors. A custom domain only matters if you want a clean URL for direct/shared links (e.g. `gallery.michaelwegter.com`).

If you want it:

1. In Squarespace → Settings → Domains → click `michaelwegter.com` → DNS Settings → Add Record:
   - Type: `CNAME`
   - Host: `gallery`
   - Data/Points to: `yourusername.github.io`

2. In your GitHub repo → Settings → Pages → Custom domain → type `gallery.michaelwegter.com` → Save

3. Update the `VITE_BASE` Actions Variable to `/` (no subdirectory needed with a custom domain)

4. Update `FRONTEND_URL` in `mw-backend/.env` to `https://gallery.michaelwegter.com`

5. Update `href` in `apps.js` to `https://gallery.michaelwegter.com`

---

## Day-to-day operation

### On your dev machine (working on gallery wall)
```bash
# Terminal 1 — mw-backend (make sure it's running)
cd ~/Projects/mw-backend && ./start.sh

# Terminal 2 — gallery wall dev server
cd ~/Projects/wall-gallery-project && npm run dev
```
The dev server proxies `/api/*`, `/auth/*`, `/uploads/*` to `localhost:5050` automatically.

### On your old PC (production)
Run `run-server.ps1` for persistent production use:
```powershell
powershell -ExecutionPolicy Bypass -File run-server.ps1
```
Or right-click `run-server.ps1` → **Run with PowerShell**. This opens a standalone window with all logs visible, prevents sleep/hibernate, and auto-restarts if the server or tunnel crashes.

If you ran `setup-startup.ps1`, this window opens automatically on every login — no manual action needed.

`./start.sh --tunnel` still works for quick sessions from Git Bash (local dev / one-off testing).

---

## Data backup
All data lives in `~/Projects/mw-backend/data/`:
- `mw.db` — SQLite database (walls, layouts, library, users)
- `uploads/` — encrypted image files (AES-256-GCM)
- `.secret_key` — JWT signing key **and** upload decryption key (keep private!)

> **Important:** `data/.secret_key` is required to decrypt uploads. If you lose it, all stored images become unrecoverable. Always back it up alongside the `uploads/` folder.

Back these up regularly:
```bash
cp -r ~/Projects/mw-backend/data ~/Backups/mw-backend-$(date +%Y%m%d)
```

---

## Adding the SEO Analyzer to mw-backend (future)

When you're ready, add SEO-specific routes to `mw-backend/server.py` (reports, settings, etc.) and update the SEO analyzer frontend to use `VITE_API_URL` + auth headers. The auth system is already in place — no changes needed there.
