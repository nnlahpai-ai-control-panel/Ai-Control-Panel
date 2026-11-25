# AI Server Control Panel

A self-contained web panel for an existing Ubuntu Server 24.04 / 26.04. It manages local AI models, Docker apps, multi-GPU telemetry and a browser terminal, all on **port 1234**. It does not change the kernel and needs no custom ISO. It runs as systemd services under `/opt/ai-panel`.

## Files

| File | What it is |
|---|---|
| `install_app.sh` | 1-step installer and upgrader (Docker, NVIDIA toolkit, Ollama, llama.cpp Vulkan, systemd) |
| `app.py` | FastAPI backend (auth, Ollama, llama.cpp, App Store, GPU stats, WebSocket terminal, updates) |
| `apps_catalog.json` | 28 one-click apps |
| `index.html` | Dark-mode single-page dashboard |
| `static/` | Tailwind CSS and xterm.js served locally, so the panel works without internet in the browser |

## Install (3 lines)

On a fresh Ubuntu Server, run:

```bash
curl -fsSL https://raw.githubusercontent.com/nnlahpai-ai-control-panel/ai-control-panel/main/install_app.sh -o install_app.sh
sudo bash install_app.sh
echo "Open http://$(hostname -I | awk '{print $1}'):1234  (login printed above)"
```

The installer downloads the panel files from this GitHub repo. It also sets this repo as the panel's update source, so **Settings > Update panel** and **Refresh app catalog** pull your latest pushes.

Without internet access to GitHub, copy the folder over instead:

```powershell
scp -r "D:\Ai server Control" YOUR_USER@SERVER_IP:~/ai-panel      # run on Windows
```
```bash
cd ~/ai-panel && sudo bash install_app.sh                          # run on the server
```

The installer prints the URL and a random `admin` password. The password is also saved in `/opt/ai-panel/initial_password.txt`. Change it in **Settings** after your first login. If an NVIDIA driver was installed, reboot once.

Installer options: `--port 1234`, `--no-drivers`, `--update-engines`, `--source URL`.
Running the installer again upgrades in place and keeps your login, apps, models and settings.

## Mixed GPUs (NVIDIA + AMD + Intel together)

* **llama.cpp (AI Models > llama.cpp)** is the engine that pools *different vendors* into one model. The installer uses the official **Vulkan** build. Every card shows up as `Vulkan0`, `Vulkan1`, and so on. With **Tensor split = auto**, layers are split by each card's total VRAM: 8GB + 12GB + 16GB becomes `--tensor-split 8,12,16`, so 36GB is pooled. Use `fit` to let llama.cpp decide from free memory, or type your own ratio. **GPU layers = -1** lets llama.cpp fit as many layers as VRAM allows and puts the rest in RAM. That is how 30GB+ models still run.
* **Ollama** spreads a model across all GPUs of the **same** vendor (`OLLAMA_SCHED_SPREAD=1` is set). To try Ollama across vendors, uncomment `OLLAMA_VULKAN=1` in `/etc/systemd/system/ollama.service.d/ai-panel.conf` (experimental). Then run `systemctl daemon-reload && systemctl restart ollama`.
* llama-server exposes an OpenAI-compatible API at `http://127.0.0.1:8081/v1`. Open WebUI is pre-wired to both Ollama and llama.cpp.
* Docker apps that use a GPU (ComfyUI, Immich ML, Jellyfin) get NVIDIA (CUDA) *or* AMD (ROCm) passthrough. You choose which in the install dialog, because container images are vendor-specific.

### How many GPUs can be pooled?

| | |
|---|---|
| **Recommended** | **2 to 6 GPUs** (works best, easy to power and cool) |
| Software limit | about 15 mixed GPUs (llama.cpp Vulkan supports 16 devices, and 1 backend slot goes to the CPU) |
| Real limit | your motherboard slots and PCIe lanes, power supply and cooling |

Tips for a good pool:
* Any count and any sizes work, for example 8+12+16GB, 6+24GB or 4x16GB. The panel detects each server's cards and splits by its real VRAM.
* With the default **layer** split, the cards take turns, so x1 risers are fine. But the **slowest card sets the speed**. A small old card adds VRAM and can make generation slower, so only add it if you need the memory.
* Each card draws 150 to 450W. Size the power supply for all cards at full load plus 20%.
* Above 6 cards, expect more setup trouble (BIOS "Above 4G decoding" and "Resizable BAR", riser stability) and smaller speed gains per card.

