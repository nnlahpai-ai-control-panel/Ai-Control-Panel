#!/usr/bin/env python3
"""
AI Server Control Panel - FastAPI backend
Runs on port 1234. Manages Ollama, llama.cpp (multi-vendor GPU pooling via Vulkan),
a 1-click Docker App Store, multi-GPU telemetry and a browser terminal.

Install dir: /opt/ai-panel   (override with AIPANEL_HOME)
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import glob
import hashlib
import hmac
import json
import os
import pty
import re
import secrets
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import psutil
import yaml
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

VERSION = "1.1.0"
HOME = Path(os.environ.get("AIPANEL_HOME", "/opt/ai-panel"))
CONFIG_FILE = HOME / "config.json"
CATALOG_FILE = HOME / "apps_catalog.json"
CUSTOM_FILE = HOME / "custom_apps.json"
APPS_DIR = HOME / "apps"
DATA_DIR = HOME / "data"
MODELS_DIR = HOME / "models"
LLAMA_DIR = HOME / "llama.cpp"
LLAMA_RUN = HOME / "llama-run.sh"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
PORT = int(os.environ.get("AIPANEL_PORT", "1234"))
SESSION_HOURS = 12
COOKIE = "aipanel_session"
PROJECT_PREFIX = "aip-"

for d in (APPS_DIR, DATA_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------------------
# Config & auth
# --------------------------------------------------------------------------------------
_cfg_lock = threading.Lock()

DEFAULT_LLAMA = {
    "model": "",
    "port": 8081,
    "host": "127.0.0.1",
    "ctx": 8192,
    "ngl": -1,
    "split_mode": "layer",
    "tensor_split": "auto",
    "devices": "",
    "extra": "--flash-attn auto",
}


def load_config() -> dict:
    with _cfg_lock:
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text())
        else:
            cfg = {}
        changed = False
        if "secret" not in cfg:
            cfg["secret"] = secrets.token_hex(32)
            changed = True
        cfg.setdefault("users", {})
        cfg.setdefault("update_url", os.environ.get("AIPANEL_UPDATE_URL", ""))
        llama = cfg.setdefault("llama", {})
        for k, v in DEFAULT_LLAMA.items():
            llama.setdefault(k, v)
        if changed or not CONFIG_FILE.exists():
            _write_config(cfg)
        return cfg


def _write_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def save_config(cfg: dict) -> None:
    with _cfg_lock:
        _write_config(cfg)


def hash_password(pw: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, iters, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


def make_token(user: str) -> str:
    cfg = load_config()
    exp = int(time.time()) + SESSION_HOURS * 3600
    # Bind token to the password hash so changing a password logs out old sessions.
    pw_fp = hashlib.sha256(cfg["users"].get(user, "").encode()).hexdigest()[:16]
    payload = f"{user}|{exp}|{pw_fp}"
    sig = hmac.new(cfg["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def check_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        user, exp, pw_fp, sig = raw.split("|")
        cfg = load_config()
        payload = f"{user}|{exp}|{pw_fp}"
        good = hmac.new(cfg["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good) or int(exp) < time.time():
            return None
        if user not in cfg["users"]:
            return None
        if hashlib.sha256(cfg["users"][user].encode()).hexdigest()[:16] != pw_fp:
            return None
        return user
    except Exception:
        return None


_login_failures: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hist = [t for t in _login_failures.get(ip, []) if now - t < 300]
    _login_failures[ip] = hist
    return len(hist) >= 8


# --------------------------------------------------------------------------------------
# Background tasks (downloads, docker actions) with progress
# --------------------------------------------------------------------------------------
TASKS: dict[str, dict] = {}


def new_task(kind: str, title: str) -> dict:
    tid = uuid.uuid4().hex[:12]
    t = {"id": tid, "kind": kind, "title": title, "status": "running", "progress": None,
         "message": "Starting...", "log": [], "started": time.time(), "finished": None,
         "total": None, "completed": None}
    TASKS[tid] = t
    # keep the list small
    if len(TASKS) > 200:
        for old in sorted(TASKS.values(), key=lambda x: x["started"])[:50]:
            if old["status"] != "running":
                TASKS.pop(old["id"], None)
    return t


def task_log(t: dict, line: str) -> None:
    line = line.rstrip()
    if not line:
        return
    t["log"].append(line)
    if len(t["log"]) > 400:
        del t["log"][:100]
    t["message"] = line[-300:]


def task_done(t: dict, ok: bool = True, msg: Optional[str] = None) -> None:
    t["status"] = "done" if ok else "error"
    t["finished"] = time.time()
    if ok and t.get("progress") is not None:
        t["progress"] = 100
    if msg:
        task_log(t, msg)


async def run_cmd(t: Optional[dict], cmd: list[str], cwd: Optional[str] = None, env: Optional[dict] = None) -> int:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    assert proc.stdout
    buf = b""
    while True:
        chunk = await proc.stdout.read(4096)
        if not chunk:
            break
        buf += chunk
        # docker progress uses \r; split on both
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            if t is not None:
                task_log(t, p.decode(errors="replace"))
    if buf and t is not None:
        task_log(t, buf.decode(errors="replace"))
    return await proc.wait()


def run_quick(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timeout"


# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------
app = FastAPI(title="AI Server Control Panel", version=VERSION, docs_url=None, redoc_url=None)

@app.exception_handler(httpx.HTTPError)
async def httpx_error(request: Request, exc: httpx.HTTPError):
    return JSONResponse({"detail": f"Network error: {exc}"}, status_code=502)


PUBLIC_PATHS = {"/", "/index.html", "/api/login", "/api/health", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or not path.startswith("/api/"):
        return await call_next(request)
    user = check_token(request.cookies.get(COOKIE))
    if not user:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    # CSRF defence: state-changing calls must come from our own page (custom header).
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "aipanel":
        return JSONResponse({"detail": "Missing CSRF header"}, status_code=403)
    request.state.user = user
    return await call_next(request)


@app.get("/")
@app.get("/index.html")
async def index():
    return FileResponse(HOME / "index.html", headers={"Cache-Control": "no-store"})


(HOME / "static").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=HOME / "static"), name="static")


@app.get("/api/health")
async def health():
    return {"ok": True, "version": VERSION}


class LoginBody(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        raise HTTPException(429, "Too many failed attempts. Wait 5 minutes.")
    cfg = load_config()
    stored = cfg["users"].get(body.username)
    if not stored or not verify_password(body.password, stored):
        _login_failures.setdefault(ip, []).append(time.time())
        await asyncio.sleep(1)
        raise HTTPException(401, "Invalid username or password")
    _login_failures.pop(ip, None)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, make_token(body.username), httponly=True, samesite="strict",
                        secure=secure, max_age=SESSION_HOURS * 3600)
    return {"ok": True, "user": body.username}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    return {"user": request.state.user, "version": VERSION, "hostname": socket.gethostname()}


class PasswordBody(BaseModel):
    current: str
    new: str


@app.post("/api/change-password")
async def change_password(body: PasswordBody, request: Request, response: Response):
    cfg = load_config()
    user = request.state.user
    if not verify_password(body.current, cfg["users"].get(user, "")):
        raise HTTPException(400, "Current password is wrong")
    if len(body.new) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    cfg["users"][user] = hash_password(body.new)
    save_config(cfg)
    response.set_cookie(COOKIE, make_token(user), httponly=True, samesite="strict", max_age=SESSION_HOURS * 3600)
    return {"ok": True}


@app.get("/api/tasks")
async def list_tasks():
    items = sorted(TASKS.values(), key=lambda x: x["started"], reverse=True)[:50]
    return [{k: v for k, v in t.items() if k != "log"} for t in items]


@app.get("/api/tasks/{tid}")
async def get_task(tid: str):
    t = TASKS.get(tid)
    if not t:
        raise HTTPException(404, "No such task")
    return t


# --------------------------------------------------------------------------------------
# GPU telemetry (NVIDIA via nvidia-smi, AMD via sysfs, Intel basic via sysfs)
# --------------------------------------------------------------------------------------
def _read(path: str) -> Optional[str]:
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def nvidia_gpus() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return []
    rc, out = run_quick(["nvidia-smi", "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw,pci.bus_id,uuid",
                         "--format=csv,noheader,nounits"], timeout=8)
    if rc != 0:
        return []
    gpus = []
    for line in out.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 9:
            continue

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        gpus.append({"vendor": "NVIDIA", "name": p[1], "vram_total_mb": num(p[2]), "vram_used_mb": num(p[3]),
                     "util": num(p[4]), "temp": num(p[5]), "power_w": num(p[6]), "bus": p[7].lower()[-12:],
                     "uuid": p[8], "nv_index": int(p[0])})
    return gpus


def sysfs_gpus() -> list[dict]:
    gpus = []
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        if "-" in os.path.basename(card):
            continue
        dev = card + "/device"
        vendor = _read(dev + "/vendor")
        if vendor not in ("0x1002", "0x8086"):
            continue  # NVIDIA handled by nvidia-smi
        total = _read(dev + "/mem_info_vram_total")
        used = _read(dev + "/mem_info_vram_used")
        if vendor == "0x8086" and not total:
            continue  # integrated Intel graphics without VRAM info
        temp = None
        for h in glob.glob(dev + "/hwmon/hwmon*/temp1_input"):
            v = _read(h)
            if v:
                temp = int(v) / 1000
                break
        busy = _read(dev + "/gpu_busy_percent")
        name = _read(dev + "/product_name") or ""
        bus = os.path.basename(os.path.realpath(dev))
        if not name:
            rc, out = run_quick(["lspci", "-s", bus], timeout=3)
            name = out.split(": ", 1)[-1].strip() if rc == 0 and out else ("AMD GPU" if vendor == "0x1002" else "Intel GPU")
        power = None
        for h in glob.glob(dev + "/hwmon/hwmon*/power1_average") + glob.glob(dev + "/hwmon/hwmon*/power1_input"):
            v = _read(h)
            if v:
                power = round(int(v) / 1_000_000, 1)
                break
        render = sorted(glob.glob(dev + "/drm/renderD*"))
        uid = _read(dev + "/unique_id")
        gpus.append({"vendor": "AMD" if vendor == "0x1002" else "Intel", "name": name,
                     "render": "/dev/dri/" + os.path.basename(render[0]) if render else None,
                     "card": "/dev/dri/" + os.path.basename(card), "uuid": f"GPU-{uid}" if uid else None,
                     "vram_total_mb": int(total) / 1048576 if total else None,
                     "vram_used_mb": int(used) / 1048576 if used else None,
                     "util": float(busy) if busy else None, "temp": temp, "power_w": power, "bus": bus[-12:]})
    return gpus


def all_gpus() -> list[dict]:
    gpus = nvidia_gpus() + sysfs_gpus()
    for i, g in enumerate(gpus):
        g["index"] = i
    return gpus


def gpu_vendors() -> set[str]:
    v = set()
    if shutil.which("nvidia-smi") and nvidia_gpus():
        v.add("nvidia")
    if os.path.exists("/dev/kfd") or any(_read(c + "/device/vendor") == "0x1002" for c in glob.glob("/sys/class/drm/card[0-9]")):
        v.add("amd")
    return v


_vk_cache: dict = {"t": 0, "map": {}}


def vulkan_bus_map() -> dict[str, int]:
    """PCI bus id ('0000:01:00.0') -> Vulkan physical-device index (what GGML_VK_VISIBLE_DEVICES uses)."""
    if time.time() - _vk_cache["t"] < 120:
        return _vk_cache["map"]
    m: dict[str, int] = {}
    if shutil.which("vulkaninfo"):
        rc, out = run_quick(["vulkaninfo"], timeout=30)
        cur = None
        vals: dict[str, int] = {}
        for line in out.splitlines():
            g = re.match(r"^GPU(\d+):\s*$", line.strip())
            if g:
                cur, vals = int(g.group(1)), {}
                continue
            k = re.match(r"^\s*(pciDomain|pciBus|pciDevice|pciFunction)\s*=\s*(\S+)", line)
            if k and cur is not None:
                raw = k.group(2)
                vals[k.group(1)] = int(raw, 16) if raw.lower().startswith("0x") else int(raw)
                if len(vals) == 4:
                    bus = "%04x:%02x:%02x.%x" % (vals["pciDomain"], vals["pciBus"], vals["pciDevice"], vals["pciFunction"])
                    m.setdefault(bus, cur)
                    cur = None  # first PCI block per GPU is enough
    _vk_cache.update(t=time.time(), map=m)
    return m


def gpu_inventory() -> list[dict]:
    gpus = all_gpus()
    vk = vulkan_bus_map()
    for g in gpus:
        g["vk_index"] = vk.get(g["bus"])
    return gpus


def pool_gpus(pool_id: str, gpus: Optional[list[dict]] = None) -> list[dict]:
    """GPUs that belong to a pool ('' = all GPUs)."""
    gpus = gpus if gpus is not None else gpu_inventory()
    if not pool_id:
        return gpus
    pool = load_config().get("pools", {}).get(pool_id)
    if not pool:
        raise HTTPException(400, f"GPU pool '{pool_id}' does not exist")
    members = set(pool.get("gpus", []))
    return [g for g in gpus if g["bus"] in members]


def pool_vk_env(pool_id: str) -> dict[str, str]:
    if not pool_id:
        return {}
    sel = pool_gpus(pool_id)
    if not sel:
        raise HTTPException(400, "This GPU pool has no GPUs that are currently installed")
    idx = [g["vk_index"] for g in sel]
    if any(i is None for i in idx):
        raise HTTPException(400, "Could not match pool GPUs to Vulkan devices (is vulkan-tools installed and the driver loaded?)")
    return {"GGML_VK_VISIBLE_DEVICES": ",".join(str(i) for i in sorted(idx))}


_last_net = {"t": time.time(), "rx": 0, "tx": 0}


# --------------------------------------------------------------------------------------
# GPU pools: split cards into named groups (e.g. "AI" and "Media") and give each engine/app a pool
# --------------------------------------------------------------------------------------
POOL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,30}$")
OLLAMA_POOL_CONF = Path("/etc/systemd/system/ollama.service.d/ai-panel-pool.conf")


class PoolsBody(BaseModel):
    pools: dict[str, dict]


@app.get("/api/pools")
async def get_pools():
    cfg = load_config()
    gpus = await asyncio.to_thread(gpu_inventory)
    return {"pools": cfg.get("pools", {}), "gpus": gpus, "ollama_pool": cfg.get("ollama_pool", ""),
            "llama_pool": cfg["llama"].get("pool", "")}


@app.post("/api/pools")
async def save_pools(body: PoolsBody):
    clean = {}
    for pid, p in body.pools.items():
        if not POOL_ID_RE.match(pid):
            raise HTTPException(400, f"Pool id '{pid}': use lowercase letters, numbers and dashes")
        name = str(p.get("name") or pid)[:40]
        gpus = [str(b) for b in p.get("gpus", []) if re.match(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f]$", str(b))]
        clean[pid] = {"name": name, "gpus": gpus}
    cfg = load_config()
    in_use = []
    if cfg["llama"].get("pool") and cfg["llama"]["pool"] not in clean:
        in_use.append("llama.cpp")
    if cfg.get("ollama_pool") and cfg["ollama_pool"] not in clean:
        in_use.append("Ollama")
    for d in APPS_DIR.iterdir():
        e = d / ".env"
        if e.exists():
            gp = parse_env(e.read_text()).get("GPU_POOL", "")
            if gp and gp not in clean:
                in_use.append(d.name)
    if in_use:
        raise HTTPException(400, "Pool still used by: " + ", ".join(in_use) + ". Switch them to another pool first.")
    cfg["pools"] = clean
    save_config(cfg)
    return {"ok": True, "note": "Restart llama.cpp / re-apply the Ollama pool / Reconfigure apps to use changed pool members."}


class PoolPick(BaseModel):
    pool: str = ""


@app.post("/api/ollama/pool")
async def ollama_pool(body: PoolPick):
    """Limit Ollama to one pool's GPUs (writes a systemd drop-in and restarts Ollama)."""
    cfg = load_config()
    if not body.pool:
        OLLAMA_POOL_CONF.unlink(missing_ok=True)
    else:
        sel = pool_gpus(body.pool)
        if not sel:
            raise HTTPException(400, "This pool has no installed GPUs")
        nv = [g["uuid"] for g in sel if g["vendor"] == "NVIDIA" and g.get("uuid")]
        amd = [g["uuid"] for g in sel if g["vendor"] == "AMD" and g.get("uuid")]
        if any(g["vendor"] == "AMD" and not g.get("uuid") for g in sel):
            raise HTTPException(400, "An AMD GPU in this pool has no unique id in sysfs, so Ollama can't be limited to it")
        lines = ["# Written by AI Panel: Ollama only uses GPU pool " + body.pool, "[Service]",
                 f'Environment="CUDA_VISIBLE_DEVICES={",".join(nv) or "-1"}"',
                 f'Environment="ROCR_VISIBLE_DEVICES={",".join(amd) or "-1"}"']
        try:
            lines.append(f'Environment="GGML_VK_VISIBLE_DEVICES={pool_vk_env(body.pool)["GGML_VK_VISIBLE_DEVICES"]}"')
        except HTTPException:
            pass
        OLLAMA_POOL_CONF.parent.mkdir(parents=True, exist_ok=True)
        OLLAMA_POOL_CONF.write_text("\n".join(lines) + "\n")
    cfg["ollama_pool"] = body.pool
    save_config(cfg)
    run_quick(["systemctl", "daemon-reload"], timeout=20)
    rc, out = run_quick(["systemctl", "restart", "ollama"], timeout=60)
    return {"ok": rc == 0, "message": "Ollama restarted" if rc == 0 else out}


