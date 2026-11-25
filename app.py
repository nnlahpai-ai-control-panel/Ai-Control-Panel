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
import tempfile
import termios
import threading
import time
import uuid
import contextvars
from pathlib import Path
from typing import Any, Optional

import httpx
import psutil
import yaml
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

VERSION = "1.6.0"
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


def user_role(cfg: dict, user: str) -> str:
    """Users without a roles entry are admins (keeps old single-admin installs working)."""
    return (cfg.get("roles", {}).get(user) or {}).get("role", "admin")


def user_apps(cfg: dict, user: str) -> set[str]:
    return set((cfg.get("roles", {}).get(user) or {}).get("apps", []))


CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar("CURRENT_USER", default="")
USER_ROUTES = [
    ("GET", re.compile(r"^/api/(me|apps|tasks|tasks/[0-9a-f]+)$"), None),
    ("POST", re.compile(r"^/api/(logout|change-password)$"), None),
    ("POST", re.compile(r"^/api/apps/([a-z0-9\-]+)/(start|stop|restart|update)$"), 1),
    ("GET", re.compile(r"^/api/apps/([a-z0-9\-]+)/(logs|secrets)$"), 1),
    ("GET", re.compile(r"^/api/logs/(sources|stream)$"), None),  # stream checks the app itself
]


def user_may(method: str, path: str, apps: set[str]) -> bool:
    for m, rx, grp in USER_ROUTES:
        mm = rx.match(path)
        if m == method and mm:
            return grp is None or mm.group(grp) in apps
    return False


# --------------------------------------------------------------------------------------
# Background tasks (downloads, docker actions) with progress
# --------------------------------------------------------------------------------------
TASKS: dict[str, dict] = {}


def new_task(kind: str, title: str) -> dict:
    tid = uuid.uuid4().hex[:12]
    t = {"id": tid, "kind": kind, "title": title, "status": "running", "progress": None,
         "message": "Starting...", "log": [], "started": time.time(), "finished": None,
         "total": None, "completed": None, "owner": CURRENT_USER.get()}
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
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(sampler_loop()), asyncio.create_task(telegram_loop())]  # defined below
    try:
        msgs = await asyncio.to_thread(apply_gpu_power, load_config())  # power limits reset at reboot
        if msgs:
            print("GPU power:", "; ".join(msgs), file=sys.stderr)
    except Exception as e:
        print("GPU power:", e, file=sys.stderr)
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="AI Server Control Panel", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)

@app.exception_handler(httpx.HTTPError)
async def httpx_error(request: Request, exc: httpx.HTTPError):
    return JSONResponse({"detail": f"Network error: {exc}"}, status_code=502)


PUBLIC_PATHS = {"/", "/index.html", "/api/login", "/api/health", "/favicon.ico", "/manifest.webmanifest", "/icon.svg"}


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
    cfg = load_config()
    request.state.user = user
    request.state.role = user_role(cfg, user)
    request.state.apps = user_apps(cfg, user)
    if request.state.role != "admin" and not user_may(request.method, path, request.state.apps):
        return JSONResponse({"detail": "Only an admin can do this"}, status_code=403)
    CURRENT_USER.set(user)
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
    return {"user": request.state.user, "role": request.state.role, "apps": sorted(request.state.apps),
            "version": VERSION, "hostname": socket.gethostname()}


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
async def list_tasks(request: Request):
    items = sorted(TASKS.values(), key=lambda x: x["started"], reverse=True)
    if request.state.role != "admin":
        items = [t for t in items if t.get("owner") == request.state.user]
    items = items[:50]
    return [{k: v for k, v in t.items() if k != "log"} for t in items]


@app.get("/api/tasks/{tid}")
async def get_task(tid: str, request: Request):
    t = TASKS.get(tid)
    if not t or (request.state.role != "admin" and t.get("owner") != request.state.user):
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


# --------------------------------------------------------------------------------------
# System monitor: background sampler (5 s) feeds live rates + 1 hour of history
# --------------------------------------------------------------------------------------
HISTORY_POINTS = 720  # 720 x 5 s = 1 hour
HISTORY: list[dict] = []
LIVE: dict = {"net": {}, "disk": {}, "gpus": [], "t": 0}
_prev: dict = {}
VIRTUAL_NICS = re.compile(r"^(lo|docker\d*|br-|veth|virbr|tailscale|zt|cni|flannel|vnet|ifb|tun|tap|wg)")


def _sample() -> None:
    now = time.time()
    dt = max(now - _prev.get("t", now - 5), 0.5)
    nics = psutil.net_io_counters(pernic=True)
    disks = psutil.disk_io_counters(perdisk=True) or {}
    net, dsk = {}, {}
    for name, c in nics.items():
        p = _prev.get("nic", {}).get(name)
        net[name] = {"rx_bps": (c.bytes_recv - p.bytes_recv) / dt if p else 0,
                     "tx_bps": (c.bytes_sent - p.bytes_sent) / dt if p else 0,
                     "rx_total": c.bytes_recv, "tx_total": c.bytes_sent}
    for name, c in disks.items():
        p = _prev.get("disk", {}).get(name)
        dsk[name] = {"read_bps": (c.read_bytes - p.read_bytes) / dt if p else 0,
                     "write_bps": (c.write_bytes - p.write_bytes) / dt if p else 0,
                     "busy": min(100.0, (getattr(c, "busy_time", 0) - getattr(p, "busy_time", 0)) / (dt * 10)) if p and hasattr(c, "busy_time") else None}
    _prev.update(t=now, nic=nics, disk=disks)
    gpus = all_gpus()
    LIVE.update(net=net, disk=dsk, gpus=gpus, t=now)
    _gpu_watch(gpus)
    phys_net = {k: v for k, v in net.items() if not VIRTUAL_NICS.match(k)}
    whole = {k: v for k, v in dsk.items() if _is_whole_disk(k)}
    vm = psutil.virtual_memory()
    HISTORY.append({
        "t": int(now),
        "cpu": psutil.cpu_percent(interval=None),
        "ram": vm.percent,
        "cpu_temp": cpu_temp(),
        "rx": sum(v["rx_bps"] for v in phys_net.values()),
        "tx": sum(v["tx_bps"] for v in phys_net.values()),
        "dr": sum(v["read_bps"] for v in whole.values()),
        "dw": sum(v["write_bps"] for v in whole.values()),
        "gpu": [g.get("util") for g in gpus],
        "vram": [round(100 * (g["vram_used_mb"] or 0) / g["vram_total_mb"], 1) if g.get("vram_total_mb") else None for g in gpus],
        "gtemp": [g.get("temp") for g in gpus],
        "gpw": round(sum((g.get("power_w") or 0) for g in gpus), 1),
    })
    if len(HISTORY) > HISTORY_POINTS:
        del HISTORY[:len(HISTORY) - HISTORY_POINTS]


def _is_whole_disk(name: str) -> bool:
    return os.path.exists(f"/sys/block/{name}") and not name.startswith(("loop", "ram", "zram", "dm-", "sr"))


async def sampler_loop() -> None:
    psutil.cpu_percent(interval=None)
    while True:
        try:
            await asyncio.to_thread(_sample)
            ev, GPU_WATCH["todo"] = GPU_WATCH["todo"], None
            if ev:
                asyncio.create_task(gpu_rebalance(ev))
            _alert_state["tick"] = _alert_state.get("tick", 0) + 1
            if _alert_state["tick"] % 12 == 0:
                asyncio.create_task(check_alerts())
        except Exception as e:  # never let monitoring kill the panel
            print("sampler:", e, file=sys.stderr)
        await asyncio.sleep(5)




def cpu_temp() -> Optional[float]:
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    for chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
        sensors = temps.get(chip, [])
        # prefer the package / die sensor (Intel "Package id 0", AMD "Tctl"/"Tdie")
        best = [t for t in sensors if (t.label or "").startswith(("Package", "Tctl", "Tdie"))] or sensors
        for t in best:
            if t.current:
                return round(t.current, 1)
    return None


def fans() -> list[dict]:
    try:
        return [{"name": f"{chip} {f.label or i}".strip(), "rpm": f.current}
                for chip, lst in psutil.sensors_fans().items() for i, f in enumerate(lst) if f.current]
    except Exception:
        return []


@app.get("/api/stats")
async def stats():
    if not LIVE["t"]:
        await asyncio.to_thread(_sample)
    gpus = [dict(g) for g in LIVE["gpus"]]
    pools = load_config().get("pools", {})
    for g in gpus:
        g["pools"] = [p.get("name", pid) for pid, p in pools.items() if g["bus"] in p.get("gpus", [])]
    total = sum(g["vram_total_mb"] or 0 for g in gpus)
    used = sum(g["vram_used_mb"] or 0 for g in gpus)
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    du = psutil.disk_usage("/")
    last = HISTORY[-1] if HISTORY else {}
    return {
        "gpus": gpus,
        "pool": {"total_mb": total, "used_mb": used, "count": len(gpus)},
        "cpu": {"percent": last.get("cpu", psutil.cpu_percent(interval=None)), "cores": psutil.cpu_count(),
                "load": os.getloadavg(), "model": _cpu_model(), "temp": cpu_temp(),
                "freq": round(psutil.cpu_freq().current) if psutil.cpu_freq() else None},
        "ram": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent,
                "cached": getattr(vm, "cached", 0), "swap_total": sw.total, "swap_used": sw.used, "swap_percent": sw.percent},
        "disk": {"total": du.total, "used": du.used, "percent": du.percent,
                 "read_bps": last.get("dr", 0), "write_bps": last.get("dw", 0)},
        "net": {"rx_bps": last.get("rx", 0), "tx_bps": last.get("tx", 0)},
        "fans": fans(),
        "uptime": int(time.time() - psutil.boot_time()),
        "hostname": socket.gethostname(),
    }


@app.get("/api/history")
async def history(points: int = 360):
    return {"interval": 5, "points": HISTORY[-max(10, min(points, HISTORY_POINTS)):]}


@app.get("/api/network")
async def network():
    addrs = psutil.net_if_addrs()
    st = psutil.net_if_stats()
    out = []
    for name, v in sorted(LIVE["net"].items()):
        s_ = st.get(name)
        ips = [a.address for a in addrs.get(name, []) if a.family == socket.AF_INET]
        mac = next((a.address for a in addrs.get(name, []) if a.family == psutil.AF_LINK), "")
        out.append({"name": name, "virtual": bool(VIRTUAL_NICS.match(name)), "up": bool(s_ and s_.isup),
                    "speed_mbps": s_.speed if s_ else 0, "ips": ips, "mac": mac, **v})
    return out


# ---- hardware inventory (auto-detected, cached 10 min) --------------------------------
_hw_cache: dict = {"t": 0, "data": None}


def _dmi(k: str) -> str:
    v = _read(f"/sys/class/dmi/id/{k}") or ""
    return "" if v.lower() in ("default string", "to be filled by o.e.m.", "not specified", "system product name") else v


def ram_modules() -> list[dict]:
    if not shutil.which("dmidecode"):
        return []
    rc, out = run_quick(["dmidecode", "-t", "17"], timeout=10)
    mods = []
    for block in out.split("\n\n"):
        if "Memory Device" not in block:
            continue
        f = dict(re.findall(r"^\s*([A-Za-z ]+):\s*(.+)$", block, re.M))
        size = f.get("Size", "")
        if not size or "No Module" in size or size.startswith("Unknown"):
            continue
        mods.append({"slot": f.get("Locator", ""), "size": size, "type": f.get("Type", ""),
                     "speed": f.get("Configured Memory Speed") or f.get("Speed", ""),
                     "maker": f.get("Manufacturer", ""), "part": (f.get("Part Number") or "").strip()})
    return mods


