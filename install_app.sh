#!/usr/bin/env bash
# =====================================================================================
#  AI Server Control Panel - 1-step installer for Ubuntu Server 24.04 / 26.04 LTS
#
#  Installs into /opt/ai-panel and registers systemd services:
#    ai-panel.service   web panel on port 1234 (FastAPI)
#    ai-llama.service   llama.cpp server (Vulkan build: pools NVIDIA + AMD + Intel GPUs)
#    ollama.service     Ollama (installed by the official script)
#  No kernel changes, no custom ISO. Re-running the script upgrades in place and keeps
#  your config, apps and models.
#
#  Usage:  sudo bash install_app.sh [options]
#    --port N            panel port (default 1234)
#    --no-drivers        do not install the NVIDIA driver automatically
#    --update-engines    only update Ollama + llama.cpp, then exit
#    --source URL        base URL to download app.py/index.html/apps_catalog.json from
#                        (used when the files are not next to this script; also becomes
#                         the panel's "Update panel" source)
# =====================================================================================
set -Eeuo pipefail

PANEL_HOME=/opt/ai-panel
PORT=1234
INSTALL_DRIVERS=1
ONLY_ENGINES=0
SOURCE_URL="${AIPANEL_SOURCE:-https://raw.githubusercontent.com/nnlahpai-ai-control-panel/ai-control-panel/main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
export DEBIAN_FRONTEND=noninteractive

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --no-drivers) INSTALL_DRIVERS=0; shift ;;
    --update-engines) ONLY_ENGINES=1; shift ;;
    --source) SOURCE_URL="${2%/}"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

C_G='\033[1;32m'; C_Y='\033[1;33m'; C_R='\033[1;31m'; C_B='\033[1;36m'; C_0='\033[0m'
log()  { echo -e "${C_B}==>${C_0} $*"; }
ok()   { echo -e "${C_G} ✓${C_0} $*"; }
warn() { echo -e "${C_Y} !${C_0} $*"; }
die()  { echo -e "${C_R} ✗ $*${C_0}"; exit 1; }
trap 'die "Failed at line $LINENO: $BASH_COMMAND"' ERR

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || warn "This is ${PRETTY_NAME:-unknown OS}; only Ubuntu 24.04/26.04 is tested."
ARCH="$(dpkg --print-architecture)"   # amd64 / arm64
HAS_SYSTEMD=0; [[ -d /run/systemd/system ]] && HAS_SYSTEMD=1

# ----------------------------------------------------------------------------- GPUs
detect_gpus() {
  HAS_NVIDIA=0; HAS_AMD=0; HAS_INTEL=0
  if command -v lspci >/dev/null; then
    lspci -nn | grep -Ei 'vga|3d|display' | grep -qi '\[10de:' && HAS_NVIDIA=1 || true
    lspci -nn | grep -Ei 'vga|3d|display' | grep -qi '\[1002:' && HAS_AMD=1 || true
    lspci -nn | grep -Ei 'vga|3d|display' | grep -qi '\[8086:' && HAS_INTEL=1 || true
  fi
  log "GPUs found: NVIDIA=$HAS_NVIDIA AMD=$HAS_AMD Intel=$HAS_INTEL"
}