@app.get("/api/stats")
async def stats():
    gpus = await asyncio.to_thread(all_gpus)
    pools = load_config().get("pools", {})
    for g in gpus:
        g["pools"] = [p.get("name", pid) for pid, p in pools.items() if g["bus"] in p.get("gpus", [])]
    total = sum(g["vram_total_mb"] or 0 for g in gpus)
    used = sum(g["vram_used_mb"] or 0 for g in gpus)
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    n = psutil.net_io_counters()
    now = time.time()
    dt = max(now - _last_net["t"], 0.5)
    rx = (n.bytes_recv - _last_net["rx"]) / dt if _last_net["rx"] else 0
    tx = (n.bytes_sent - _last_net["tx"]) / dt if _last_net["tx"] else 0
    _last_net.update(t=now, rx=n.bytes_recv, tx=n.bytes_sent)
    load = os.getloadavg()
    return {
        "gpus": gpus,
        "pool": {"total_mb": total, "used_mb": used, "count": len(gpus)},
        "cpu": {"percent": psutil.cpu_percent(interval=None), "cores": psutil.cpu_count(), "load": load,
                "model": _cpu_model()},
        "ram": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
        "disk": {"total": du.total, "used": du.used, "percent": du.percent},
        "net": {"rx_bps": rx, "tx_bps": tx},
        "uptime": int(now - psutil.boot_time()),
        "hostname": socket.gethostname(),
    }