## Getting models and apps in

* **Ollama:** 1-click library, or type any name (`qwen3:14b`, `hf.co/user/repo`). You can also turn any local GGUF into an Ollama model with the "→ Ollama" button.
* **llama.cpp GGUF:** from Hugging Face (type the repo and pick a quant), from any direct URL, or by uploading from your computer. Downloads resume if interrupted.
* **Custom Docker apps:** paste a compose file, load a `.yml` from your computer, or fetch one from a URL (GitHub links work). Use `${DATA_DIR}` for persistent folders.

## Keeping everything up to date

| What | How |
|---|---|
| One app | 🔄 button on the app card (pulls the latest image and recreates it, keeping data and passwords) |
| All apps | **App Store > Update all apps** (also refreshes each app's compose from the latest catalog) |
| Automatic nightly | Install **Watchtower** from the store (updates every night at 4 AM) |
| App catalog | **Settings > Refresh app catalog** (downloads the new `apps_catalog.json` from your update URL) |
| The panel itself | **Settings > Update panel** (downloads the new files, validates them, backs up the old ones to `/opt/ai-panel/backup/`, restarts) |
| Ollama + llama.cpp | **Settings > Update Ollama + llama.cpp** |
| Models | **AI Models > Update all** |

To add a new app for everyone, add an entry to `apps_catalog.json` in your GitHub repo, then click **Refresh app catalog**.

### Catalog entry format

```json
{
  "id": "my-app", "name": "My App", "category": "Web", "icon": "🚀", "port": 8100,
  "description": "One line shown on the card",
  "env": { "DB_PASSWORD": "{{random}}" },
  "inputs": [{ "key": "API_KEY", "label": "API key", "required": false, "secret": true }],
  "gpu": { "services": ["app"], "images": { "nvidia": "img:cuda", "amd": "img:rocm" } },
  "chown": { "uid": 1000, "dirs": ["data"] },
  "compose": "services:\n  app:\n    image: nginx\n    ports:\n      - \"${APP_PORT}:80\"\n    volumes:\n      - ${DATA_DIR}/data:/usr/share/nginx/html\n"
}
```
Variables available in compose: `${APP_PORT}`, `${DATA_DIR}` (`/opt/ai-panel/data/<id>`), `${TZ}`, `${HOST_IP}`, plus every `env` and `inputs` key. `{{random}}` and `{{random_hex}}` secrets are generated once and kept across updates. You can see them in **Settings > App secrets**.

## Security checklist

1. Change the admin password on first login.
2. The panel gives **root** access (terminal and Docker). Don't expose port 1234 to the internet. Use it on your LAN or VPN (WireGuard is in the store), or put it behind Cloudflare Tunnel with Cloudflare Access.
3. Login is rate-limited (8 failures per 5 minutes per IP). Sessions last 12 hours and end when the password changes. API calls require a CSRF header.
4. Ollama and llama.cpp listen on `127.0.0.1` only by default.
5. If `ufw` is active, the installer opens only the panel port.

## Services and paths

```
systemctl status ai-panel ai-llama ollama docker
journalctl -u ai-panel -f
/opt/ai-panel/config.json        login and settings (mode 600)
/opt/ai-panel/apps/<id>/         each app's docker-compose.yml + .env
/opt/ai-panel/data/<id>/         each app's persistent data
/opt/ai-panel/models/            GGUF files for llama.cpp
```

Reset a forgotten password:
`sudo /opt/ai-panel/venv/bin/python /opt/ai-panel/app.py --set-password admin NEWPASSWORD && sudo systemctl restart ai-panel`

## Notes on specific apps

* **Hermes Agent / OpenClaw:** run their one-time setup from the Terminal tab. The exact command is in the install dialog. For OpenClaw, if the dashboard isn't reachable on first start, check its logs. Its gateway bind setting may need `lan`.
* **Cloudflare Tunnel:** paste the tunnel token. In Cloudflare, point each public hostname at `http://localhost:<app port>`.
* **InvoicePlane:** finish the web setup wizard. The database host is `db`, and the credentials are in App secrets.
* **Vaultwarden:** browsers require HTTPS, so publish it through Cloudflare Tunnel or Nginx Proxy Manager.