PCI_GROUPS = [("GPU", r"VGA|3D|Display"), ("Network", r"Ethernet|Network|Wireless"),
              ("Storage", r"SATA|NVM|RAID|SAS|Mass storage|Non-Volatile"), ("USB", r"USB"), ("Audio", r"Audio")]


def pci_devices() -> dict[str, list[str]]:
    rc, out = run_quick(["lspci", "-mm"], timeout=10)
    groups: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = re.findall(r'"([^"]*)"', line)
        if len(parts) < 3:
            continue
        cls, vendor, dev = parts[0], parts[1], parts[2]
        for g, rx in PCI_GROUPS:
            if re.search(rx, cls, re.I):
                groups.setdefault(g, []).append(f"{vendor} {dev}".strip())
                break
    return groups


@app.get("/api/hardware")
async def hardware(refresh: bool = False):
    if _hw_cache["data"] and not refresh and time.time() - _hw_cache["t"] < 600:
        return _hw_cache["data"]

    def collect():
        f = psutil.cpu_freq()
        mods = ram_modules()
        os_name = (_read("/etc/os-release") or "").split('PRETTY_NAME="')[-1].split('"')[0]
        return {
            "system": " ".join(x for x in (_dmi("sys_vendor"), _dmi("product_name")) if x),
            "board": " ".join(x for x in (_dmi("board_vendor"), _dmi("board_name")) if x),
            "bios": " ".join(x for x in (_dmi("bios_vendor"), _dmi("bios_version"), _dmi("bios_date")) if x),
            "cpu": {"model": _cpu_model(), "cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(),
                    "max_mhz": round(f.max) if f and f.max else None},
            "ram_total": psutil.virtual_memory().total, "ram_modules": mods,
            "pci": pci_devices(), "os": os_name, "kernel": os.uname().release,
        }
    data = await asyncio.to_thread(collect)
    _hw_cache.update(t=time.time(), data=data)
    return data


# ---- disks: HDD / SSD / NVMe, usage, live speed, SMART health -------------------------
_smart_cache: dict[str, tuple[float, dict]] = {}


def smart_info(dev: str) -> dict:
    hit = _smart_cache.get(dev)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    info: dict = {}
    if shutil.which("smartctl"):
        rc, out = run_quick(["smartctl", "-j", "-H", "-A", "-i", f"/dev/{dev}"], timeout=20)
        try:
            j = json.loads(out)
            info["available"] = True
            if "smart_status" in j:
                info["healthy"] = bool(j["smart_status"].get("passed"))
            info["temp"] = (j.get("temperature") or {}).get("current")
            info["power_on_hours"] = (j.get("power_on_time") or {}).get("hours")
            nv = j.get("nvme_smart_health_information_log") or {}
            if nv:
                info["wear_percent"] = nv.get("percentage_used")
                info["media_errors"] = nv.get("media_errors")
                info["written_tb"] = round(nv.get("data_units_written", 0) * 512000 / 1e12, 2)
            for a in (j.get("ata_smart_attributes") or {}).get("table", []):
                if a.get("id") == 5:
                    info["reallocated"] = a.get("raw", {}).get("value")
                elif a.get("id") == 197:
                    info["pending"] = a.get("raw", {}).get("value")
                elif a.get("id") in (177, 231, 233) and "wear_percent" not in info:
                    v = a.get("value")
                    if a.get("id") == 177 and v is not None:
                        info["wear_percent"] = max(0, 100 - v)
                elif a.get("id") == 241:
                    raw = a.get("raw", {}).get("value") or 0
                    info["written_tb"] = round(raw * 512 / 1e12, 2)
        except Exception:
            info = {"available": False}
    else:
        info = {"available": False, "reason": "smartmontools not installed"}
    _smart_cache[dev] = (time.time(), info)
    return info


@app.get("/api/disks")
async def disks():
    rc, out = run_quick(["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,MOUNTPOINTS,FSTYPE,LABEL"], timeout=10)
    try:
        tree = json.loads(out).get("blockdevices", [])
    except Exception:
        tree = []
    result = []

    def mounts_of(node, acc):
        for mp in node.get("mountpoints") or []:
            if mp and not mp.startswith("/snap") and mp != "[SWAP]":
                try:
                    u = psutil.disk_usage(mp)
                    acc.append({"mount": mp, "fs": node.get("fstype"), "label": node.get("label"),
                                "total": u.total, "used": u.used, "percent": u.percent, "part": node["name"]})
                except Exception:
                    pass
        for ch in node.get("children") or []:
            mounts_of(ch, acc)
        return acc

    for d in tree:
        if d.get("type") != "disk" or d["name"].startswith(("loop", "zram", "ram", "sr")):
            continue
        tran = (d.get("tran") or "").lower()
        kind = "NVMe" if tran == "nvme" or d["name"].startswith("nvme") else ("HDD" if d.get("rota") in (True, "1", 1) else "SSD")
        if tran == "usb":
            kind += " (USB)"
        smart = await asyncio.to_thread(smart_info, d["name"])
        io = LIVE["disk"].get(d["name"], {})
        # one row per partition: bind mounts (e.g. /var/lib/docker on /) show the shortest path only
        per_part: dict[str, dict] = {}
        for m in mounts_of(d, []):
            cur = per_part.get(m["part"])
            if not cur or len(m["mount"]) < len(cur["mount"]):
                per_part[m["part"]] = m
        result.append({"name": d["name"], "model": (d.get("model") or "").strip(), "serial": d.get("serial"),
                       "size": int(d.get("size") or 0), "type": kind, "mounts": sorted(per_part.values(), key=lambda m: m["mount"]),
                       "read_bps": io.get("read_bps", 0), "write_bps": io.get("write_bps", 0), "busy": io.get("busy"),
                       "smart": smart})
    return result


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
    ids = {a["id"] for a in apps}
    for src in load_config().get("catalog_sources", []):
        try:
            for a in json.loads((CATALOGS_DIR / f"{src['id']}.json").read_text()).get("apps", []):
                if a.get("id") not in ids:
                    ids.add(a["id"])
                    apps.append({**a, "source": src.get("name", src["id"])})
        except Exception:
            continue
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
async def list_apps(request: Request):
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
    doms = {}
    for d in load_config().get("domains", []):
        doms.setdefault(d["target"], d["domain"])
    for a in out:
        a["domain"] = doms.get(a["id"], "")
    if request.state.role != "admin":
        out = [a for a in out if a["installed"] and a["id"] in request.state.apps]
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
            "inputs": a.get("inputs", []), "notes": a.get("notes", ""), "custom": a.get("custom", False), "source": a.get("source", ""),
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
    env: dict[str, str] = {}  # extra .env values (bundles use this to wire apps together)
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
    for k, v in body.env.items():
        if not re.match(r"^[A-Z_][A-Z0-9_]*$", k) or "\n" in str(v):
            raise HTTPException(400, f"Bad env value {k}")
        env[k] = str(v)
    for k, v in old.items():  # keep values set earlier (bundle wiring, hand edits)
        if k not in env and k not in ("GPU_MODE", "GPU_POOL"):
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
        # seed config files the app expects to exist (downloaded once, never overwritten)
        seeded = set()
        base = (DATA_DIR / app_id).resolve()
        for rel, content in (a.get("write_files") or {}).items():
            dest = (DATA_DIR / app_id / rel).resolve()
            if str(dest).startswith(str(base) + "/") and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
                task_log(t, f"Created {rel}")
        for rel, url in (a.get("seed_files") or {}).items():
            dest = (DATA_DIR / app_id / rel).resolve()
            if not str(dest).startswith(str((DATA_DIR / app_id).resolve()) + "/") or dest.exists():
                continue
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                    r = await c.get(url)
                    r.raise_for_status()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.content)
                seeded.add(rel)
                task_log(t, f"Created {rel}")
            except Exception as e:
                task_log(t, f"Could not download {rel}: {e}")
        for rel, kv in (a.get("patch_files") or {}).items():
            if rel not in seeded:
                continue
            dest = DATA_DIR / app_id / rel
            text = dest.read_text()
            for k, v in kv.items():
                if v == "{{ollama_model}}":
                    try:
                        tags = await ollama_get("/api/tags", timeout=5)
                        names = [m["name"] for m in tags.get("models", []) if "embed" not in m["name"]]
                        v = next((x for x in names if TOOL_MODEL_HINT.search(x)), names[0] if names else "qwen3:8b")
                    except Exception:
                        v = "qwen3:8b"
                text = re.sub(rf'^(\s*){re.escape(k)}\s*=.*$', lambda m: f'{m.group(1)}{k} = {json.dumps(v)}', text, count=1, flags=re.M)
            dest.write_text(text)
            task_log(t, f"Configured {rel}: " + ", ".join(kv))
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
    if action in ("stop", "uninstall"):
        _expected_stops.add(app_id)
    if action not in ACTIONS:
        raise HTTPException(404, "Unknown action")
    t = new_task("app", f"{action.capitalize()} {app_id}")

    async def run():
        rc = 0
        if action == "update":
            refresh_compose(app_id, t)
        for step in ACTIONS[action]:
            rc = await run_cmd(t, compose_cmd(app_id) + step)
            if rc != 0:
                break
        if action == "update" and rc == 0:
            await run_cmd(t, ["docker", "image", "prune", "-f"])
        task_done(t, rc == 0, "Done" if rc == 0 else "Failed - see log")
    asyncio.create_task(run())
    return {"task": t["id"]}


def refresh_compose(aid: str, t: dict) -> None:
    """Re-render an installed app from the latest catalog (keeps .env secrets, port, GPU and pool)."""
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


@app.post("/api/apps-update-all")
async def apps_update_all():
    installed = [d.name for d in APPS_DIR.iterdir() if (d / "docker-compose.yml").exists()]
    t = new_task("app", f"Update all apps ({len(installed)})")
    t["progress"] = 0

    async def run():
        failed = []
        for i, aid in enumerate(installed):
            task_log(t, f"== [{i + 1}/{len(installed)}] {aid} ==")
            refresh_compose(aid, t)
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
    srcs = await refresh_sources()
    return {"ok": True, "apps": len(data["apps"]), "sources": len(srcs)}


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
# Users and roles: admins run everything, users only see and control the apps they are given
# --------------------------------------------------------------------------------------
USER_RE = re.compile(r"^[a-z_][a-z0-9_.\-]{1,31}$")


def _admins(cfg: dict) -> list[str]:
    return [u for u in cfg["users"] if user_role(cfg, u) == "admin"]


@app.get("/api/users")
async def list_users():
    cfg = load_config()
    return [{"username": u, "role": user_role(cfg, u), "apps": sorted(user_apps(cfg, u))} for u in sorted(cfg["users"])]


class UserBody(BaseModel):
    username: str
    password: str = ""
    role: str = "user"
    apps: list[str] = []


@app.post("/api/users")
async def save_user(body: UserBody, request: Request):
    cfg = load_config()
    u = body.username.strip().lower()
    if not USER_RE.match(u):
        raise HTTPException(400, "Username: 2-32 lowercase letters, numbers, dot, dash or underscore")
    if body.role not in ("admin", "user"):
        raise HTTPException(400, "Role must be admin or user")
    new = u not in cfg["users"]
    if new and len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if body.password and len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if not new and body.role != "admin" and user_role(cfg, u) == "admin" and len(_admins(cfg)) <= 1:
        raise HTTPException(400, "You can't remove the last admin")
    if u == request.state.user and body.role != "admin":
        raise HTTPException(400, "You can't take admin rights away from yourself")
    bad = [a for a in body.apps if not APP_ID_RE.match(a)]
    if bad:
        raise HTTPException(400, "Bad app id: " + ", ".join(bad))
    if body.password:
        cfg["users"][u] = hash_password(body.password)
    roles = cfg.setdefault("roles", {})
    if body.role == "admin":
        roles.pop(u, None)
    else:
        roles[u] = {"role": "user", "apps": sorted(set(body.apps))}
    save_config(cfg)
    return {"ok": True, "created": new}