_cpu_name = None


def _cpu_model() -> str:
    global _cpu_name
    if _cpu_name is None:
        _cpu_name = "CPU"
        txt = _read("/proc/cpuinfo") or ""
        m = re.search(r"model name\s*:\s*(.+)", txt)
        if m:
            _cpu_name = m.group(1).strip()
    return _cpu_name


# --------------------------------------------------------------------------------------
# Ollama model manager
# --------------------------------------------------------------------------------------
MODEL_LIBRARY = [
    # name, approx download size GB, description, family
    ("llama3.2:3b", 2.0, "Meta Llama 3.2 3B - fast, small", "Llama"),
    ("llama3.1:8b", 4.9, "Meta Llama 3.1 8B - great all-rounder", "Llama"),
    ("llama3.3:70b", 43, "Meta Llama 3.3 70B - needs ~48GB VRAM+RAM", "Llama"),
    ("qwen3:8b", 5.2, "Qwen3 8B - reasoning + chat", "Qwen"),
    ("qwen3:14b", 9.3, "Qwen3 14B", "Qwen"),
    ("qwen3:30b", 19, "Qwen3 30B-A3B MoE - fast for its size", "Qwen"),
    ("qwen3:32b", 20, "Qwen3 32B dense", "Qwen"),
    ("qwen2.5-coder:14b", 9.0, "Qwen2.5 Coder 14B - coding", "Qwen"),
    ("qwen2.5-coder:32b", 20, "Qwen2.5 Coder 32B - top local coder", "Qwen"),
    ("qwen2.5vl:7b", 6.0, "Qwen2.5 VL 7B - vision", "Qwen"),
    ("deepseek-r1:8b", 5.2, "DeepSeek R1 8B - reasoning", "DeepSeek"),
    ("deepseek-r1:14b", 9.0, "DeepSeek R1 14B - reasoning", "DeepSeek"),
    ("deepseek-r1:32b", 20, "DeepSeek R1 32B - reasoning", "DeepSeek"),
    ("deepseek-coder-v2:16b", 8.9, "DeepSeek Coder V2 Lite 16B", "DeepSeek"),
    ("mistral:7b", 4.4, "Mistral 7B Instruct", "Mistral"),
    ("mistral-nemo:12b", 7.1, "Mistral Nemo 12B - 128k context", "Mistral"),
    ("mistral-small:24b", 14, "Mistral Small 24B", "Mistral"),
    ("gemma3:4b", 3.3, "Google Gemma 3 4B - vision", "Gemma"),
    ("gemma3:12b", 8.1, "Google Gemma 3 12B - vision", "Gemma"),
    ("gemma3:27b", 17, "Google Gemma 3 27B - vision", "Gemma"),
    ("gpt-oss:20b", 14, "OpenAI gpt-oss 20B open-weight", "OpenAI"),
    ("gpt-oss:120b", 65, "OpenAI gpt-oss 120B - needs ~70GB", "OpenAI"),
    ("phi4:14b", 9.1, "Microsoft Phi-4 14B", "Phi"),
    ("llava:13b", 8.0, "LLaVA 13B - vision", "Other"),
    ("nomic-embed-text", 0.27, "Embeddings for RAG", "Embedding"),
    ("bge-m3", 1.2, "Multilingual embeddings", "Embedding"),
]


async def ollama_get(path: str, timeout: float = 10) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(OLLAMA_URL + path)
        r.raise_for_status()
        return r.json()


@app.get("/api/ollama/status")
async def ollama_status():
    try:
        v = await ollama_get("/api/version", 3)
        return {"running": True, "version": v.get("version")}
    except Exception as e:
        return {"running": False, "error": str(e)}


@app.get("/api/ollama/models")
async def ollama_models():
    try:
        tags = await ollama_get("/api/tags")
        ps = await ollama_get("/api/ps")
    except Exception as e:
        raise HTTPException(503, f"Ollama not reachable at {OLLAMA_URL}: {e}")
    loaded = {m["name"]: m for m in ps.get("models", [])}
    out = []
    for m in tags.get("models", []):
        l = loaded.get(m["name"])
        out.append({"name": m["name"], "size": m.get("size", 0), "modified": m.get("modified_at"),
                    "family": (m.get("details") or {}).get("family"),
                    "params": (m.get("details") or {}).get("parameter_size"),
                    "quant": (m.get("details") or {}).get("quantization_level"),
                    "loaded": bool(l), "vram": l.get("size_vram") if l else None})
    return out


@app.get("/api/ollama/library")
async def ollama_library():
    return [{"name": n, "size_gb": s, "desc": d, "family": f} for n, s, d, f in MODEL_LIBRARY]


class ModelName(BaseModel):
    name: str


MODEL_RE = re.compile(r"^[A-Za-z0-9._\-/:]+$")


async def _ollama_pull(t: dict, name: str) -> bool:
    t["progress"] = 0
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", OLLAMA_URL + "/api/pull", json={"name": name, "stream": True}) as r:
                if r.status_code != 200:
                    task_done(t, False, f"Ollama error {r.status_code}: {(await r.aread()).decode()[:300]}")
                    return False
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    ev = json.loads(line)
                    if "error" in ev:
                        task_done(t, False, ev["error"])
                        return False
                    status = ev.get("status", "")
                    if ev.get("total"):
                        t["total"] = ev["total"]
                        t["completed"] = ev.get("completed", 0)
                        t["progress"] = round(100 * t["completed"] / t["total"], 1)
                    t["message"] = status
                    if not t["log"] or t["log"][-1] != status:
                        if not status.startswith("pulling") or not ev.get("completed"):
                            task_log(t, status)
        task_done(t, True, f"{name} ready")
        return True
    except Exception as e:
        task_done(t, False, f"Pull failed: {e}")
        return False


@app.post("/api/ollama/pull")
async def ollama_pull(body: ModelName):
    if not MODEL_RE.match(body.name):
        raise HTTPException(400, "Invalid model name")
    t = new_task("pull", f"Pull {body.name}")
    asyncio.create_task(_ollama_pull(t, body.name))
    return {"task": t["id"]}