# ----------------------------------------------------------------------------- llama.cpp
install_llama() {
  log "Installing llama.cpp (Vulkan build for mixed-GPU pooling)"
  local api asset url tmp
  tmp="$(mktemp -d)"
  local want="ubuntu-vulkan-x64"; [[ "$ARCH" == "arm64" ]] && want="ubuntu-vulkan-arm64"
  api="$(curl -fsSL https://api.github.com/repos/ggml-org/llama.cpp/releases/latest || true)"
  url="$(echo "$api" | jq -r --arg w "$want" '.assets[]?.browser_download_url | select(test($w + "\\.(zip|tar\\.gz)$"))' | head -n1 || true)"
  local tag=""
  if [[ -n "$url" ]]; then
    tag="$(echo "$api" | jq -r '.tag_name')"
  else
    # GitHub API rate-limited or blocked: find the newest bNNNN tag with git and build the URL
    # (the newest tags may still be building, so try the last few)
    local t ext try
    for t in $(git ls-remote --tags --refs https://github.com/ggml-org/llama.cpp 2>/dev/null | awk -F/ '{print $3}' | grep -E '^b[0-9]+$' | sort -V | tail -n6 | tac); do
      for ext in tar.gz zip; do
        try="https://github.com/ggml-org/llama.cpp/releases/download/$t/llama-$t-bin-$want.$ext"
        if curl -fsIL "$try" >/dev/null 2>&1; then url="$try"; tag="$t"; break 2; fi
      done
    done
  fi
  if [[ -n "$url" ]]; then
    log "Downloading $(basename "$url")"
    curl -fL --retry 3 -o "$tmp/llama.pkg" "$url"
    mkdir -p "$tmp/x"
    if [[ "$url" == *.zip ]]; then unzip -q "$tmp/llama.pkg" -d "$tmp/x"; else tar -xzf "$tmp/llama.pkg" -C "$tmp/x"; fi
    local bin; bin="$(find "$tmp/x" -type f -name llama-server | head -n1)"
    [[ -n "$bin" ]] || { warn "llama-server not in release package"; url=""; }
    if [[ -n "$url" ]]; then
      rm -rf "$PANEL_HOME/llama.cpp.new"; mkdir -p "$PANEL_HOME/llama.cpp.new"
      cp -a "$(dirname "$bin")"/. "$PANEL_HOME/llama.cpp.new/"
      rm -rf "$PANEL_HOME/llama.cpp"; mv "$PANEL_HOME/llama.cpp.new" "$PANEL_HOME/llama.cpp"
      echo "$tag" > "$PANEL_HOME/llama.cpp/VERSION"
    fi
  fi
  if [[ -z "$url" ]]; then
    warn "No prebuilt Vulkan package for $ARCH, building from source (5-15 min)"
    apt-get install -y -qq build-essential cmake git libvulkan-dev glslc libcurl4-openssl-dev >/dev/null
    rm -rf "$tmp/src"
    if ! { git clone -q --depth 1 https://github.com/ggml-org/llama.cpp "$tmp/src" \
        && cmake -S "$tmp/src" -B "$tmp/src/build" -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=ON >/dev/null \
        && cmake --build "$tmp/src/build" --config Release -j"$(nproc)" --target llama-server >/dev/null; }; then
      warn "llama.cpp build failed. Ollama still works; retry later: Settings > Update Ollama + llama.cpp"
      rm -rf "$tmp"; return 0
    fi
    rm -rf "$PANEL_HOME/llama.cpp"; mkdir -p "$PANEL_HOME/llama.cpp"
    cp -a "$tmp/src/build/bin"/. "$PANEL_HOME/llama.cpp/"
    find "$tmp/src/build" -name '*.so*' -exec cp -a {} "$PANEL_HOME/llama.cpp/" \;
    git -C "$tmp/src" rev-parse --short HEAD > "$PANEL_HOME/llama.cpp/VERSION"
  fi
  rm -rf "$tmp"
  ok "llama.cpp $(cat "$PANEL_HOME/llama.cpp/VERSION" 2>/dev/null) in $PANEL_HOME/llama.cpp"
}

# ----------------------------------------------------------------------------- Ollama
install_ollama() {
  log "Installing / updating Ollama"
  if ! curl -fsSL https://ollama.com/install.sh | sh; then
    warn "Ollama install failed (network?). The panel still works with llama.cpp. Retry later: Settings > Update Ollama + llama.cpp"
    return 0
  fi
  mkdir -p /etc/systemd/system/ollama.service.d
  cat > /etc/systemd/system/ollama.service.d/ai-panel.conf <<'EOF'
# Written by AI Panel installer
[Service]
# Spread one model across ALL GPUs (proportional to free VRAM), not just the first that fits
Environment="OLLAMA_SCHED_SPREAD=1"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_FLASH_ATTENTION=1"
# Localhost only. Apps on the host network (Open WebUI) and the panel can reach it.
# To let Docker apps on the bridge network use Ollama, change to 0.0.0.0:11434 and firewall port 11434.
Environment="OLLAMA_HOST=127.0.0.1:11434"
# Ollama pools GPUs of the SAME vendor. For NVIDIA + AMD in one model use llama.cpp in the panel,
# or try Ollama's experimental Vulkan backend by uncommenting the next line:
#Environment="OLLAMA_VULKAN=1"
EOF
  if [[ $HAS_SYSTEMD -eq 1 ]]; then systemctl daemon-reload; systemctl enable --now ollama >/dev/null 2>&1 || true; systemctl restart ollama || true; fi
  ok "$(ollama --version 2>/dev/null | tail -n1)"
}

update_engines() {
  detect_gpus
  install_ollama
  install_llama
  if [[ $HAS_SYSTEMD -eq 1 ]] && systemctl is-active --quiet ai-llama; then systemctl restart ai-llama; fi
  ok "Engines updated"
}

if [[ $ONLY_ENGINES -eq 1 ]]; then
  command -v jq >/dev/null || apt-get install -y -qq jq unzip >/dev/null
  update_engines; exit 0
fi

# ----------------------------------------------------------------------------- base packages
log "Installing base packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates gnupg jq unzip git \
  pciutils lsb-release vulkan-tools libvulkan1 mesa-vulkan-drivers libgomp1 >/dev/null
apt-get install -y -qq --no-install-recommends smartmontools lm-sensors dmidecode >/dev/null || warn "smartmontools not installed (disk health will be hidden)"
ok "Base packages"
detect_gpus

# ----------------------------------------------------------------------------- Docker
if ! command -v docker >/dev/null; then
  log "Installing Docker Engine"
  if ! curl -fsSL https://get.docker.com | sh; then
    warn "get.docker.com unreachable, installing Ubuntu's docker.io package instead"
    apt-get install -y -qq docker.io >/dev/null
    apt-get install -y -qq docker-compose-v2 >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
  fi
else
  ok "Docker already installed: $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || apt-get install -y -qq docker-compose-v2 >/dev/null
[[ $HAS_SYSTEMD -eq 1 ]] && systemctl enable --now docker >/dev/null 2>&1 || true

# ----------------------------------------------------------------------------- NVIDIA
NEED_REBOOT=0
if [[ $HAS_NVIDIA -eq 1 ]]; then
  if ! command -v nvidia-smi >/dev/null || ! nvidia-smi >/dev/null 2>&1; then
    if [[ $INSTALL_DRIVERS -eq 1 ]]; then
      log "Installing NVIDIA driver (ubuntu-drivers, server/compute flavour)"
      apt-get install -y -qq ubuntu-drivers-common >/dev/null
      ubuntu-drivers install --gpgpu || ubuntu-drivers install || warn "Driver install failed - install manually"
      NEED_REBOOT=1
    else
      warn "NVIDIA GPU found but no working driver (--no-drivers given)"
    fi
  else
    ok "NVIDIA driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
  fi
  log "Installing NVIDIA Container Toolkit (GPU passthrough for Docker apps)"
  if [[ ! -f /etc/apt/sources.list.d/nvidia-container-toolkit.list ]]; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -qq
  fi
  apt-get install -y -qq nvidia-container-toolkit >/dev/null
  nvidia-ctk runtime configure --runtime=docker >/dev/null
  [[ $HAS_SYSTEMD -eq 1 ]] && systemctl restart docker || true
  ok "NVIDIA Container Toolkit"
fi

# ----------------------------------------------------------------------------- AMD
if [[ $HAS_AMD -eq 1 ]]; then
  # amdgpu is in the stock Ubuntu kernel; Vulkan (RADV) comes from mesa-vulkan-drivers.
  # Ollama's installer adds its own ROCm libraries. Docker apps get /dev/kfd + /dev/dri.
  ok "AMD GPU: using in-kernel amdgpu + Mesa RADV Vulkan (no kernel changes)"
  getent group render >/dev/null && usermod -aG render,video root || true
fi

# ----------------------------------------------------------------------------- panel files
log "Installing panel into $PANEL_HOME"
mkdir -p "$PANEL_HOME"/{apps,data,models,backup,tmp}
chmod 755 "$PANEL_HOME" "$PANEL_HOME/models"
FILES=(app.py index.html apps_catalog.json)
if [[ -f "$SCRIPT_DIR/app.py" && -f "$SCRIPT_DIR/index.html" && -d "$SCRIPT_DIR/static" ]]; then
  if [[ "$SCRIPT_DIR" != "$PANEL_HOME" ]]; then
    for f in "${FILES[@]}"; do cp "$SCRIPT_DIR/$f" "$PANEL_HOME/$f"; done
    mkdir -p "$PANEL_HOME/static"; cp -r "$SCRIPT_DIR/static/." "$PANEL_HOME/static/"
    cp "$SCRIPT_DIR/install_app.sh" "$PANEL_HOME/install_app.sh"
  fi
elif [[ -n "$SOURCE_URL" ]]; then
  mkdir -p "$PANEL_HOME/static"
  for f in "${FILES[@]}" install_app.sh static/tailwind.css static/xterm.css static/xterm.js static/addon-fit.js; do curl -fsSL "$SOURCE_URL/$f" -o "$PANEL_HOME/$f.new" && mv "$PANEL_HOME/$f.new" "$PANEL_HOME/$f"; done
else
  die "app.py not found next to this script. Run it from the folder with the panel files, or pass --source URL"
fi
chmod +x "$PANEL_HOME/install_app.sh"

log "Python environment"
python3 -m venv "$PANEL_HOME/venv"
"$PANEL_HOME/venv/bin/pip" install -q --upgrade pip
"$PANEL_HOME/venv/bin/pip" install -q --upgrade "fastapi>=0.110" "uvicorn[standard]>=0.29" "httpx>=0.27" "psutil>=5.9" "pyyaml>=6"
ok "Python deps"

# ----------------------------------------------------------------------------- engines
install_ollama
install_llama

# llama.cpp service (panel writes llama-run.sh when you press Start)
if [[ ! -f "$PANEL_HOME/llama-run.sh" ]]; then
  printf '#!/bin/sh\necho "No model configured yet - choose one in the AI Panel (AI Models > llama.cpp)"\nexit 0\n' > "$PANEL_HOME/llama-run.sh"
  chmod 755 "$PANEL_HOME/llama-run.sh"
fi
cat > /etc/systemd/system/ai-llama.service <<EOF
[Unit]
Description=AI Panel - llama.cpp server (multi-GPU)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PANEL_HOME/llama-run.sh
Restart=on-failure
RestartSec=5
LimitMEMLOCK=infinity

[Install]
WantedBy=multi-user.target
EOF

# ----------------------------------------------------------------------------- panel service
cat > /etc/systemd/system/ai-panel.service <<EOF
[Unit]
Description=AI Server Control Panel (port $PORT)
After=network-online.target docker.service ollama.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PANEL_HOME
Environment=AIPANEL_HOME=$PANEL_HOME
Environment=AIPANEL_PORT=$PORT
Environment=AIPANEL_UPDATE_URL=$SOURCE_URL
Environment=HOME=/root
ExecStart=$PANEL_HOME/venv/bin/python $PANEL_HOME/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# ----------------------------------------------------------------------------- admin user
INITIAL_PW=""
if ! AIPANEL_HOME=$PANEL_HOME "$PANEL_HOME/venv/bin/python" "$PANEL_HOME/app.py" --has-users; then
  INITIAL_PW="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
  AIPANEL_HOME=$PANEL_HOME "$PANEL_HOME/venv/bin/python" "$PANEL_HOME/app.py" --set-password admin "$INITIAL_PW" >/dev/null
  echo "admin / $INITIAL_PW" > "$PANEL_HOME/initial_password.txt"; chmod 600 "$PANEL_HOME/initial_password.txt"
fi
if [[ -n "$SOURCE_URL" ]]; then
  "$PANEL_HOME/venv/bin/python" - "$PANEL_HOME/config.json" "$SOURCE_URL" <<'PY'
import json, sys
p, url = sys.argv[1], sys.argv[2]
c = json.load(open(p))
if not c.get("update_url"):
    c["update_url"] = url
json.dump(c, open(p, "w"), indent=2)
PY
fi
chmod 600 "$PANEL_HOME/config.json"

if [[ $HAS_SYSTEMD -eq 1 ]]; then
  systemctl daemon-reload
  systemctl enable ai-llama >/dev/null 2>&1
  systemctl enable ai-panel >/dev/null 2>&1
  systemctl restart ai-panel
fi

# ----------------------------------------------------------------------------- firewall
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null && ok "ufw: opened port $PORT"
fi

# ----------------------------------------------------------------------------- done
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
for i in $(seq 1 20); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
echo
echo -e "${C_G}=====================================================================${C_0}"
echo -e "${C_G}  AI Server Control Panel is ready${C_0}"
echo -e "  Open:      ${C_B}http://${IP:-<server-ip>}:$PORT${C_0}"
if [[ -n "$INITIAL_PW" ]]; then
  echo -e "  Login:     ${C_Y}admin / $INITIAL_PW${C_0}   (saved in $PANEL_HOME/initial_password.txt)"
  echo    "             Change it in Settings after your first login."
else
  echo    "  Login:     your existing account (config kept)"
fi
echo    "  Services:  ai-panel, ai-llama, ollama, docker"
[[ $NEED_REBOOT -eq 1 ]] && echo -e "  ${C_Y}NVIDIA driver installed: reboot now (sudo reboot) so the GPUs show up.${C_0}"
echo -e "${C_G}=====================================================================${C_0}"