@app.delete("/api/users/{username}")
async def delete_user(username: str, request: Request):
    cfg = load_config()
    if username not in cfg["users"]:
        raise HTTPException(404, "No such user")
    if username == request.state.user:
        raise HTTPException(400, "You can't delete yourself")
    if user_role(cfg, username) == "admin" and len(_admins(cfg)) <= 1:
        raise HTTPException(400, "You can't delete the last admin")
    cfg["users"].pop(username)
    cfg.get("roles", {}).pop(username, None)
    save_config(cfg)
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Extra app store sources: add any catalog JSON by URL (your own repo, a friend's, a community list)
# --------------------------------------------------------------------------------------
CATALOGS_DIR = HOME / "catalogs"


def _raw_github(url: str) -> str:
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)/?$", url)
    if m:  # bare repo link: assume apps_catalog.json on the default branch
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/HEAD/apps_catalog.json"
    return url


async def _fetch_source(url: str) -> tuple[list[dict], int]:
    """Download a catalog, keep only apps that look valid. Returns (apps, skipped)."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(_raw_github(url))
    if r.status_code != 200:
        raise HTTPException(502, f"HTTP {r.status_code} from {url}")
    if len(r.content) > 5_000_000:
        raise HTTPException(400, "Catalog is bigger than 5MB")
    try:
        data = r.json()
    except Exception:
        raise HTTPException(400, "That URL is not a JSON app catalog")
    items = data.get("apps") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(400, 'Catalog JSON must be {"apps": [...]} or a list of apps')
    good, skipped = [], 0
    for a in items:
        try:
            assert isinstance(a, dict) and APP_ID_RE.match(str(a.get("id", "")))
            comp = yaml.safe_load(a["compose"])
            assert isinstance(comp, dict) and isinstance(comp.get("services"), dict)
            a = {k: v for k, v in a.items() if k not in ("custom", "source")}
            good.append(a)
        except Exception:
            skipped += 1
    if not good:
        raise HTTPException(400, "No valid apps found in that catalog")
    return good, skipped


class SourceBody(BaseModel):
    url: str
    name: str = ""


@app.get("/api/catalog-sources")
async def list_sources():
    return load_config().get("catalog_sources", [])


@app.post("/api/catalog-sources")
async def add_source(body: SourceBody):
    url = body.url.strip()
    if not re.match(r"^https://[^\s]+$", url):
        raise HTTPException(400, "Enter an https:// URL")
    cfg = load_config()
    srcs = cfg.setdefault("catalog_sources", [])
    if any(s["url"] == url for s in srcs):
        raise HTTPException(400, "That source is already added")
    apps, skipped = await _fetch_source(url)
    sid = re.sub(r"[^a-z0-9]+", "-", (body.name or url.split("//", 1)[-1]).lower()).strip("-")[:30] or "source"
    while any(s["id"] == sid for s in srcs):
        sid += "-2"
    CATALOGS_DIR.mkdir(parents=True, exist_ok=True)
    (CATALOGS_DIR / f"{sid}.json").write_text(json.dumps({"apps": apps}))
    src = {"id": sid, "name": (body.name or (url.split("/")[3] if url.count("/") > 3 else url))[:40], "url": url,
           "apps": len(apps), "skipped": skipped, "updated": int(time.time()), "error": ""}
    srcs.append(src)
    save_config(cfg)
    return src


async def refresh_sources() -> list[dict]:
    cfg = load_config()
    for s in cfg.get("catalog_sources", []):
        try:
            apps, skipped = await _fetch_source(s["url"])
            CATALOGS_DIR.mkdir(parents=True, exist_ok=True)
            (CATALOGS_DIR / f"{s['id']}.json").write_text(json.dumps({"apps": apps}))
            s.update(apps=len(apps), skipped=skipped, updated=int(time.time()), error="")
        except HTTPException as e:
            s["error"] = str(e.detail)
        except Exception as e:
            s["error"] = str(e)
    save_config(cfg)
    return cfg.get("catalog_sources", [])


@app.post("/api/catalog-sources/refresh")
async def refresh_sources_api():
    return await refresh_sources()


@app.delete("/api/catalog-sources/{sid}")
async def delete_source(sid: str):
    cfg = load_config()
    cfg["catalog_sources"] = [s for s in cfg.get("catalog_sources", []) if s["id"] != sid]
    save_config(cfg)
    if re.match(r"^[a-z0-9\-]+$", sid):
        (CATALOGS_DIR / f"{sid}.json").unlink(missing_ok=True)
    return {"ok": True}


# --------------------------------------------------------------------------------------
# 1-click domains with automatic HTTPS (Caddy + Let's Encrypt), for the panel and any app
# --------------------------------------------------------------------------------------
PROXY_DIR = HOME / "proxy"
DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9\-]{1,62}$")
PROXY_COMPOSE = """services:
  caddy:
    image: caddy:2
    container_name: aipanel-proxy
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./data:/data
      - ./config:/config