@app.post("/api/ollama/update-all")
async def ollama_update_all():
    tags = await ollama_get("/api/tags")
    names = [m["name"] for m in tags.get("models", [])]
    t = new_task("pull", f"Update all models ({len(names)})")

    async def run():
        ok_all = True
        for i, n in enumerate(names):
            sub = new_task("pull", f"Update {n}")
            task_log(t, f"[{i + 1}/{len(names)}] updating {n}")
            ok_all &= await _ollama_pull(sub, n)
            t["progress"] = round(100 * (i + 1) / max(len(names), 1))
        task_done(t, ok_all, "All models updated" if ok_all else "Some updates failed")
    t["progress"] = 0
    asyncio.create_task(run())
    return {"task": t["id"]}


@app.post("/api/ollama/delete")
async def ollama_delete(body: ModelName):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request("DELETE", OLLAMA_URL + "/api/delete", json={"name": body.name})
    if r.status_code != 200:
        raise HTTPException(r.status_code, r.text)
    return {"ok": True}


@app.post("/api/ollama/unload")
async def ollama_unload(body: ModelName):
    async with httpx.AsyncClient(timeout=30) as c:
        await c.post(OLLAMA_URL + "/api/generate", json={"model": body.name, "keep_alive": 0})
    return {"ok": True}


# --------------------------------------------------------------------------------------
# llama.cpp (Vulkan build pools NVIDIA + AMD + Intel GPUs in one model)
# --------------------------------------------------------------------------------------
def llama_bin() -> Optional[str]:
    for p in (LLAMA_DIR / "llama-server", LLAMA_DIR / "bin" / "llama-server", LLAMA_DIR / "build" / "bin" / "llama-server"):
        if p.exists():
            return str(p)
    return shutil.which("llama-server")


DEV_RE = re.compile(r"^\s*([A-Za-z]+\d+):\s+(.+?)\s+\((\d+) MiB,\s*(\d+) MiB free\)\s*$")


def llama_devices(extra_env: Optional[dict] = None) -> list[dict]:
    b = llama_bin()
    if not b:
        return []
    env = {**os.environ, "LD_LIBRARY_PATH": str(Path(b).parent) + ":" + os.environ.get("LD_LIBRARY_PATH", ""), **(extra_env or {})}
    try:
        p = subprocess.run([b, "--list-devices"], capture_output=True, text=True, timeout=30, env=env)
    except Exception:
        return []
    devs = []
    for line in (p.stdout + p.stderr).splitlines():
        m = DEV_RE.match(line)
        if m:
            devs.append({"id": m.group(1), "name": m.group(2), "total_mb": int(m.group(3)), "free_mb": int(m.group(4))})
    return devs


def auto_tensor_split(devs: list[dict]) -> str:
    """Proportional split by total VRAM: 8GB+12GB+16GB -> 8,12,16 (llama.cpp normalises)."""
    if len(devs) < 2:
        return ""
    return ",".join(str(round(d["total_mb"] / 1024, 1)) for d in devs)


def build_llama_args(cfg: dict, devs: list[dict]) -> list[str]:
    l = cfg["llama"]
    model = l["model"]
    if not model:
        raise HTTPException(400, "Pick a GGUF model first")
    mpath = (MODELS_DIR / model).resolve()
    if not str(mpath).startswith(str(MODELS_DIR.resolve())) or not mpath.exists():
        raise HTTPException(400, "Model file not found")
    args = ["-m", str(mpath), "--host", str(l["host"]), "--port", str(int(l["port"])),
            "-c", str(int(l["ctx"])), "--split-mode", l["split_mode"], "--alias", Path(model).stem]
    if int(l["ngl"]) >= 0:  # -1 = let llama.cpp's --fit work out how many layers fit in VRAM
        args += ["-ngl", str(int(l["ngl"]))]
    chosen = devs
    if l.get("devices"):
        wanted = [x.strip() for x in l["devices"].split(",") if x.strip()]
        args += ["--device", ",".join(wanted)]
        chosen = [d for d in devs if d["id"] in wanted]
    ts = l.get("tensor_split", "auto")
    if ts == "fit":
        ts = ""  # let llama.cpp decide from free memory
    elif ts == "auto":
        ts = auto_tensor_split(chosen)
    if ts:
        args += ["--tensor-split", ts]
    if l.get("extra"):
        args += shlex.split(l["extra"])
    return args


@app.get("/api/llama/status")
async def llama_status():
    cfg = load_config()
    b = llama_bin()
    try:
        penv = pool_vk_env(cfg["llama"].get("pool", ""))
    except HTTPException:
        penv = {}
    devs = await asyncio.to_thread(llama_devices, penv) if b else []
    rc, out = run_quick(["systemctl", "is-active", "ai-llama.service"])
    out = out.strip().splitlines()[0] if out.strip() else "unknown"
    if out not in ("active", "inactive", "failed", "activating", "deactivating"):
        out = "unavailable"
    health = None
    if out == "active":
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"http://127.0.0.1:{cfg['llama']['port']}/health")
                health = r.json().get("status", r.status_code) if r.headers.get("content-type", "").startswith("application/json") else r.status_code
        except Exception:
            health = "starting"
    ver = None
    if b:
        rc2, vout = run_quick([b, "--version"], timeout=10)
        m = re.search(r"version:\s*(\S+.*)", vout)
        ver = m.group(1).strip() if m else None
    return {"installed": bool(b), "binary": b, "version": ver, "devices": devs, "auto_split": auto_tensor_split(devs),
            "service": out, "health": health, "config": cfg["llama"]}


@app.get("/api/llama/models")
async def llama_models():
    out = []
    for p in sorted(MODELS_DIR.rglob("*.gguf")):
        name = str(p.relative_to(MODELS_DIR))
        m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", name)
        if m and m.group(1) != "00001":
            continue  # only show first shard of split models
        size = p.stat().st_size
        if m:
            size = sum(x.stat().st_size for x in p.parent.glob(re.sub(r"-\d{5}-of-", "-*-of-", p.name)))
        out.append({"name": name, "size": size, "shards": int(m.group(2)) if m else 1})
    return out


class LlamaConfig(BaseModel):
    model: str = ""
    port: int = 8081
    host: str = "127.0.0.1"
    ctx: int = 8192
    ngl: int = -1
    split_mode: str = "layer"
    tensor_split: str = "auto"
    devices: str = ""
    extra: str = ""
    pool: str = ""


def write_llama_runner(args: list[str], extra_env: Optional[dict] = None) -> None:
    b = llama_bin()
    if not b:
        raise HTTPException(400, "llama.cpp is not installed (re-run install_app.sh)")
    script = "#!/bin/sh\n# generated by AI Panel - do not edit, use the web UI\n"
    script += f"export LD_LIBRARY_PATH={shlex.quote(str(Path(b).parent))}:$LD_LIBRARY_PATH\n"
    for k, v in (extra_env or {}).items():
        script += f"export {k}={shlex.quote(v)}\n"
    script += "exec " + " ".join(shlex.quote(x) for x in [b] + args) + "\n"
    LLAMA_RUN.write_text(script)
    os.chmod(LLAMA_RUN, 0o755)


@app.post("/api/llama/config")
async def llama_config(body: LlamaConfig):
    if body.split_mode not in ("layer", "row", "tensor", "none"):
        raise HTTPException(400, "split_mode must be layer, row, tensor or none")
    if body.tensor_split not in ("auto", "fit") and body.tensor_split and not re.match(r"^[0-9.]+(,[0-9.]+)*$", body.tensor_split):
        raise HTTPException(400, "tensor_split must be 'auto', 'fit' or numbers like 8,12,16")
    if body.devices and not re.match(r"^[A-Za-z]+\d+(,[A-Za-z]+\d+)*$", body.devices):
        raise HTTPException(400, "devices must look like Vulkan0,Vulkan1")
    if body.host not in ("127.0.0.1", "0.0.0.0"):
        raise HTTPException(400, "host must be 127.0.0.1 or 0.0.0.0")
    if body.pool and body.pool not in load_config().get("pools", {}):
        raise HTTPException(400, "Unknown GPU pool")
    cfg = load_config()
    cfg["llama"].update(body.model_dump())
    save_config(cfg)
    return {"ok": True}


@app.post("/api/llama/service/{action}")
async def llama_action(action: str):
    if action not in ("start", "stop", "restart"):
        raise HTTPException(404)
    if action in ("start", "restart"):
        cfg = load_config()
        penv = pool_vk_env(cfg["llama"].get("pool", ""))
        devs = await asyncio.to_thread(llama_devices, penv)
        args = build_llama_args(cfg, devs)
        write_llama_runner(args, penv)
        action = "restart"
    rc, out = run_quick(["systemctl", action, "ai-llama.service"], timeout=30)
    if rc != 0:
        raise HTTPException(500, out)
    return {"ok": True}


@app.get("/api/llama/logs")
async def llama_logs():
    rc, out = run_quick(["journalctl", "-u", "ai-llama.service", "-n", "200", "--no-pager", "-o", "cat"], timeout=10)
    return {"log": out}


@app.get("/api/hf/files")
async def hf_files(repo: str):
    if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
        raise HTTPException(400, "Use the form owner/repo, e.g. unsloth/Qwen3-30B-A3B-GGUF")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(f"https://huggingface.co/api/models/{repo}/tree/main", params={"recursive": "true"})
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Hugging Face: {r.text[:200]}")
    files = [{"path": f["path"], "size": f.get("lfs", {}).get("size") or f.get("size", 0)}
             for f in r.json() if f.get("type") == "file" and f["path"].endswith(".gguf")]
    # group split shards
    grouped: dict[str, dict] = {}
    for f in files:
        m = re.match(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", f["path"])
        key = m.group(1) if m else f["path"]
        g = grouped.setdefault(key, {"path": f["path"] if not m else key + "-00001-of-" + m.group(3) + ".gguf",
                                     "size": 0, "parts": []})
        g["size"] += f["size"]
        g["parts"].append(f["path"])
    return sorted(grouped.values(), key=lambda x: x["path"])


class HFDownload(BaseModel):
    repo: str
    path: str


@app.post("/api/llama/download")
async def llama_download(body: HFDownload):
    if not re.match(r"^[\w.\-]+/[\w.\-]+$", body.repo) or ".." in body.path or body.path.startswith("/"):
        raise HTTPException(400, "Bad repo or path")
    files = await hf_files(body.repo)
    group = next((g for g in files if g["path"] == body.path or body.path in g["parts"]), None)
    if not group:
        raise HTTPException(404, "File not found in repo")
    t = new_task("download", f"Download {Path(group['path']).name}")
    t["progress"] = 0
    t["total"] = group["size"]
    t["completed"] = 0
    dest_dir = MODELS_DIR / body.repo.split("/")[1]
    dest_dir.mkdir(parents=True, exist_ok=True)

    async def run():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), follow_redirects=True) as c:
                for part in sorted(group["parts"]):
                    dest = dest_dir / Path(part).name
                    tmp = dest.with_suffix(dest.suffix + ".part")
                    done = tmp.stat().st_size if tmp.exists() else 0
                    headers = {"Range": f"bytes={done}-"} if done else {}
                    task_log(t, f"Downloading {part}" + (" (resuming)" if done else ""))
                    url = f"https://huggingface.co/{body.repo}/resolve/main/{part}?download=true"
                    async with c.stream("GET", url, headers=headers) as r:
                        if r.status_code == 200 and done:
                            done = 0  # server ignored Range
                            tmp.unlink(missing_ok=True)
                        if r.status_code not in (200, 206):
                            raise RuntimeError(f"HTTP {r.status_code}")
                        t["completed"] += done
                        with open(tmp, "ab") as f:
                            async for chunk in r.aiter_bytes(1 << 20):
                                f.write(chunk)
                                t["completed"] += len(chunk)
                                t["progress"] = round(100 * t["completed"] / max(t["total"], 1), 1)
                    tmp.replace(dest)
            task_done(t, True, f"Saved to {dest_dir}")
        except Exception as e:
            task_done(t, False, f"Download failed: {e} (click download again to resume)")
    asyncio.create_task(run())
    return {"task": t["id"]}


class UrlDownload(BaseModel):
    url: str
    filename: str = ""


SAFE_NAME = re.compile(r"^[\w.\-]+\.gguf$")


@app.post("/api/llama/download-url")
async def llama_download_url(body: UrlDownload):
    """Download a GGUF from any direct https link (HF, ModelScope, your own server...)."""
    if not re.match(r"^https?://[^\s]+$", body.url):
        raise HTTPException(400, "Enter a direct http(s) link to a .gguf file")
    name = body.filename or body.url.split("?")[0].rstrip("/").split("/")[-1]
    if not SAFE_NAME.match(name):
        raise HTTPException(400, "File name must end with .gguf (letters, numbers, . - _ only)")
    dest_dir = MODELS_DIR / "custom"
    dest_dir.mkdir(parents=True, exist_ok=True)
    t = new_task("download", f"Download {name}")
    t["progress"] = 0

    async def run():
        dest = dest_dir / name
        tmp = dest.with_suffix(".gguf.part")
        try:
            done = tmp.stat().st_size if tmp.exists() else 0
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), follow_redirects=True) as c:
                async with c.stream("GET", body.url, headers={"Range": f"bytes={done}-"} if done else {}) as r:
                    if r.status_code == 200:
                        done = 0
                        tmp.unlink(missing_ok=True)
                    elif r.status_code != 206:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    size = int(r.headers.get("content-length", 0)) + done
                    t["total"], t["completed"] = size or None, done
                    with open(tmp, "ab") as f:
                        async for chunk in r.aiter_bytes(1 << 20):
                            f.write(chunk)
                            t["completed"] += len(chunk)
                            if size:
                                t["progress"] = round(100 * t["completed"] / size, 1)
            tmp.replace(dest)
            os.chmod(dest, 0o644)
            task_done(t, True, f"Saved to {dest}")
        except Exception as e:
            task_done(t, False, f"Download failed: {e} (try again to resume)")
    asyncio.create_task(run())
    return {"task": t["id"]}


@app.put("/api/llama/upload")
async def llama_upload(request: Request, filename: str):
    """Upload a local GGUF straight from the browser (streamed, no size limit besides disk)."""
    if not SAFE_NAME.match(filename):
        raise HTTPException(400, "File name must end with .gguf (letters, numbers, . - _ only)")
    dest_dir = MODELS_DIR / "custom"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    tmp = dest.with_suffix(".gguf.upload")
    n = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                n += len(chunk)
        tmp.replace(dest)
        os.chmod(dest, 0o644)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"Upload failed: {e}")
    return {"ok": True, "path": str(dest.relative_to(MODELS_DIR)), "bytes": n}


class OllamaImport(BaseModel):
    gguf: str
    name: str


@app.post("/api/ollama/import")
async def ollama_import(body: OllamaImport):
    """Create an Ollama model from a local GGUF in the models folder."""
    p = (MODELS_DIR / body.gguf).resolve()
    if not str(p).startswith(str(MODELS_DIR.resolve()) + "/") or not p.exists():
        raise HTTPException(404, "GGUF not found")
    if not MODEL_RE.match(body.name):
        raise HTTPException(400, "Invalid model name")
    t = new_task("pull", f"Import {body.name} into Ollama")
    mf = HOME / "tmp" / f"Modelfile.{t['id']}"
    mf.parent.mkdir(exist_ok=True)
    mf.write_text(f"FROM {p}\n")

    async def run():
        rc = await run_cmd(t, ["ollama", "create", body.name, "-f", str(mf)])
        mf.unlink(missing_ok=True)
        task_done(t, rc == 0, f"{body.name} is ready in Ollama" if rc == 0 else "Import failed")
    asyncio.create_task(run())
    return {"task": t["id"]}


@app.post("/api/llama/delete-model")
async def llama_delete_model(body: ModelName):
    p = (MODELS_DIR / body.name).resolve()
    if not str(p).startswith(str(MODELS_DIR.resolve()) + "/") or not p.exists():
        raise HTTPException(404, "Not found")
    m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", p.name)
    targets = list(p.parent.glob(re.sub(r"-\d{5}-of-", "-*-of-", p.name))) if m else [p]
    for x in targets:
        x.unlink(missing_ok=True)
    return {"ok": True, "deleted": len(targets)}


# --------------------------------------------------------------------------------------
# Docker App Store
# --------------------------------------------------------------------------------------
APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,40}$")


def load_catalog() -> list[dict]:
    apps = []
    try:
        apps = json.loads(CATALOG_FILE.read_text()).get("apps", [])
    except Exception:
        pass
    try:
        for a in json.loads(CUSTOM_FILE.read_text()):
            a["custom"] = True
            apps.append(a)
    except Exception:
        pass
    return apps


def get_app(app_id: str) -> dict:
    for a in load_catalog():
        if a["id"] == app_id:
            return a
    # installed app removed from catalog: still manageable from its folder
    d = APPS_DIR / app_id
    if (d / "docker-compose.yml").exists():
        meta = d / "app.json"
        if meta.exists():
            return json.loads(meta.read_text())
    raise HTTPException(404, "Unknown app")