"""


def _proxy_cmd() -> list[str]:
    return ["docker", "compose", "-p", "aipanel-proxy", "-f", str(PROXY_DIR / "docker-compose.yml")]


def _target_port(target: str) -> tuple[Optional[int], str]:
    if target == "panel":
        return PORT, "http"
    env_file = APPS_DIR / target / ".env"
    if not env_file.exists():
        return None, "http"
    port = parse_env(env_file.read_text()).get("APP_PORT")
    scheme = "http"
    try:
        scheme = get_app(target).get("scheme", "http")
        port = port or get_app(target).get("port")
    except HTTPException:
        pass
    return (int(port) if port else None), scheme


def render_caddyfile(cfg: dict) -> str:
    email = cfg.get("acme_email", "")
    out = ["# Written by AI Panel. Edit domains in the web UI, not here.", "{"]
    if email:
        out.append(f"\temail {email}")
    out += ["}", ""]
    for d in cfg.get("domains", []):
        port, scheme = _target_port(d["target"])
        if not port:
            out.append(f"# {d['domain']}: {d['target']} is not installed, skipped\n")
            continue
        out.append(f"{d['domain']} {{")
        if d.get("tls") == "internal":
            out.append("\ttls internal")
        out.append("\tencode zstd gzip")
        if scheme == "https":
            out += [f"\treverse_proxy https://127.0.0.1:{port} {{", "\t\ttransport http {", "\t\t\ttls_insecure_skip_verify", "\t\t}", "\t}"]
        else:
            out.append(f"\treverse_proxy 127.0.0.1:{port}")
        out += ["}", ""]
    return "\n".join(out)


def _proxy_running() -> bool:
    rc, out = run_quick(["docker", "inspect", "-f", "{{.State.Running}}", "aipanel-proxy"], timeout=10)
    return rc == 0 and out.strip() == "true"


async def apply_proxy(cfg: dict) -> str:
    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    (PROXY_DIR / "docker-compose.yml").write_text(PROXY_COMPOSE)
    cf = PROXY_DIR / "Caddyfile"
    old = cf.read_text() if cf.exists() else None
    if not cfg.get("domains"):
        cf.write_text(render_caddyfile(cfg))
        if await asyncio.to_thread(_proxy_running):
            run_quick(_proxy_cmd() + ["down"], timeout=60)
        return "No domains left, so the HTTPS proxy was stopped"
    if not docker_ok():
        raise HTTPException(400, "Docker is needed for domains. Re-run install_app.sh.")
    running = await asyncio.to_thread(_proxy_running)
    if not running:
        busy = [p for p in (80, 443) if not port_free(p)]
        if busy:
            raise HTTPException(400, f"Port {' and '.join(map(str, busy))} is already used by something else "
                                     "(for example Nginx Proxy Manager or another web server). Stop it first.")
    with open(cf, "w") as f:  # rewrite in place: the container bind-mounts this exact file
        f.write(render_caddyfile(cfg))
    if not running:
        rc, out = run_quick(_proxy_cmd() + ["up", "-d"], timeout=300)
        if rc != 0:
            raise HTTPException(500, "Could not start the HTTPS proxy: " + out[-400:])
        return "HTTPS proxy started"
    rc, out = run_quick(["docker", "exec", "aipanel-proxy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile",
                         "--adapter", "caddyfile"], timeout=60)
    if rc != 0:
        if old is not None:
            with open(cf, "w") as f:
                f.write(old)
        raise HTTPException(400, "The proxy rejected the new settings: " + out[-400:])
    return "HTTPS proxy reloaded"


_ip_cache: dict = {"t": 0, "ip": None}


async def public_ip() -> Optional[str]:
    if time.time() - _ip_cache["t"] < 600:
        return _ip_cache["ip"]
    ip = None
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(url)
            if r.status_code == 200 and re.match(r"^[0-9a-f.:]+$", r.text.strip()):
                ip = r.text.strip()
                break
        except Exception:
            continue
    _ip_cache.update(t=time.time(), ip=ip)
    return ip


def _resolve(domain: str) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)})
    except Exception:
        return []


@app.get("/api/domains")
async def list_domains():
    cfg = load_config()
    pip = await public_ip()
    out = []
    for d in cfg.get("domains", []):
        ips = await asyncio.to_thread(_resolve, d["domain"])
        port, _ = _target_port(d["target"])
        out.append({**d, "port": port, "resolves_to": ips, "dns_ok": bool(pip and pip in ips) if d.get("tls") != "internal" else bool(ips)})
    return {"domains": out, "email": cfg.get("acme_email", ""), "public_ip": pip,
            "proxy_running": await asyncio.to_thread(_proxy_running) if docker_ok() else False}


class DomainBody(BaseModel):
    domain: str
    target: str
    email: str = ""
    tls: str = "auto"


@app.post("/api/domains")
async def add_domain(body: DomainBody):
    dom = body.domain.strip().lower().rstrip(".")
    if dom.startswith(("http://", "https://")):
        dom = dom.split("://", 1)[1].split("/")[0]
    if not DOMAIN_RE.match(dom):
        raise HTTPException(400, "Enter a domain like app.example.com")
    if body.target != "panel" and not (APP_ID_RE.match(body.target) and (APPS_DIR / body.target / ".env").exists()):
        raise HTTPException(400, "Pick the panel or an installed app")
    if body.tls not in ("auto", "internal"):
        raise HTTPException(400, "tls must be auto or internal")
    cfg = load_config()
    if body.email:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", body.email):
            raise HTTPException(400, "Email address doesn't look right")
        cfg["acme_email"] = body.email.strip()
    doms = [d for d in cfg.get("domains", []) if d["domain"] != dom]
    doms.append({"domain": dom, "target": body.target, "tls": body.tls, "added": int(time.time())})
    cfg["domains"] = doms
    msg = await apply_proxy(cfg)
    save_config(cfg)
    ips = await asyncio.to_thread(_resolve, dom)
    pip = await public_ip()
    warn = ""
    if body.tls == "auto" and pip and pip not in ips:
        warn = (f"DNS for {dom} points to {', '.join(ips) or 'nothing'}, not this server's public IP {pip}. "
                "Add an A record, and forward ports 80 and 443 on your router. The certificate is issued as soon as both work.")
    return {"ok": True, "message": msg, "warning": warn, "url": f"https://{dom}/"}


@app.delete("/api/domains/{domain}")
async def delete_domain(domain: str):
    cfg = load_config()
    cfg["domains"] = [d for d in cfg.get("domains", []) if d["domain"] != domain]
    msg = await apply_proxy(cfg)
    save_config(cfg)
    return {"ok": True, "message": msg}


# --------------------------------------------------------------------------------------
# GPU hot plug: notice GPUs that appear, disappear or crash, then re-split models across what's left
# --------------------------------------------------------------------------------------
GPU_WATCH: dict = {"known": None, "names": {}, "pending": None, "count": 0, "events": [], "todo": None}


def _gpu_watch(gpus: list[dict]) -> None:
    """Called from the sampler thread every 5 s. A change must hold for 2 samples (10 s) to count."""
    sig = frozenset(g["bus"] for g in gpus if g.get("bus"))
    names = {g["bus"]: (g["name"] if g["name"].lower().startswith(g["vendor"].lower()) else f'{g["vendor"]} {g["name"]}') + (f' ({round((g.get("vram_total_mb") or 0) / 1024)}GB)' if g.get("vram_total_mb") else "") for g in gpus}
    w = GPU_WATCH
    if w["known"] is None:
        w["known"], w["names"] = sig, names
        return
    if sig == w["known"]:
        w["pending"], w["count"] = None, 0
        return
    if sig != w["pending"]:
        w["pending"], w["count"] = sig, 1
        return
    w["count"] += 1
    if w["count"] < 2:
        return
    added = sorted(sig - w["known"])
    removed = sorted(w["known"] - sig)
    allnames = {**w["names"], **names}
    ev = {"t": int(time.time()), "added": [allnames.get(b, b) for b in added], "removed": [allnames.get(b, b) for b in removed],
          "gpus": len(sig), "vram_gb": round(sum((g.get("vram_total_mb") or 0) for g in gpus) / 1024, 1), "actions": []}
    w["known"], w["names"], w["pending"], w["count"] = sig, names, None, 0
    w["events"].insert(0, ev)
    del w["events"][50:]
    w["todo"] = ev


def _svc_active(name: str) -> bool:
    rc, out = run_quick(["systemctl", "is-active", name], timeout=10)
    return out.strip().startswith("active")


async def gpu_rebalance(ev: dict, manual: bool = False) -> None:
    cfg = load_config()
    if not manual and not cfg.get("auto_rebalance", True):
        ev["actions"].append("Auto rebalance is off, nothing was restarted")
        return
    _vk_cache["t"] = 0  # Vulkan device numbers may have shifted
    t = new_task("gpu", "Rebalance models across GPUs")
    t["progress"] = None
    try:
        l = cfg["llama"]
        if l.get("model") and await asyncio.to_thread(_svc_active, "ai-llama.service"):
            if l.get("devices"):
                msg = "llama.cpp uses a fixed device list, so it was left alone. Pick devices again on the Models page."
            else:
                try:
                    penv = pool_vk_env(l.get("pool", ""))
                except HTTPException as e:
                    penv = {}
                    task_log(t, f"Pool problem ({e.detail}), using every GPU instead")
                devs = await asyncio.to_thread(llama_devices, penv)
                args = build_llama_args(cfg, devs)
                write_llama_runner(args, penv)
                rc, out = run_quick(["systemctl", "restart", "ai-llama.service"], timeout=60)
                split = auto_tensor_split(devs) if l.get("tensor_split", "auto") == "auto" else l.get("tensor_split") or "fit"
                msg = (f"llama.cpp restarted on {len(devs)} GPU(s), {round(sum(d['total_mb'] for d in devs) / 1024, 1)}GB VRAM, split {split or 'single GPU'}"
                       if rc == 0 else "llama.cpp restart failed: " + out[-200:])
            ev["actions"].append(msg)
            task_log(t, msg)
        if await asyncio.to_thread(_svc_active, "ollama"):
            if cfg.get("ollama_pool"):
                try:
                    r = await ollama_pool(PoolPick(pool=cfg["ollama_pool"]))
                    msg = "Ollama pool re-applied and Ollama restarted" if r.get("ok") else "Ollama restart failed"
                except HTTPException as e:
                    msg = f"Ollama pool could not be re-applied: {e.detail}"
            else:
                rc, out = run_quick(["systemctl", "restart", "ollama"], timeout=60)
                msg = "Ollama restarted and now spreads models over every GPU" if rc == 0 else "Ollama restart failed"
            ev["actions"].append(msg)
            task_log(t, msg)
        if not ev["actions"]:
            ev["actions"].append("No model server was running, so nothing needed a restart")
        if notify_cfg(load_config())["alerts"].get("gpu_change") and not manual:
            what = ", ".join(["+" + x for x in ev["added"]] + ["-" + x for x in ev["removed"]])
            asyncio.create_task(notify(f"🔌 GPU change: {what}. Now {ev['gpus']} GPU(s), {ev['vram_gb']}GB VRAM. " + "; ".join(ev["actions"])))
        task_done(t, True, "; ".join(ev["actions"]))
    except Exception as e:
        ev["actions"].append(f"Rebalance failed: {e}")
        task_done(t, False, str(e))


class GpuSettings(BaseModel):
    auto_rebalance: bool = True


@app.get("/api/gpu/events")
async def gpu_events():
    return {"auto_rebalance": load_config().get("auto_rebalance", True), "events": GPU_WATCH["events"],
            "gpus": len(GPU_WATCH["known"] or [])}


@app.post("/api/gpu/settings")
async def gpu_settings(body: GpuSettings):
    cfg = load_config()
    cfg["auto_rebalance"] = body.auto_rebalance
    save_config(cfg)
    return {"ok": True}


@app.post("/api/gpu/rebalance")
async def gpu_rebalance_now():
    gpus = await asyncio.to_thread(all_gpus)
    ev = {"t": int(time.time()), "added": [], "removed": [], "manual": True, "gpus": len(gpus),
          "vram_gb": round(sum((g.get("vram_total_mb") or 0) for g in gpus) / 1024, 1), "actions": []}
    GPU_WATCH["events"].insert(0, ev)
    await gpu_rebalance(ev, manual=True)
    return ev


# --------------------------------------------------------------------------------------
# File manager: browse, edit, upload and download anywhere on the server (root, like the terminal)
# --------------------------------------------------------------------------------------
PROTECTED_DIRS = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/libx32", "/media", "/mnt",
                  "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/snap",
                  str(HOME), str(APPS_DIR), str(DATA_DIR), str(MODELS_DIR)}
TEXT_LIMIT = 2 * 1024 * 1024


def fpath(p: str) -> Path:
    if not p or not p.startswith("/") or "\x00" in p:
        raise HTTPException(400, "Use a full path that starts with /")
    return Path(os.path.normpath(p))


def _guard(p: Path, what: str) -> None:
    if str(p) in PROTECTED_DIRS:
        raise HTTPException(400, f"{p} is a system folder and can't be {what} from here. Use the Terminal if you really mean it.")


def _owner(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return str(uid)


def _entry(p: Path) -> dict:
    st = p.lstat()
    link = p.is_symlink()
    try:
        is_dir = p.is_dir()
    except OSError:
        is_dir = False
    return {"name": p.name, "dir": is_dir, "link": os.readlink(p) if link else None, "size": st.st_size if not is_dir else None,
            "mtime": int(st.st_mtime), "mode": oct(st.st_mode & 0o7777)[2:], "owner": _owner(st.st_uid)}


@app.get("/api/files/places")
async def file_places():
    places = [{"name": "Server root", "path": "/", "icon": "🖥️"}, {"name": "Home folders", "path": "/home", "icon": "🏠"},
              {"name": "Root home", "path": "/root", "icon": "🔑"}, {"name": "App settings", "path": str(APPS_DIR), "icon": "🧩"},
              {"name": "App data", "path": str(DATA_DIR), "icon": "🗄️"}, {"name": "Shared media", "path": str(DATA_DIR / "shared-media"), "icon": "🎬"},
              {"name": "AI models (GGUF)", "path": str(MODELS_DIR), "icon": "🧠"}, {"name": "System logs", "path": "/var/log", "icon": "📜"},
              {"name": "System config", "path": "/etc", "icon": "⚙️"}]
    seen = {"/"}
    for part in psutil.disk_partitions(all=False):
        mp = part.mountpoint
        if mp in seen or mp.startswith(("/boot", "/snap", "/var/lib/docker", "/proc", "/sys", "/run")) \
                or part.fstype in ("squashfs", "overlay", "tmpfs", "devtmpfs") or part.fstype.startswith("fuse."):
            continue
        seen.add(mp)
        try:
            u = psutil.disk_usage(mp)
            if u.total < 1 << 30:
                continue
            places.append({"name": f"Drive {mp}", "path": mp, "icon": "💽", "free": u.free, "total": u.total})
        except OSError:
            pass
    apps = sorted(d.name for d in APPS_DIR.iterdir() if d.is_dir()) if APPS_DIR.exists() else []
    return {"places": [p for p in places if os.path.isdir(p["path"])], "apps": apps}


@app.get("/api/files/list")
async def file_list(path: str = "/"):
    p = fpath(path)
    if not p.is_dir():
        raise HTTPException(404, "Folder not found")
    out = []
    try:
        for c in p.iterdir():
            try:
                out.append(_entry(c))
            except OSError:
                continue
    except PermissionError:
        raise HTTPException(403, "Permission denied")
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    try:
        u = psutil.disk_usage(str(p))
        disk = {"free": u.free, "total": u.total}
    except OSError:
        disk = None
    return {"path": str(p), "parent": str(p.parent) if str(p) != "/" else None, "entries": out[:5000],
            "truncated": len(out) > 5000, "disk": disk}


@app.get("/api/files/read")
async def file_read(path: str):
    p = fpath(path)
    if not p.is_file():
        raise HTTPException(404, "File not found")
    if p.stat().st_size > TEXT_LIMIT:
        raise HTTPException(400, "File is bigger than 2MB. Download it instead.")
    data = p.read_bytes()
    if b"\x00" in data[:8192]:
        raise HTTPException(400, "This looks like a binary file. Download it instead.")
    return {"path": str(p), "content": data.decode("utf-8", errors="replace"), "mtime": int(p.stat().st_mtime)}


class FileWrite(BaseModel):
    path: str
    content: str


@app.post("/api/files/write")
async def file_write(body: FileWrite):
    p = fpath(body.path)
    if p.exists() and not p.is_file():
        raise HTTPException(400, "That path is a folder")
    if not p.parent.is_dir():
        raise HTTPException(400, "Folder does not exist")
    tmp = p.with_name(f".{p.name}.aipanel-tmp")
    tmp.write_text(body.content)
    if p.exists():
        st = p.stat()
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:
            pass
    tmp.replace(p)
    return {"ok": True}


class FilePath(BaseModel):
    path: str


@app.post("/api/files/mkdir")
async def file_mkdir(body: FilePath):
    p = fpath(body.path)
    if p.exists():
        raise HTTPException(400, "Already exists")
    p.mkdir(parents=True)
    return {"ok": True}


class FileMove(BaseModel):
    src: str
    dest: str
    duplicate: bool = False


@app.post("/api/files/move")
async def file_move(body: FileMove):
    s, d = fpath(body.src), fpath(body.dest)
    if not s.exists() and not s.is_symlink():
        raise HTTPException(404, "Source not found")
    if d.is_dir():
        d = d / s.name
    if d.exists():
        raise HTTPException(400, f"{d} already exists")
    if str(d).startswith(str(s) + "/"):
        raise HTTPException(400, "Can't put a folder inside itself")
    if not body.duplicate:
        _guard(s, "moved")
    t0 = time.time()
    if body.duplicate:
        await asyncio.to_thread(shutil.copytree if s.is_dir() and not s.is_symlink() else shutil.copy2, s, d,
                                **({"symlinks": True} if s.is_dir() and not s.is_symlink() else {"follow_symlinks": False}))
    else:
        await asyncio.to_thread(shutil.move, str(s), str(d))
    return {"ok": True, "dest": str(d), "seconds": round(time.time() - t0, 1)}


class FileDelete(BaseModel):
    paths: list[str]


@app.post("/api/files/delete")
async def file_delete(body: FileDelete):
    ps = [fpath(x) for x in body.paths]
    for p in ps:
        _guard(p, "deleted")
    for p in ps:
        if p.is_dir() and not p.is_symlink():
            await asyncio.to_thread(shutil.rmtree, p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    return {"ok": True, "deleted": len(ps)}


class FileChmod(BaseModel):
    path: str
    mode: str
    owner: str = ""


@app.post("/api/files/chmod")
async def file_chmod(body: FileChmod):
    p = fpath(body.path)
    _guard(p, "changed")
    if not re.match(r"^[0-7]{3,4}$", body.mode):
        raise HTTPException(400, "Mode must look like 644 or 755")
    os.chmod(p, int(body.mode, 8))
    if body.owner:
        if not re.match(r"^[a-z_][a-z0-9_\-]*(:[a-z_][a-z0-9_\-]*)?$|^\d+(:\d+)?$", body.owner):
            raise HTTPException(400, "Owner must look like user, user:group or 1000:1000")
        u, _, g = body.owner.partition(":")
        shutil.chown(p, int(u) if u.isdigit() else u, (int(g) if g.isdigit() else g) if g else None)
    return {"ok": True}


@app.put("/api/files/upload")
async def file_upload(request: Request, dir: str, name: str):
    folder = fpath(dir)
    if not folder.is_dir():
        raise HTTPException(400, "Folder does not exist")
    if not name or "/" in name or name in (".", ".."):
        raise HTTPException(400, "Bad file name")
    dest = folder / name
    tmp = folder / f".{name}.upload"
    n = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                n += len(chunk)
        tmp.replace(dest)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"Upload failed: {e}")
    return {"ok": True, "path": str(dest), "bytes": n}


@app.get("/api/files/download")
async def file_download(path: str, inline: bool = False):
    p = fpath(path)
    if p.is_file():
        return FileResponse(p, filename=p.name, content_disposition_type="inline" if inline else "attachment")
    if p.is_dir():
        _guard(p, "zipped")
        from starlette.background import BackgroundTask
        tmpdir = Path(tempfile.mkdtemp(prefix="aipanel-zip-"))
        archive = await asyncio.to_thread(shutil.make_archive, str(tmpdir / (p.name or "root")), "zip", str(p))
        return FileResponse(archive, filename=(p.name or "root") + ".zip",
                            background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True))
    raise HTTPException(404, "Not found")


# --------------------------------------------------------------------------------------
# GPU eco mode: power limits per GPU (NVIDIA via nvidia-smi, AMD via hwmon power1_cap), re-applied at boot
# --------------------------------------------------------------------------------------
ECO_MODES = {"max": 1.0, "balanced": 0.8, "eco": 0.6}


def _amd_hwmon(g: dict) -> Optional[str]:
    card = g.get("card")
    if not card:
        return None
    for h in glob.glob(f"/sys/class/drm/{os.path.basename(card)}/device/hwmon/hwmon*"):
        if os.path.exists(h + "/power1_cap"):
            return h
    return None


def gpu_power_info(g: dict) -> dict:
    """Current / default / min / max power limit in watts (None when the driver doesn't expose it)."""
    if g["vendor"] == "NVIDIA":
        rc, out = run_quick(["nvidia-smi", "-i", str(g.get("nv_index", 0)), "--query-gpu=power.limit,power.default_limit,power.min_limit,power.max_limit",
                             "--format=csv,noheader,nounits"], timeout=8)
        try:
            cur, dflt, mn, mx = [float(x) for x in out.strip().split(",")]
            return {"limit": cur, "default": dflt, "min": mn, "max": mx}
        except Exception:
            return {}
    h = _amd_hwmon(g)
    if not h:
        return {}

    def w(name):
        v = _read(f"{h}/{name}")
        return round(int(v) / 1_000_000) if v and v.isdigit() else None
    return {"limit": w("power1_cap"), "default": w("power1_cap_default") or w("power1_cap_max"), "min": w("power1_cap_min") or 0, "max": w("power1_cap_max")}


def set_gpu_power(g: dict, watts: float) -> str:
    if g["vendor"] == "NVIDIA":
        run_quick(["nvidia-smi", "-i", str(g.get("nv_index", 0)), "-pm", "1"], timeout=10)
        rc, out = run_quick(["nvidia-smi", "-i", str(g.get("nv_index", 0)), "-pl", str(int(watts))], timeout=15)
        return "" if rc == 0 else out.strip()[-200:]
    h = _amd_hwmon(g)
    if not h:
        return "no power control in sysfs"
    try:
        Path(h + "/power1_cap").write_text(str(int(watts * 1_000_000)))
        return ""
    except Exception as e:
        return str(e)


def apply_gpu_power(cfg: dict) -> list[str]:
    p = cfg.get("gpu_power", {})
    mode, custom = p.get("mode", "max"), p.get("custom", {})
    msgs = []
    for g in all_gpus():
        info = gpu_power_info(g)
        if not info.get("default"):
            continue
        if mode == "custom" and g["bus"] in custom:
            target = float(custom[g["bus"]])
        else:
            target = info["default"] * ECO_MODES.get(mode, 1.0)
        target = max(info.get("min") or 0, min(info.get("max") or target, target))
        if info.get("limit") and abs(info["limit"] - target) < 1:
            msgs.append(f"{g['name']}: {round(target)} W")
            continue
        err = set_gpu_power(g, target)
        msgs.append(f"{g['name']}: {round(target)} W" + (f" (failed: {err})" if err else ""))
    return msgs


@app.get("/api/gpu/power")
async def gpu_power_get():
    cfg = load_config()
    gpus = await asyncio.to_thread(all_gpus)
    out = []
    for g in gpus:
        info = await asyncio.to_thread(gpu_power_info, g)
        out.append({"bus": g["bus"], "name": g["name"], "vendor": g["vendor"], "draw": g.get("power_w"), **info})
    hist = [h.get("gpw") for h in HISTORY if h.get("gpw") is not None]
    avg = sum(hist) / len(hist) if hist else sum((g.get("power_w") or 0) for g in gpus)
    p = cfg.get("gpu_power", {})
    price = float(p.get("price", 0) or 0)
    kwh_month = avg * 24 * 30 / 1000
    return {"mode": p.get("mode", "max"), "custom": p.get("custom", {}), "price": price, "currency": p.get("currency", "$"),
            "gpus": out, "avg_w": round(avg, 1), "kwh_month": round(kwh_month, 1), "cost_month": round(kwh_month * price, 2)}


class PowerBody(BaseModel):
    mode: str = "max"
    custom: dict[str, float] = {}
    price: Optional[float] = None
    currency: Optional[str] = None


@app.post("/api/gpu/power")
async def gpu_power_set(body: PowerBody):
    if body.mode not in (*ECO_MODES, "custom"):
        raise HTTPException(400, "mode must be max, balanced, eco or custom")
    cfg = load_config()
    p = cfg.setdefault("gpu_power", {})
    p["mode"] = body.mode
    if body.custom:
        p["custom"] = {k: float(v) for k, v in body.custom.items() if re.match(r"^[0-9a-f:.]+$", k) and 10 <= float(v) <= 1000}
    if body.price is not None:
        p["price"] = max(0.0, float(body.price))
    if body.currency is not None:
        p["currency"] = body.currency[:4]
    save_config(cfg)
    msgs = await asyncio.to_thread(apply_gpu_power, cfg)
    return {"ok": True, "applied": msgs}


# --------------------------------------------------------------------------------------
# Notifications (Telegram, Discord, ntfy) and alerts
# --------------------------------------------------------------------------------------
def notify_cfg(cfg: dict) -> dict:
    n = cfg.setdefault("notify", {})
    n.setdefault("telegram_token", "")
    n.setdefault("telegram_chats", [])
    n.setdefault("telegram_seen", {})
    n.setdefault("discord_webhook", "")
    n.setdefault("ntfy_url", "")
    a = n.setdefault("alerts", {})
    for k, v in {"gpu_temp": 85, "disk_pct": 90, "gpu_change": True, "app_down": True}.items():
        a.setdefault(k, v)
    return n


async def tg_api(token: str, method: str, _timeout: float = 15, **params) -> dict:
    async with httpx.AsyncClient(timeout=_timeout) as c:
        r = await c.post(f"https://api.telegram.org/bot{token}/{method}", json=params)
    return r.json()


async def notify(text: str) -> None:
    cfg = load_config()
    n = notify_cfg(cfg)
    host = socket.gethostname()
    msg = f"🧠 {host}: {text}"
    try:
        if n["telegram_token"]:
            for chat in n["telegram_chats"]:
                await tg_api(n["telegram_token"], "sendMessage", chat_id=chat, text=msg[:4000])
        async with httpx.AsyncClient(timeout=10) as c:
            if n["discord_webhook"].startswith("https://"):
                await c.post(n["discord_webhook"], json={"content": msg[:1900]})
            if n["ntfy_url"].startswith(("https://", "http://")):
                await c.post(n["ntfy_url"], content=msg.encode(), headers={"Title": f"AI Panel ({host})"})
    except Exception as e:
        print("notify:", e, file=sys.stderr)


_alert_state: dict = {"sent": {}, "apps": None}


def _cooldown(key: str, seconds: int) -> bool:
    now = time.time()
    if now - _alert_state["sent"].get(key, 0) < seconds:
        return False
    _alert_state["sent"][key] = now
    return True


async def check_alerts() -> None:
    cfg = load_config()
    n = notify_cfg(cfg)
    if not (n["telegram_token"] and n["telegram_chats"]) and not n["discord_webhook"] and not n["ntfy_url"]:
        return
    a = n["alerts"]
    msgs = []
    for g in LIVE.get("gpus", []):
        if a.get("gpu_temp") and (g.get("temp") or 0) >= a["gpu_temp"] and _cooldown("temp" + g["bus"], 3600):
            msgs.append(f"🔥 {g['name']} is at {g['temp']:.0f}°C")
    if a.get("disk_pct"):
        for part in psutil.disk_partitions(all=False):
            if part.fstype in ("squashfs", "overlay", "tmpfs") or part.mountpoint.startswith(("/snap", "/boot")):
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            if u.total >= 1 << 30 and u.percent >= a["disk_pct"] and _cooldown("disk" + part.mountpoint, 6 * 3600):
                msgs.append(f"💽 {part.mountpoint} is {u.percent:.0f}% full ({u.free / 2**30:.1f}GB free)")
    if a.get("app_down") and docker_ok():
        states = await asyncio.to_thread(app_states)
        now_ok = {k: v["running"] == v["total"] and v["total"] > 0 for k, v in states.items()}
        prev = _alert_state["apps"]
        if prev is not None:
            for k, was in prev.items():
                if was and not now_ok.get(k, False) and (APPS_DIR / k / "docker-compose.yml").exists() and k not in _expected_stops:
                    msgs.append(f"⚠️ App {k} stopped or crashed")
        _expected_stops.clear()
        _alert_state["apps"] = now_ok
    for m in msgs:
        await notify(m)


_expected_stops: set[str] = set()


class NotifyBody(BaseModel):
    telegram_token: Optional[str] = None
    telegram_chats: Optional[list[int]] = None
    discord_webhook: Optional[str] = None
    ntfy_url: Optional[str] = None
    alerts: Optional[dict] = None


@app.get("/api/notify")
async def notify_get():
    n = dict(notify_cfg(load_config()))
    n["telegram_token"] = ("set:" + n["telegram_token"][-4:]) if n["telegram_token"] else ""
    if n["telegram_token"]:
        n["bot"] = TG_STATE.get("bot", "")
    return n


@app.post("/api/notify")
async def notify_set(body: NotifyBody):
    cfg = load_config()
    n = notify_cfg(cfg)
    if body.telegram_token is not None and not body.telegram_token.startswith("set:"):
        tok = body.telegram_token.strip()
        if tok and not re.match(r"^\d+:[A-Za-z0-9_\-]{30,}$", tok):
            raise HTTPException(400, "That doesn't look like a Telegram bot token (from @BotFather)")
        if tok:
            r = await tg_api(tok, "getMe")
            if not r.get("ok"):
                raise HTTPException(400, "Telegram rejected the token: " + str(r.get("description")))
            TG_STATE["bot"] = r["result"].get("username", "")
        n["telegram_token"] = tok
    if body.telegram_chats is not None:
        n["telegram_chats"] = [int(c) for c in body.telegram_chats]
    if body.discord_webhook is not None:
        if body.discord_webhook and not body.discord_webhook.startswith("https://"):
            raise HTTPException(400, "Discord webhook must start with https://")
        n["discord_webhook"] = body.discord_webhook.strip()
    if body.ntfy_url is not None:
        if body.ntfy_url and not body.ntfy_url.startswith(("https://", "http://")):
            raise HTTPException(400, "ntfy URL must start with http(s)://")
        n["ntfy_url"] = body.ntfy_url.strip()
    if body.alerts is not None:
        for k in ("gpu_temp", "disk_pct"):
            if k in body.alerts:
                n["alerts"][k] = int(body.alerts[k] or 0)
        for k in ("gpu_change", "app_down"):
            if k in body.alerts:
                n["alerts"][k] = bool(body.alerts[k])
    save_config(cfg)
    return {"ok": True}


@app.post("/api/notify/test")
async def notify_test():
    await notify("✅ Test message from AI Server Control Panel")
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Telegram bot: status, app control, eco mode and the AI assistant from your phone
# --------------------------------------------------------------------------------------
TG_STATE: dict = {"bot": "", "hist": {}, "running": False}
TG_HELP = ("Commands:\n/status - CPU, RAM, GPUs, disks\n/apps - installed apps\n/start_app ID, /stop_app ID, /restart_app ID\n"
           "/eco max|balanced|eco - GPU power\n/help\nOr just write what you want, for example \"install jellyfin\" or "
           "\"make a video about the ocean\". I ask before changing anything.")


def status_text() -> str:
    vm = psutil.virtual_memory()
    lines = [f"CPU {psutil.cpu_percent(interval=0.3):.0f}% · RAM {vm.percent:.0f}% ({(vm.total - vm.available) / 2**30:.1f}/{vm.total / 2**30:.0f}GB)"]
    for g in LIVE.get("gpus") or all_gpus():
        vt, vu = g.get("vram_total_mb") or 0, g.get("vram_used_mb") or 0
        lines.append(f"🎮 {g['name']}: {g.get('util') or 0:.0f}% · {vu / 1024:.1f}/{vt / 1024:.0f}GB · "
                     f"{(g.get('temp') or 0):.0f}°C · {(g.get('power_w') or 0):.0f}W")
    for part in psutil.disk_partitions(all=False):
        if part.fstype in ("squashfs", "overlay", "tmpfs") or part.mountpoint.startswith(("/snap", "/boot")):
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
            if u.total < 1 << 30:
                continue
            lines.append(f"💽 {part.mountpoint}: {u.percent:.0f}% used, {u.free / 2**30:.0f}GB free")
        except OSError:
            pass
    up = int(time.time() - psutil.boot_time())
    lines.append(f"⏱ up {up // 86400}d {up % 86400 // 3600}h")
    return "\n".join(lines)


def apps_text() -> str:
    states = app_states()
    out = []
    for d in sorted(APPS_DIR.iterdir()) if APPS_DIR.exists() else []:
        if (d / "docker-compose.yml").exists():
            st = states.get(d.name)
            ok = st and st["total"] and st["running"] == st["total"]
            out.append(f"{'🟢' if ok else '⚪'} {d.name}")
    return "\n".join(out) or "No apps installed yet."


def _action_keyboard(actions: list[dict]) -> Optional[dict]:
    if not actions:
        return None
    return {"inline_keyboard": [[{"text": "✅ " + a["label"][:50], "callback_data": "ok:" + a["id"]},
                                 {"text": "✖", "callback_data": "no:" + a["id"]}] for a in actions]}


async def tg_handle(token: str, upd: dict) -> None:
    cfg = load_config()
    n = notify_cfg(cfg)
    if "callback_query" in upd:
        cq = upd["callback_query"]
        chat = cq["message"]["chat"]["id"]
        if chat not in n["telegram_chats"]:
            return
        kind, _, aid = cq.get("data", "").partition(":")
        await tg_api(token, "answerCallbackQuery", callback_query_id=cq["id"])
        if kind == "ok":
            res = await run_proposal(aid)
            await tg_api(token, "sendMessage", chat_id=chat, text=res["message"][:4000])
        else:
            ASSIST_PENDING.pop(aid, None)
            await tg_api(token, "sendMessage", chat_id=chat, text="Cancelled.")
        return
    m = upd.get("message") or {}
    chat = (m.get("chat") or {}).get("id")
    text = (m.get("text") or "").strip()
    if not chat or not text:
        return
    if chat not in n["telegram_chats"]:
        n["telegram_seen"][str(chat)] = (m.get("from") or {}).get("username") or (m.get("chat") or {}).get("first_name") or ""
        save_config(cfg)
        await tg_api(token, "sendMessage", chat_id=chat, text=f"This chat isn't allowed yet. Chat ID: {chat}\n"
                     "In the panel go to Settings > Phone and alerts and click Allow next to this chat.")
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    reply, actions = "", []
    if cmd in ("/start", "/help"):
        reply = TG_HELP
    elif cmd == "/status":
        reply = await asyncio.to_thread(status_text)
    elif cmd == "/apps":
        reply = await asyncio.to_thread(apps_text)
    elif cmd in ("/start_app", "/stop_app", "/restart_app") and arg:
        act = cmd[1:].split("_")[0]
        actions = [propose("app_control", {"app_id": arg.strip(), "action": act}, f"{act.capitalize()} {arg.strip()}")]
        reply = "Confirm:"
    elif cmd == "/eco" and arg.strip() in ECO_MODES:
        actions = [propose("gpu_power", {"mode": arg.strip()}, f"GPU power: {arg.strip()}")]
        reply = "Confirm:"
    else:
        hist = TG_STATE["hist"].setdefault(chat, [])
        hist.append({"role": "user", "content": text})
        r = await assistant_chat(hist[-10:])
        hist.append({"role": "assistant", "content": r["reply"]})
        del hist[:-10]
        reply, actions = r["reply"], r["actions"]
    params = {"chat_id": chat, "text": (reply or "OK")[:4000]}
    kb = _action_keyboard(actions)
    if kb:
        params["reply_markup"] = kb
    await tg_api(token, "sendMessage", **params)


async def telegram_loop() -> None:
    offset = 0
    while True:
        token = notify_cfg(load_config())["telegram_token"]
        if not token:
            await asyncio.sleep(10)
            continue
        try:
            r = await tg_api(token, "getUpdates", _timeout=40, offset=offset, timeout=25)
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    await tg_handle(token, upd)
                except Exception as e:
                    print("telegram handle:", e, file=sys.stderr)
            if not r.get("ok"):
                await asyncio.sleep(30)
        except Exception:
            await asyncio.sleep(10)


# --------------------------------------------------------------------------------------
# AI assistant: chat with a local model that can look at the server and propose actions
# --------------------------------------------------------------------------------------
ASSIST_PENDING: dict[str, dict] = {}
TOOL_MODEL_HINT = re.compile(r"qwen|llama3\.[123]|llama-3|mistral|hermes|gpt-oss|granite|command-r|deepseek|phi4|gemma3", re.I)
ASSIST_SYSTEM = """You are the assistant inside "AI Server Control Panel", a web panel that runs a home AI server (Ubuntu, Docker apps, Ollama, llama.cpp, mixed GPUs).
Help the owner run the server. Use the tools to look things up before answering. Never invent app ids: call list_apps first.
Tools that change something (install, start/stop, update, uninstall, GPU power, pull model, create video, add domain, install bundle) do NOT run right away:
they show the user a confirm button. After calling one, tell the user in one short sentence what will happen when they press it.
Keep answers short and plain. If a log shows an error, explain the likely cause in simple words and propose the fix with a tool when one fits."""


def _tool(name: str, desc: str, props: dict, req: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": req}}}


_STR = {"type": "string"}
ASSIST_TOOLS = [
    _tool("get_status", "CPU, RAM, GPUs (load, VRAM, temperature, power) and disk usage right now.", {}, []),
    _tool("list_apps", "Search the app store. Returns id, name, category and whether it is installed/running.", {"query": _STR}, []),
    _tool("list_bundles", "Ready-made sets of apps that are wired together (e.g. private ChatGPT, AI video studio).", {}, []),
    _tool("list_models", "Ollama models and GGUF files on this server.", {}, []),
    _tool("read_logs", "Last log lines of an installed app (app id) or a service: ai-panel, ollama, ai-llama, docker.", {"source": _STR}, ["source"]),
    _tool("install_app", "Install an app from the store.", {"app_id": _STR, "gpu": {"type": "string", "enum": ["auto", "nvidia", "amd", "none"]}}, ["app_id"]),
    _tool("app_control", "Start, stop, restart, update (pull latest) or uninstall an installed app.",
          {"app_id": _STR, "action": {"type": "string", "enum": ["start", "stop", "restart", "update", "uninstall"]}}, ["app_id", "action"]),
    _tool("install_bundle", "Install a ready-made bundle of apps by bundle id.", {"bundle_id": _STR}, ["bundle_id"]),
    _tool("set_gpu_power", "GPU power mode: max (full speed), balanced (80%), eco (60%, much less power and heat).",
          {"mode": {"type": "string", "enum": ["max", "balanced", "eco"]}}, ["mode"]),
    _tool("pull_model", "Download an Ollama model, e.g. qwen3:8b, llama3.2:3b, gemma3:12b.", {"name": _STR}, ["name"]),
    _tool("create_video", "Make a short video from a topic with MoneyPrinterTurbo (script, voice, subtitles, stock clips).",
          {"topic": _STR, "aspect": {"type": "string", "enum": ["9:16", "16:9", "1:1"]}}, ["topic"]),
    _tool("add_domain", "Give the panel or an installed app a domain with automatic HTTPS.", {"domain": _STR, "target": _STR}, ["domain", "target"]),
]


def propose(kind: str, args: dict, label: str) -> dict:
    aid = secrets.token_hex(6)
    ASSIST_PENDING[aid] = {"kind": kind, "args": args, "label": label, "t": time.time(), "owner": CURRENT_USER.get()}
    for k in [k for k, v in ASSIST_PENDING.items() if time.time() - v["t"] > 3600]:
        ASSIST_PENDING.pop(k, None)
    return {"id": aid, "kind": kind, "label": label, "args": args}


def _find_app(q: str) -> Optional[dict]:
    q = q.strip().lower()
    cat = load_catalog()
    for a in cat:
        if a["id"] == q:
            return a
    for a in cat:
        if a.get("name", "").lower() == q or q.replace(" ", "-") == a["id"]:
            return a
    for a in cat:
        if q and (q in a["id"] or q in a.get("name", "").lower()):
            return a
    return None


def load_bundles() -> list[dict]:
    try:
        return json.loads(CATALOG_FILE.read_text()).get("bundles", [])
    except Exception:
        return []


def _log_tail(source: str, lines: int = 80) -> str:
    if source in ("ai-panel", "ollama", "ai-llama", "docker"):
        rc, out = run_quick(["journalctl", "-u", source + (".service" if source != "docker" else ""), "-n", str(lines), "--no-pager", "-o", "cat"], timeout=15)
        return out
    if APP_ID_RE.match(source) and (APPS_DIR / source / "docker-compose.yml").exists():
        rc, out = run_quick(compose_cmd(source) + ["logs", "--tail", str(lines), "--no-color"], timeout=20)
        return out
    return f"Unknown log source {source}"


async def run_tool(name: str, args: dict, actions: list) -> str:
    if name == "get_status":
        return await asyncio.to_thread(status_text)
    if name == "list_apps":
        q = str(args.get("query") or "").lower()
        states = await asyncio.to_thread(app_states)
        rows = []
        for a in load_catalog():
            if q and q not in (a["id"] + " " + a.get("name", "") + " " + a.get("category", "") + " " + a.get("description", "")).lower():
                continue
            inst = (APPS_DIR / a["id"] / "docker-compose.yml").exists()
            st = states.get(a["id"])
            flag = "running" if inst and st and st["running"] else ("installed" if inst else "")
            rows.append(f"{a['id']}: {a.get('name')} [{a.get('category')}] {flag} - {a.get('description', '')[:80]}")
        return "\n".join(rows[:40]) or "No apps match."
    if name == "list_bundles":
        return "\n".join(f"{b['id']}: {b['name']} = {', '.join(b['apps'])}. {b.get('description', '')}" for b in load_bundles())
    if name == "list_models":
        try:
            tags = await ollama_get("/api/tags")
            names = [m["name"] for m in tags.get("models", [])]
        except Exception:
            names = ["(Ollama not reachable)"]
        ggufs = [str(p.relative_to(MODELS_DIR)) for p in MODELS_DIR.rglob("*.gguf")][:30]
        return "Ollama: " + (", ".join(names) or "none") + "\nGGUF: " + (", ".join(ggufs) or "none")
    if name == "read_logs":
        return (await asyncio.to_thread(_log_tail, str(args.get("source", "")), 60))[-6000:]
    # ---- actions: propose, the user confirms in the UI / Telegram
    if name == "install_app":
        a = _find_app(str(args.get("app_id", "")))
        if not a:
            return "No such app. Call list_apps to find the id."
        if (APPS_DIR / a["id"] / "docker-compose.yml").exists():
            return f"{a['name']} is already installed."
        req = [i for i in a.get("inputs", []) if i.get("required")]
        if req:
            return f"{a['name']} needs {', '.join(i.get('label', i['key']) for i in req)}. Tell the user to install it from the App Store page."
        actions.append(propose("install_app", {"app_id": a["id"], "gpu": args.get("gpu") or "auto"}, f"Install {a['name']}"))
        return "Confirm button shown to the user."
    if name == "app_control":
        aid, act = str(args.get("app_id", "")), str(args.get("action", ""))
        if not (APPS_DIR / aid / "docker-compose.yml").exists():
            a = _find_app(aid)
            aid = a["id"] if a else aid
        if not (APPS_DIR / aid / "docker-compose.yml").exists():
            return f"{aid} is not installed."
        actions.append(propose("app_control", {"app_id": aid, "action": act}, f"{act.capitalize()} {aid}"))
        return "Confirm button shown to the user."
    if name == "install_bundle":
        b = next((b for b in load_bundles() if b["id"] == args.get("bundle_id")), None)
        if not b:
            return "No such bundle. Call list_bundles."
        actions.append(propose("install_bundle", {"bundle_id": b["id"]}, f"Install bundle: {b['name']}"))
        return "Confirm button shown to the user."
    if name == "set_gpu_power":
        mode = str(args.get("mode", ""))
        if mode not in ECO_MODES:
            return "mode must be max, balanced or eco"
        actions.append(propose("gpu_power", {"mode": mode}, f"Set GPU power to {mode}"))
        return "Confirm button shown to the user."
    if name == "pull_model":
        mname = str(args.get("name", ""))
        if not MODEL_RE.match(mname):
            return "Invalid model name"
        actions.append(propose("pull_model", {"name": mname}, f"Download model {mname}"))
        return "Confirm button shown to the user."
    if name == "create_video":
        if not (APPS_DIR / "moneyprinter" / "docker-compose.yml").exists():
            actions.append(propose("install_app", {"app_id": "moneyprinter", "gpu": "auto"}, "Install MoneyPrinterTurbo"))
            return "MoneyPrinterTurbo is not installed. An install button was shown. After it's installed, add a free Pexels key in its settings, then ask again."
        topic = str(args.get("topic", ""))[:300]
        aspect = args.get("aspect") if args.get("aspect") in ("9:16", "16:9", "1:1") else "9:16"
        actions.append(propose("create_video", {"topic": topic, "aspect": aspect}, f"Make video: {topic[:40]}"))
        return "Confirm button shown to the user."
    if name == "add_domain":
        actions.append(propose("add_domain", {"domain": str(args.get("domain", "")), "target": str(args.get("target", "panel"))},
                               f"Add domain {args.get('domain')}"))
        return "Confirm button shown to the user."
    return f"Unknown tool {name}"


async def _llm_target(cfg: dict) -> tuple[Optional[str], Optional[str], str]:
    a = cfg.get("assistant", {})
    prov = a.get("provider", "ollama")
    if prov == "openai" and a.get("base_url"):
        return a["base_url"].rstrip("/"), a.get("model") or "gpt-4o-mini", a.get("api_key", "")
    if prov == "llama":
        return f"http://127.0.0.1:{cfg['llama']['port']}/v1", "local", ""
    model = a.get("model", "")
    if not model:
        try:
            tags = await ollama_get("/api/tags", timeout=5)
            names = [m["name"] for m in tags.get("models", []) if "embed" not in m["name"]]
            model = next((x for x in names if TOOL_MODEL_HINT.search(x)), names[0] if names else "")
        except Exception:
            return None, None, ""
    return (OLLAMA_URL + "/v1", model, "") if model else (None, None, "")


def _simple_intent(text: str, actions: list) -> str:
    """Works without any AI model: understands short commands."""
    t = text.lower().strip()
    m = re.match(r"^(install|add|setup|set up)\s+(.+)$", t)
    if m:
        b = next((b for b in load_bundles() if b["id"] in m.group(2) or b["name"].lower() in m.group(2)), None)
        if b:
            actions.append(propose("install_bundle", {"bundle_id": b["id"]}, f"Install bundle: {b['name']}"))
            return f"I can install the {b['name']} bundle."
        a = _find_app(m.group(2))
        if a:
            actions.append(propose("install_app", {"app_id": a["id"], "gpu": "auto"}, f"Install {a['name']}"))
            return f"I can install {a['name']}: {a.get('description', '')}"
        return f"I couldn't find an app called \"{m.group(2)}\". Try the App Store search."
    m = re.match(r"^(start|stop|restart|update|uninstall)\s+(.+)$", t)
    if m:
        a = _find_app(m.group(2))
        if a and (APPS_DIR / a["id"] / "docker-compose.yml").exists():
            actions.append(propose("app_control", {"app_id": a["id"], "action": m.group(1)}, f"{m.group(1).capitalize()} {a['name']}"))
            return f"Ready to {m.group(1)} {a['name']}."
        return "That app isn't installed."
    for mode in ECO_MODES:
        if re.search(rf"\b{mode}\b", t) and ("gpu" in t or "power" in t or "mode" in t or t == mode):
            actions.append(propose("gpu_power", {"mode": mode}, f"Set GPU power to {mode}"))
            return f"Ready to switch the GPUs to {mode} mode."
    m = re.search(r"(?:make|create|generate)\s+(?:a\s+)?(?:short\s+)?video\s+(?:about|on|of)\s+(.+)", t)
    if m:
        if (APPS_DIR / "moneyprinter" / "docker-compose.yml").exists():
            actions.append(propose("create_video", {"topic": m.group(1), "aspect": "9:16"}, f"Make video: {m.group(1)[:40]}"))
            return "Ready to make that video with MoneyPrinterTurbo."
        actions.append(propose("install_app", {"app_id": "moneyprinter", "gpu": "auto"}, "Install MoneyPrinterTurbo"))
        return "Videos are made with MoneyPrinterTurbo, which isn't installed yet."
    if re.search(r"status|how is|health|gpu|temp", t):
        return status_text()
    return ("No local AI model is running, so I only understand short commands like \"install jellyfin\", \"restart comfyui\", "
            "\"gpu eco\", \"status\" or \"make a video about the ocean\". Download a model such as qwen3:8b in AI Models > Ollama for full chat.")


async def assistant_chat(messages: list[dict], extra_system: str = "") -> dict:
    cfg = load_config()
    actions: list[dict] = []
    base, model, key = await _llm_target(cfg)
    last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    if not base:
        return {"reply": await asyncio.to_thread(_simple_intent, last, actions), "actions": actions, "model": None}
    msgs = [{"role": "system", "content": ASSIST_SYSTEM + ("\n" + extra_system if extra_system else "")}]
    msgs += [{"role": m["role"], "content": str(m.get("content", ""))[:8000]} for m in messages[-16:] if m.get("role") in ("user", "assistant")]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    reply = ""
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            for _ in range(6):
                r = await c.post(base + "/chat/completions", headers=headers,
                                 json={"model": model, "messages": msgs, "tools": ASSIST_TOOLS, "temperature": 0.2, "stream": False})
                if r.status_code >= 400:
                    if "tool" in r.text.lower() and "support" in r.text.lower():
                        reply = (f"The model {model} can't use tools. Pick one that can (for example qwen3:8b) in Settings > AI assistant.\n\n"
                                 + await asyncio.to_thread(_simple_intent, last, actions))
                        break
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                msg = r.json()["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    reply = msg.get("content") or ""
                    break
                msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for call in calls:
                    fn = call.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
                    except Exception:
                        args = {}
                    try:
                        res = await run_tool(fn.get("name", ""), args, actions)
                    except Exception as e:
                        res = f"Tool error: {e}"
                    msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": res[:8000]})
    except Exception as e:
        reply = f"(The AI model didn't answer: {e}. Using simple commands instead.)\n\n" + await asyncio.to_thread(_simple_intent, last, actions)
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S).strip()
    if not reply and actions:
        reply = "Press the button to confirm."
    return {"reply": reply, "actions": actions, "model": model}


class ChatBody(BaseModel):
    messages: list[dict]


@app.post("/api/assistant")
async def assistant_api(body: ChatBody):
    return await assistant_chat(body.messages)


class DiagnoseBody(BaseModel):
    source: str
    log: str = ""


@app.post("/api/assistant/diagnose")
async def assistant_diagnose(body: DiagnoseBody):
    log = body.log[-7000:] or (await asyncio.to_thread(_log_tail, body.source, 120))[-7000:]
    q = (f"These are the latest log lines from '{body.source}'. Find any errors or warnings, explain the cause in simple words, "
         f"and say how to fix it. Propose a fix with a tool if one fits (for example restart or update the app).\n\n```\n{log}\n```")
    return await assistant_chat([{"role": "user", "content": q}])


async def run_proposal(aid: str) -> dict:
    p = ASSIST_PENDING.pop(aid, None)
    if not p:
        return {"ok": False, "message": "That button has expired. Ask again."}
    k, a = p["kind"], p["args"]
    try:
        if k == "install_app":
            r = await install_app(a["app_id"], InstallBody(gpu=a.get("gpu", "auto")))
            return {"ok": True, "message": f"Installing {a['app_id']}. Follow it in Tasks.", "task": r["task"]}
        if k == "app_control":
            if a["action"] == "stop":
                _expected_stops.add(a["app_id"])
            r = await app_action(a["app_id"], a["action"])
            return {"ok": True, "message": f"{a['action'].capitalize()} {a['app_id']} started.", "task": r.get("task")}
        if k == "install_bundle":
            r = await bundle_install(a["bundle_id"], BundleBody())
            return {"ok": True, "message": "Installing the bundle. Follow it in Tasks.", "task": r["task"]}
        if k == "gpu_power":
            r = await gpu_power_set(PowerBody(mode=a["mode"]))
            return {"ok": True, "message": f"GPU power set to {a['mode']}: " + ("; ".join(r["applied"]) or "no GPU power controls found")}
        if k == "pull_model":
            r = await ollama_pull(ModelName(name=a["name"]))
            return {"ok": True, "message": f"Downloading {a['name']}.", "task": r["task"]}
        if k == "create_video":
            r = await create_video(a["topic"], a.get("aspect", "9:16"))
            return {"ok": True, "message": "Making the video. It will be saved in Shared media > videos.", "task": r["task"]}
        if k == "add_domain":
            r = await add_domain(DomainBody(domain=a["domain"], target=a["target"]))
            return {"ok": True, "message": r["message"] + (". " + r["warning"] if r["warning"] else "")}
    except HTTPException as e:
        return {"ok": False, "message": str(e.detail)}
    except Exception as e:
        return {"ok": False, "message": str(e)}
    return {"ok": False, "message": "Unknown action"}


class ConfirmBody(BaseModel):
    id: str


@app.post("/api/assistant/confirm")
async def assistant_confirm(body: ConfirmBody):
    return await run_proposal(body.id)


class AssistantCfg(BaseModel):
    provider: str = "ollama"
    model: str = ""
    base_url: str = ""
    api_key: str = ""


@app.get("/api/assistant/config")
async def assistant_cfg_get():
    cfg = load_config()
    a = dict(cfg.get("assistant", {"provider": "ollama", "model": ""}))
    if a.get("api_key"):
        a["api_key"] = "set"
    base, model, _ = await _llm_target(cfg)
    a["active_model"] = model
    return a


@app.post("/api/assistant/config")
async def assistant_cfg_set(body: AssistantCfg):
    if body.provider not in ("ollama", "llama", "openai"):
        raise HTTPException(400, "provider must be ollama, llama or openai")
    cfg = load_config()
    old = cfg.get("assistant", {})
    cfg["assistant"] = {"provider": body.provider, "model": body.model.strip(), "base_url": body.base_url.strip(),
                        "api_key": old.get("api_key", "") if body.api_key == "set" else body.api_key.strip()}
    save_config(cfg)
    return {"ok": True}


# --------------------------------------------------------------------------------------
# Make a video with MoneyPrinterTurbo's API, then copy it to the shared media folder
# --------------------------------------------------------------------------------------
MPT_API = "http://127.0.0.1:8080/api/v1"


async def create_video(topic: str, aspect: str = "9:16") -> dict:
    conf = DATA_DIR / "moneyprinter" / "config.toml"
    if conf.exists() and re.search(r'^\s*pexels_api_keys\s*=\s*\[\s*\]', conf.read_text(), re.M):
        raise HTTPException(400, "Add a free Pexels API key in MoneyPrinterTurbo's settings first (Basic settings > Pexels API key).")
    t = new_task("video", f"Video: {topic[:50]}")
    t["progress"] = 0

    async def run():
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(MPT_API + "/videos", json={"video_subject": topic, "video_aspect": aspect, "subtitle_enabled": True})
                if r.status_code >= 400:
                    raise RuntimeError(f"MoneyPrinterTurbo API: HTTP {r.status_code} {r.text[:200]}")
                tid = r.json()["data"]["task_id"]
                task_log(t, f"MoneyPrinterTurbo task {tid}")
                for _ in range(720):
                    await asyncio.sleep(5)
                    d = (await c.get(f"{MPT_API}/tasks/{tid}")).json().get("data", {})
                    t["progress"] = int(d.get("progress") or 0)
                    if d.get("state") == -1:
                        raise RuntimeError("MoneyPrinterTurbo reported a failure. Open its web UI to see why.")
                    if d.get("videos") or (d.get("state") == 1 and t["progress"] >= 100):
                        break
                else:
                    raise RuntimeError("Timed out after 1 hour")
            src = DATA_DIR / "moneyprinter" / "storage" / "tasks" / tid
            dest = DATA_DIR / "shared-media" / "videos"
            dest.mkdir(parents=True, exist_ok=True)
            n = 0
            for f in sorted(src.glob("final-*.mp4")):
                name = re.sub(r"[^\w\- ]", "", topic)[:40].strip().replace(" ", "_") or "video"
                shutil.copy2(f, dest / f"{name}_{tid[:6]}_{f.stem[-1]}.mp4")
                n += 1
            task_done(t, True, f"Done: {n} video(s) saved to {dest}")
            await notify(f"🎬 Video ready: {topic[:80]}")
        except Exception as e:
            task_done(t, False, str(e))
    asyncio.create_task(run())
    return {"task": t["id"]}


# --------------------------------------------------------------------------------------
# Bundles: several apps installed together, already wired to each other
# --------------------------------------------------------------------------------------
@app.get("/api/bundles")
async def bundles_list():
    out = []
    for b in load_bundles():
        inst = [a for a in b["apps"] if (APPS_DIR / a / "docker-compose.yml").exists()]
        out.append({**b, "installed": inst})
    return out


class BundleBody(BaseModel):
    gpu: str = "auto"
    inputs: dict[str, dict[str, str]] = {}


@app.post("/api/bundles/{bid}/install")
async def bundle_install(bid: str, body: BundleBody):
    b = next((x for x in load_bundles() if x["id"] == bid), None)
    if not b:
        raise HTTPException(404, "No such bundle")
    t = new_task("bundle", f"Bundle: {b['name']}")
    t["progress"] = 0

    async def run():
        ok = True
        todo = b["apps"]
        for i, aid in enumerate(todo):
            if (APPS_DIR / aid / "docker-compose.yml").exists():
                task_log(t, f"{aid}: already installed, skipped")
                continue
            try:
                r = await install_app(aid, InstallBody(gpu=body.gpu, inputs=body.inputs.get(aid, {}), env=(b.get("env") or {}).get(aid, {})))
                task_log(t, f"{aid}: installing...")
                sub = TASKS[r["task"]]
                while sub["status"] == "running":
                    await asyncio.sleep(2)
                task_log(t, f"{aid}: {'done' if sub['status'] == 'done' else 'FAILED - ' + sub['message']}")
                ok = ok and sub["status"] == "done"
            except HTTPException as e:
                task_log(t, f"{aid}: skipped - {e.detail}")
                ok = False
            t["progress"] = int(100 * (i + 1) / len(todo))
        task_done(t, ok, (b.get("notes") or "All apps installed") if ok else "Some apps failed, see the log above")
    asyncio.create_task(run())
    return {"task": t["id"]}


# --------------------------------------------------------------------------------------
# Live logs (Server-Sent Events): apps, panel services, Docker, the HTTPS proxy and system errors
# --------------------------------------------------------------------------------------
LOG_SERVICES = {"ai-panel": "AI Panel", "ollama": "Ollama", "ai-llama": "llama.cpp server", "docker": "Docker"}


@app.get("/api/logs/sources")
async def log_sources(request: Request):
    admin = request.state.role == "admin"
    out = []
    for d in sorted(APPS_DIR.iterdir()) if APPS_DIR.exists() else []:
        if (d / "docker-compose.yml").exists() and (admin or d.name in request.state.apps):
            out.append({"id": "app:" + d.name, "name": d.name, "group": "Apps"})
    if admin:
        out += [{"id": "svc:" + k, "name": v, "group": "Services"} for k, v in LOG_SERVICES.items()]
        out += [{"id": "proxy", "name": "HTTPS proxy (Caddy)", "group": "Services"},
                {"id": "errors", "name": "All system errors", "group": "System"},
                {"id": "kernel", "name": "Kernel (GPU drivers, disks)", "group": "System"}]
    return out


def _log_cmd(source: str, tail: int) -> list[str]:
    if source.startswith("app:") and APP_ID_RE.match(source[4:]) and (APPS_DIR / source[4:] / "docker-compose.yml").exists():
        return compose_cmd(source[4:]) + ["logs", "-f", "--tail", str(tail), "--no-color", "--timestamps"]
    if source.startswith("svc:") and source[4:] in LOG_SERVICES:
        unit = source[4:] + ("" if source[4:] == "docker" else ".service")
        return ["journalctl", "-u", unit, "-f", "-n", str(tail), "--no-pager", "-o", "short-iso"]
    if source == "proxy":
        return ["docker", "logs", "-f", "--tail", str(tail), "aipanel-proxy"]
    if source == "errors":
        return ["journalctl", "-p", "err", "-f", "-n", str(tail), "--no-pager", "-o", "short-iso"]
    if source == "kernel":
        return ["journalctl", "-k", "-f", "-n", str(tail), "--no-pager", "-o", "short-iso"]
    raise HTTPException(400, "Unknown log source")


@app.get("/api/logs/stream")
async def log_stream(source: str, request: Request, tail: int = 200):
    if request.state.role != "admin" and not (source.startswith("app:") and source[4:] in request.state.apps):
        raise HTTPException(403, "Only an admin can do this")
    cmd = _log_cmd(source, max(10, min(tail, 5000)))
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        raise HTTPException(400, f"{cmd[0]} is not available on this server")

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), 15)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": ping\n\n"
                    continue
                if not line:
                    yield "event: end\ndata: (log stream ended)\n\n"
                    break
                yield "data: " + line.decode(errors="replace").rstrip("\n").replace("\r", "") + "\n\n"
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------------------
# Phone: installable web app (PWA)
# --------------------------------------------------------------------------------------
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#0b1220"/>'
            '<text x="256" y="330" font-size="300" text-anchor="middle">🧠</text></svg>')


@app.get("/manifest.webmanifest")
async def manifest():
    return JSONResponse({"name": "AI Server Control", "short_name": "AI Server", "start_url": "/", "display": "standalone",
                         "background_color": "#0b1220", "theme_color": "#0b1220",
                         "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]},
                        media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon_svg():
    return Response(ICON_SVG, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


# --------------------------------------------------------------------------------------
# Web terminal (xterm.js <-> PTY over WebSocket)
# --------------------------------------------------------------------------------------
@app.websocket("/ws/terminal")
async def terminal(ws: WebSocket):
    user = check_token(ws.cookies.get(COOKIE))
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if not user or (origin and origin.split("://", 1)[-1] != host) or user_role(load_config(), user) != "admin":
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