def docker_ok() -> bool:
    return shutil.which("docker") is not None


def compose_cmd(app_id: str) -> list[str]:
    return ["docker", "compose", "-p", PROJECT_PREFIX + app_id, "-f", str(APPS_DIR / app_id / "docker-compose.yml"),
            "--env-file", str(APPS_DIR / app_id / ".env")]


def app_states() -> dict[str, dict]:
    """Map app_id -> {running, total, containers}. One docker call for everything."""
    if not docker_ok():
        return {}
    rc, out = run_quick(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project",
                         "--format", "{{json .}}"], timeout=15)
    states: dict[str, dict] = {}
    if rc != 0:
        return states
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except Exception:
            continue
        labels = dict(kv.split("=", 1) for kv in c.get("Labels", "").split(",") if "=" in kv)
        proj = labels.get("com.docker.compose.project", "")
        if not proj.startswith(PROJECT_PREFIX):
            continue
        aid = proj[len(PROJECT_PREFIX):]
        s = states.setdefault(aid, {"running": 0, "total": 0, "containers": []})
        s["total"] += 1
        if c.get("State") == "running":
            s["running"] += 1
        s["containers"].append({"name": c.get("Names"), "state": c.get("State"), "status": c.get("Status"),
                                "image": c.get("Image")})
    return states


@app.get("/api/apps")
async def list_apps():
    catalog = load_catalog()
    states = await asyncio.to_thread(app_states)
    seen = set()
    out = []
    for a in catalog:
        seen.add(a["id"])
        installed = (APPS_DIR / a["id"] / "docker-compose.yml").exists()
        st = states.get(a["id"])
        out.append(_app_view(a, installed, st))
    # installed apps no longer in catalog
    for d in APPS_DIR.iterdir() if APPS_DIR.exists() else []:
        if d.name not in seen and (d / "app.json").exists():
            a = json.loads((d / "app.json").read_text())
            out.append(_app_view(a, True, states.get(d.name)))
    return {"apps": out, "docker": docker_ok(), "gpu_vendors": sorted(gpu_vendors())}


def _app_view(a: dict, installed: bool, st: Optional[dict]) -> dict:
    port = a.get("port")
    env_file = APPS_DIR / a["id"] / ".env"
    if installed and env_file.exists():
        env = parse_env(env_file.read_text())
        if env.get("APP_PORT"):
            port = int(env["APP_PORT"])
    gpu_pool = ""
    if installed and env_file.exists():
        gpu_pool = parse_env(env_file.read_text()).get("GPU_POOL", "")
    status = "not_installed"
    if installed:
        if not st or st["total"] == 0:
            status = "stopped"
        elif st["running"] == st["total"]:
            status = "running"
        elif st["running"] == 0:
            status = "stopped"
        else:
            status = "partial"
    return {"id": a["id"], "name": a.get("name", a["id"]), "description": a.get("description", ""),
            "category": a.get("category", "Other"), "icon": a.get("icon", ""), "port": port,
            "scheme": a.get("scheme", "http"), "path": a.get("path", "/"), "gpu": bool(a.get("gpu")),
            "inputs": a.get("inputs", []), "notes": a.get("notes", ""), "custom": a.get("custom", False),
            "website": a.get("website", ""), "installed": installed, "status": status, "gpu_pool": gpu_pool,
            "containers": (st or {}).get("containers", [])}


def parse_env(text: str) -> dict:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        env[k.strip()] = v
    return env


def port_free(port: int) -> bool:
    for c in psutil.net_connections(kind="inet"):
        if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
            return False
    return True


def apply_gpu(compose: dict, a: dict, gpu_mode: str, sel: Optional[list[dict]] = None) -> None:
    """sel = GPUs of the chosen pool (None = all GPUs)."""
    gpu = a.get("gpu") or {}
    if not gpu or gpu_mode == "none":
        return
    nv_ids = [g["uuid"] for g in (sel or []) if g["vendor"] == "NVIDIA" and g.get("uuid")] if sel is not None else None
    amd_nodes = None
    if sel is not None:
        amd_nodes = []
        for g in sel:
            if g["vendor"] == "AMD":
                amd_nodes += [x for x in (g.get("render"), g.get("card")) if x]
    services = gpu.get("services") or list(compose.get("services", {}).keys())
    for name in services:
        svc = compose["services"].get(name)
        if svc is None:
            continue
        img = (gpu.get("images") or {}).get(gpu_mode)
        if img:
            svc["image"] = img
        if gpu_mode == "nvidia":
            dev = {"driver": "nvidia", "capabilities": ["gpu"]}
            if nv_ids is None:
                dev["count"] = "all"
            else:
                dev["device_ids"] = nv_ids
            svc.setdefault("deploy", {}).setdefault("resources", {}).setdefault("reservations", {})["devices"] = [dev]
            vis = "all" if nv_ids is None else ",".join(nv_ids)
            env = svc.setdefault("environment", {})
            if isinstance(env, dict):
                env["NVIDIA_VISIBLE_DEVICES"] = vis
                env.setdefault("NVIDIA_DRIVER_CAPABILITIES", "all")
            else:
                env += [f"NVIDIA_VISIBLE_DEVICES={vis}", "NVIDIA_DRIVER_CAPABILITIES=all"]
        elif gpu_mode == "amd":
            devs = svc.setdefault("devices", [])
            wanted = ["/dev/kfd"] + (["/dev/dri"] if amd_nodes is None else amd_nodes)
            for d in wanted:
                if os.path.exists(d) and d not in devs:
                    devs.append(d)
            ga = svc.setdefault("group_add", [])
            for g in ("video", "render"):
                if g not in ga:
                    ga.append(g)
            svc.setdefault("security_opt", []).append("seccomp=unconfined")
        extra_env = (gpu.get("env") or {}).get(gpu_mode)
        if extra_env:
            env = svc.setdefault("environment", {})
            if isinstance(env, dict):
                env.update(extra_env)
            else:
                env += [f"{k}={v}" for k, v in extra_env.items()]


def prepare_data_dirs(a: dict) -> None:
    """Pre-create volume folders owned by the container user (e.g. node=1000) so apps can write."""
    spec = a.get("chown") or {}
    uid = int(spec.get("uid", 0))
    for sub in spec.get("dirs", []):
        d = DATA_DIR / a["id"] / sub
        d.mkdir(parents=True, exist_ok=True)
        if uid:
            for root, dirs, files in os.walk(d):
                os.chown(root, uid, uid)
                for f in files:
                    try:
                        os.chown(os.path.join(root, f), uid, uid)
                    except OSError:
                        pass


class InstallBody(BaseModel):
    inputs: dict[str, str] = {}
    port: Optional[int] = None
    gpu: str = "auto"
    pool: str = ""


def render_app(a: dict, body: InstallBody) -> tuple[str, str]:
    """Returns (compose_yaml, env_text)."""
    app_dir = APPS_DIR / a["id"]
    env_path = app_dir / ".env"
    old = parse_env(env_path.read_text()) if env_path.exists() else {}
    env: dict[str, str] = {}
    port = body.port or (int(old["APP_PORT"]) if old.get("APP_PORT") else a.get("port"))
    if port:
        env["APP_PORT"] = str(port)
    env["DATA_DIR"] = str(DATA_DIR / a["id"])
    env["TZ"] = old.get("TZ") or _timezone()
    env["HOST_IP"] = _host_ip()
    for k, spec in (a.get("env") or {}).items():
        if k in old:
            env[k] = old[k]  # keep generated secrets stable across updates
        elif spec == "{{random}}":
            env[k] = secrets.token_urlsafe(18).replace("-", "a").replace("_", "b")
        elif spec == "{{random_hex}}":
            env[k] = secrets.token_hex(32)
        else:
            env[k] = str(spec).replace("{{host_ip}}", env["HOST_IP"]).replace("{{port}}", str(port or ""))
    for inp in a.get("inputs", []):
        k = inp["key"]
        v = body.inputs.get(k, old.get(k, inp.get("default", "")))
        if inp.get("required") and not v:
            raise HTTPException(400, f"{inp.get('label', k)} is required")
        if "\n" in v:
            raise HTTPException(400, f"{k} must be a single line")
        env[k] = v
    compose_text = a.get("compose", "")
    if not compose_text:
        raise HTTPException(400, "App has no compose definition")
    try:
        compose = yaml.safe_load(compose_text)
        assert isinstance(compose, dict) and isinstance(compose.get("services"), dict)
    except Exception as e:
        raise HTTPException(400, f"Invalid compose YAML: {e}")
    gpu_mode = body.gpu
    pool = body.pool if body.pool is not None else ""
    if a.get("gpu"):
        sel = pool_gpus(pool) if pool else None
        if sel is not None:
            vendors = {g["vendor"].lower() for g in sel}
        else:
            vendors = gpu_vendors()
        if gpu_mode == "auto":
            gpu_mode = "nvidia" if "nvidia" in vendors else ("amd" if "amd" in vendors else "none")
        if sel is not None and gpu_mode in ("nvidia", "amd") and gpu_mode not in vendors:
            raise HTTPException(400, f"GPU pool '{pool}' has no {gpu_mode.upper()} GPU")
        apply_gpu(compose, a, gpu_mode, sel)
    env["GPU_MODE"] = gpu_mode
    env["GPU_POOL"] = pool
    env_text = "".join(f"{k}={_env_quote(v)}\n" for k, v in env.items())
    return yaml.safe_dump(compose, sort_keys=False), env_text


def _env_quote(v: str) -> str:
    if re.match(r"^[A-Za-z0-9_./:@%+,=\-]*$", v):
        return v
    return "'" + v.replace("'", "") + "'"


def _timezone() -> str:
    tz = _read("/etc/timezone")
    if tz:
        return tz
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/")[1]
    except Exception:
        return "UTC"


def _host_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.post("/api/apps/{app_id}/install")
async def install_app(app_id: str, body: InstallBody):
    if not docker_ok():
        raise HTTPException(400, "Docker is not installed")
    a = get_app(app_id)
    app_dir = APPS_DIR / app_id
    if body.port and not (1 <= body.port <= 65535):
        raise HTTPException(400, "Bad port")
    reinstall = (app_dir / "docker-compose.yml").exists()
    port = body.port or a.get("port")
    if port and not reinstall and not port_free(int(port)):
        raise HTTPException(409, f"Port {port} is already in use. Choose another port.")
    compose_text, env_text = render_app(a, body)
    app_dir.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / app_id).mkdir(parents=True, exist_ok=True)
    prepare_data_dirs(a)
    (app_dir / "docker-compose.yml").write_text(compose_text)
    (app_dir / ".env").write_text(env_text)
    os.chmod(app_dir / ".env", 0o600)
    (app_dir / "app.json").write_text(json.dumps(a, indent=2))
    t = new_task("app", f"Install {a.get('name', app_id)}")

    async def run():
        task_log(t, "Pulling images (this can take a while)...")
        rc = await run_cmd(t, compose_cmd(app_id) + ["pull"])
        if rc != 0:
            task_log(t, "Pull reported errors, trying to start anyway")
        rc = await run_cmd(t, compose_cmd(app_id) + ["up", "-d", "--remove-orphans"])
        task_done(t, rc == 0, "Installed and started" if rc == 0 else "Install failed - see log")
    asyncio.create_task(run())
    return {"task": t["id"]}


ACTIONS = {
    "start": [["up", "-d", "--remove-orphans"]],
    "stop": [["stop"]],
    "restart": [["restart"]],
    "update": [["pull"], ["up", "-d", "--remove-orphans"]],
}


@app.post("/api/apps/{app_id}/{action}")
async def app_action(app_id: str, action: str, purge: bool = False):
    if not APP_ID_RE.match(app_id):
        raise HTTPException(400, "Bad app id")
    app_dir = APPS_DIR / app_id
    if not (app_dir / "docker-compose.yml").exists():
        raise HTTPException(404, "App is not installed")
    if action == "uninstall":
        t = new_task("app", f"Uninstall {app_id}")

        async def run_un():
            rc = await run_cmd(t, compose_cmd(app_id) + ["down", "--remove-orphans"] + (["-v"] if purge else []))
            if rc == 0:
                shutil.rmtree(app_dir, ignore_errors=True)
                if purge:
                    shutil.rmtree(DATA_DIR / app_id, ignore_errors=True)
                    task_log(t, "App data deleted")
                else:
                    task_log(t, f"App data kept in {DATA_DIR / app_id}")
            task_done(t, rc == 0, "Uninstalled" if rc == 0 else "Uninstall failed")
        asyncio.create_task(run_un())
        return {"task": t["id"]}
    if action not in ACTIONS:
        raise HTTPException(404, "Unknown action")
    t = new_task("app", f"{action.capitalize()} {app_id}")

    async def run():
        rc = 0
        for step in ACTIONS[action]:
            rc = await run_cmd(t, compose_cmd(app_id) + step)
            if rc != 0:
                break
        if action == "update" and rc == 0:
            await run_cmd(t, ["docker", "image", "prune", "-f"])
        task_done(t, rc == 0, "Done" if rc == 0 else "Failed - see log")
    asyncio.create_task(run())
    return {"task": t["id"]}


@app.post("/api/apps-update-all")
async def apps_update_all():
    installed = [d.name for d in APPS_DIR.iterdir() if (d / "docker-compose.yml").exists()]
    t = new_task("app", f"Update all apps ({len(installed)})")
    t["progress"] = 0

    async def run():
        failed = []
        for i, aid in enumerate(installed):
            task_log(t, f"== [{i + 1}/{len(installed)}] {aid} ==")
            # refresh compose from the latest catalog (keeps .env secrets and port)
            try:
                a = next((x for x in load_catalog() if x["id"] == aid), None)
                if a:
                    old = parse_env((APPS_DIR / aid / ".env").read_text())
                    c, e = render_app(a, InstallBody(gpu=old.get("GPU_MODE", "auto"), pool=old.get("GPU_POOL", "")))
                    (APPS_DIR / aid / "docker-compose.yml").write_text(c)
                    (APPS_DIR / aid / ".env").write_text(e)
                    (APPS_DIR / aid / "app.json").write_text(json.dumps(a, indent=2))
            except Exception as ex:
                task_log(t, f"(kept old compose: {ex})")
            rc = await run_cmd(t, compose_cmd(aid) + ["pull"])
            if rc == 0:
                rc = await run_cmd(t, compose_cmd(aid) + ["up", "-d", "--remove-orphans"])
            if rc != 0:
                failed.append(aid)
            t["progress"] = round(100 * (i + 1) / max(len(installed), 1))
        await run_cmd(t, ["docker", "image", "prune", "-f"])
        task_done(t, not failed, "All apps updated" if not failed else f"Failed: {', '.join(failed)}")
    asyncio.create_task(run())
    return {"task": t["id"]}


@app.get("/api/apps/{app_id}/logs")
async def app_logs(app_id: str, lines: int = 200):
    if not APP_ID_RE.match(app_id) or not (APPS_DIR / app_id / "docker-compose.yml").exists():
        raise HTTPException(404)
    proc = await asyncio.create_subprocess_exec(*compose_cmd(app_id), "logs", "--no-color", "--tail", str(min(lines, 2000)),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return {"log": out.decode(errors="replace")}


@app.get("/api/apps/{app_id}/secrets")
async def app_secrets(app_id: str):
    """Generated passwords/tokens for an installed app (shown on demand in the UI)."""
    if not APP_ID_RE.match(app_id):
        raise HTTPException(400)
    p = APPS_DIR / app_id / ".env"
    if not p.exists():
        raise HTTPException(404, "App is not installed")
    hidden = {"DATA_DIR", "TZ", "HOST_IP", "GPU_MODE", "GPU_POOL"}
    return {k: v for k, v in parse_env(p.read_text()).items() if k not in hidden}


@app.get("/api/apps/{app_id}/compose")
async def app_compose(app_id: str):
    if not APP_ID_RE.match(app_id):
        raise HTTPException(400)
    p = APPS_DIR / app_id / "docker-compose.yml"
    if p.exists():
        return {"compose": p.read_text(), "installed": True}
    return {"compose": get_app(app_id).get("compose", ""), "installed": False}


class CustomApp(BaseModel):
    id: str
    name: str
    port: Optional[int] = None
    path: str = "/"
    scheme: str = "http"
    description: str = ""
    category: str = "Custom"
    compose: str


@app.post("/api/apps-custom")
async def add_custom(body: CustomApp):
    if not APP_ID_RE.match(body.id):
        raise HTTPException(400, "ID: lowercase letters, numbers and dashes (2-41 chars)")
    try:
        c = yaml.safe_load(body.compose)
        assert isinstance(c, dict) and isinstance(c.get("services"), dict) and c["services"]
    except Exception:
        raise HTTPException(400, "Compose YAML must contain a 'services:' section")
    catalog_ids = {a["id"] for a in load_catalog() if not a.get("custom")}
    if body.id in catalog_ids:
        raise HTTPException(409, "That ID is used by a built-in app")
    try:
        customs = json.loads(CUSTOM_FILE.read_text())
    except Exception:
        customs = []
    customs = [x for x in customs if x["id"] != body.id]
    entry = body.model_dump()
    entry["icon"] = "📦"
    customs.append(entry)
    CUSTOM_FILE.write_text(json.dumps(customs, indent=2))
    return {"ok": True}


@app.get("/api/apps-custom/fetch")
async def fetch_compose(url: str):
    """Load a compose file from an online URL (GitHub raw, gist, your server...)."""
    if not re.match(r"^https?://[^\s]+$", url):
        raise HTTPException(400, "Enter an http(s) URL")
    # GitHub "blob" page -> raw file
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    if m:
        url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url)
    if r.status_code != 200:
        raise HTTPException(502, f"HTTP {r.status_code} from {url}")
    if len(r.content) > 512_000:
        raise HTTPException(400, "File too large for a compose file")
    text = r.text
    try:
        c = yaml.safe_load(text)
        assert isinstance(c, dict) and isinstance(c.get("services"), dict)
    except Exception:
        raise HTTPException(400, "That URL does not contain a Docker Compose file with 'services:'")
    ports = []
    for svc in c["services"].values():
        for pspec in (svc or {}).get("ports", []) or []:
            mm = re.match(r"^(?:[\d.]+:)?\$?\{?[A-Z_]*:?-?(\d+)\}?:\d+", str(pspec))
            if mm:
                ports.append(int(mm.group(1)))
    return {"compose": text, "suggested_port": ports[0] if ports else None,
            "suggested_id": re.sub(r"[^a-z0-9\-]", "-", list(c["services"].keys())[0].lower())[:40]}


@app.delete("/api/apps-custom/{app_id}")
async def delete_custom(app_id: str):
    if (APPS_DIR / app_id / "docker-compose.yml").exists():
        raise HTTPException(400, "Uninstall the app first")
    try:
        customs = json.loads(CUSTOM_FILE.read_text())
    except Exception:
        customs = []
    CUSTOM_FILE.write_text(json.dumps([x for x in customs if x["id"] != app_id], indent=2))
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Updates: catalog refresh + panel self-update
# --------------------------------------------------------------------------------------
STATIC_FILES = ["static/tailwind.css", "static/xterm.css", "static/xterm.js", "static/addon-fit.js"]


class UpdateUrl(BaseModel):
    url: str


@app.get("/api/system/info")
async def system_info():
    cfg = load_config()
    rc, ov = run_quick(["ollama", "--version"])
    rc2, dv = run_quick(["docker", "--version"])
    return {"version": VERSION, "update_url": cfg.get("update_url", ""),
            "ollama": ov.strip().splitlines()[-1] if rc == 0 and ov.strip() else None,
            "docker": dv.strip() if rc2 == 0 else None,
            "os": (_read("/etc/os-release") or "").split('PRETTY_NAME="')[-1].split('"')[0]}


@app.post("/api/system/update-url")
async def set_update_url(body: UpdateUrl):
    if body.url and not re.match(r"^https://[^\s]+$", body.url):
        raise HTTPException(400, "Must be an https:// URL (base folder holding app.py, index.html, apps_catalog.json)")
    cfg = load_config()
    cfg["update_url"] = body.url.rstrip("/")
    save_config(cfg)
    return {"ok": True}


async def _fetch_release_files(names: list[str]) -> dict[str, bytes]:
    base = load_config().get("update_url", "")
    if not base:
        raise HTTPException(400, "Set an update URL in Settings first")
    out = {}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        for n in names:
            r = await c.get(f"{base}/{n}", params={"t": int(time.time())})
            if r.status_code != 200:
                raise HTTPException(502, f"Could not download {n}: HTTP {r.status_code}")
            out[n] = r.content
    return out


@app.post("/api/system/update-catalog")
async def update_catalog():
    files = await _fetch_release_files(["apps_catalog.json"])
    try:
        data = json.loads(files["apps_catalog.json"])
        assert isinstance(data.get("apps"), list)
    except Exception:
        raise HTTPException(400, "Downloaded catalog is not valid")
    shutil.copy(CATALOG_FILE, CATALOG_FILE.with_suffix(".json.bak")) if CATALOG_FILE.exists() else None
    CATALOG_FILE.write_bytes(files["apps_catalog.json"])
    return {"ok": True, "apps": len(data["apps"])}


@app.post("/api/system/update-panel")
async def update_panel():
    files = await _fetch_release_files(["app.py", "index.html", "apps_catalog.json", "install_app.sh"] + STATIC_FILES)
    try:
        compile(files["app.py"], "app.py", "exec")
        json.loads(files["apps_catalog.json"])
    except Exception as e:
        raise HTTPException(400, f"Downloaded files failed validation, nothing changed: {e}")
    backup = HOME / "backup" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    for n, data in files.items():
        (backup / n).parent.mkdir(parents=True, exist_ok=True)
        if (HOME / n).exists():
            shutil.copy(HOME / n, backup / n)
        (HOME / n).write_bytes(data)
    # restart ourselves after the response is sent
    asyncio.get_running_loop().call_later(1.5, lambda: subprocess.Popen(["systemctl", "restart", "ai-panel.service"]))
    return {"ok": True, "backup": str(backup)}


@app.post("/api/system/update-engines")
async def update_engines():
    """Re-run the installer's engine-update step (Ollama + llama.cpp)."""
    script = HOME / "install_app.sh"
    if not script.exists():
        raise HTTPException(400, "install_app.sh not found in " + str(HOME))
    t = new_task("system", "Update Ollama + llama.cpp")
    t["progress"] = None

    async def run():
        rc = await run_cmd(t, ["bash", str(script), "--update-engines"])
        task_done(t, rc == 0, "Engines updated" if rc == 0 else "Update failed")
    asyncio.create_task(run())
    return {"task": t["id"]}


# --------------------------------------------------------------------------------------
# Web terminal (xterm.js <-> PTY over WebSocket)
# --------------------------------------------------------------------------------------
@app.websocket("/ws/terminal")
async def terminal(ws: WebSocket):
    user = check_token(ws.cookies.get(COOKIE))
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if not user or (origin and origin.split("://", 1)[-1] != host):
        await ws.close(code=4401)
        return
    await ws.accept()
    pid, fd = pty.fork()
    if pid == 0:  # child
        env = {"TERM": "xterm-256color", "HOME": os.environ.get("HOME", "/root"), "LANG": "C.UTF-8",
               "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "SHELL": "/bin/bash",
               "USER": "root"}
        os.chdir(env["HOME"])
        os.execvpe("/bin/bash", ["bash", "-l"], env)
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def on_read():
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        q.put_nowait(data)
        if not data:
            loop.remove_reader(fd)

    loop.add_reader(fd, on_read)

    async def pump():
        while True:
            data = await q.get()
            if not data:
                await ws.close()
                break
            await ws.send_bytes(data)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is None and msg.get("bytes") is not None:
                os.write(fd, msg["bytes"])
                continue
            try:
                m = json.loads(text)
            except Exception:
                continue
            if m.get("type") == "input":
                os.write(fd, m["data"].encode())
            elif m.get("type") == "resize":
                rows, cols = int(m.get("rows", 24)), int(m.get("cols", 80))
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        pump_task.cancel()
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            os.kill(pid, signal.SIGHUP)
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        await asyncio.to_thread(_reap, pid)


def _reap(pid: int) -> None:
    for _ in range(20):
        try:
            p, _ = os.waitpid(pid, os.WNOHANG)
            if p:
                return
        except ChildProcessError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# CLI helpers used by the installer
# --------------------------------------------------------------------------------------
def cli() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "--set-password":
        cfg = load_config()
        cfg["users"][sys.argv[2]] = hash_password(sys.argv[3])
        save_config(cfg)
        print(f"Password set for user '{sys.argv[2]}'")
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--has-users":
        sys.exit(0 if load_config()["users"] else 1)
    import uvicorn
    load_config()
    uvicorn.run(app, host=os.environ.get("AIPANEL_HOST", "0.0.0.0"), port=PORT, log_level="info",
                proxy_headers=True, ws_ping_interval=20)


if __name__ == "__main__":
    cli()
