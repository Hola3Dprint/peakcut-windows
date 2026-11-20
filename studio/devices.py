"""Local device roles and private worker status for the owner dashboard."""
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from . import config

HOST = config.env("PEAKCUT_DEVICE", "mac" if sys.platform == "darwin" else "desktop")
LABELS = {"desktop": "Desktop", "mac": "Mac"}
WORKERS = ("desktop", "mac", "both")
SETTINGS = Path(config.env("PEAKCUT_DEVICE_SETTINGS_FILE") or config.ROOT / "device-settings.json")
ROLE = Path(config.env("PEAKCUT_COORDINATOR_FILE") or config.ROOT / "coordinator.json")
HANDOFF_STATUS = config.ROOT / "handoff-status.json"
DESKTOP_HEARTBEAT = config.ROOT / "desktop-heartbeat.json"
_lock = threading.RLock()


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def coordinator():
    if not ROLE.exists():
        return HOST
    return _read(ROLE).get("active")


def active():
    return coordinator() == HOST


def set_coordinator(host):
    if host is not None and host not in LABELS:
        raise ValueError("Unknown coordinator.")
    with _lock:
        temp = ROLE.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"active": host}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(ROLE)


def control_token():
    return config._secret("PEAKCUT_HANDOFF_TOKEN", "PEAKCUT_HANDOFF_TOKEN_FILE")


def start_handoff(target):
    if target not in LABELS or target == coordinator():
        raise ValueError("Choose the other device.")
    if config.env("PEAKCUT_HANDOFF_ENABLED") != "1" or HOST != "desktop" or not control_token():
        raise ValueError("Start the handoff from the paired desktop.")
    if (config.ROOT / "handoff.lock").exists():
        raise ValueError("A device handoff is already running.")
    flags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    with (config.ROOT / "handoff.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-m", "tools.peakcut_handoff", target], cwd=config.ROOT,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags, start_new_session=True)


def routing():
    default = "both" if HOST == "desktop" else "mac"
    return {"speech": default, "ai": "saved", **_read(SETTINGS)}


def ollama_url():
    url = config.env("PEAKCUT_MAC_OLLAMA_URL", "http://127.0.0.1:11434" if HOST == "mac" else "").rstrip("/")
    return url if re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}", url) else ""


def local_model():
    if routing()["ai"] == "mac" and (url := ollama_url()):
        return dict(provider="ollama", models=[config.env("PEAKCUT_MAC_MODEL", "qwen3.5:4b")],
                    base_url=url + "/v1", key="", mac_local=True)
    return None


def save(patch):
    with _lock:
        current = routing()
        if set(patch) - {"speech", "ai"}:
            raise ValueError("Unknown device setting.")
        if patch.get("speech", current["speech"]) != current["speech"] and not config.env("PEAKCUT_WORKER_ROUTER"):
            raise ValueError("This installation uses its own speech worker.")
        current.update(patch)
        if current["speech"] not in WORKERS or current["ai"] not in ("saved", "mac"):
            raise ValueError("Choose a supported processing mode.")
        if current["ai"] == "mac" and not ollama_url():
            raise ValueError("The Mac model service is not configured on this installation.")
        temp = SETTINGS.with_suffix(".tmp")
        temp.write_text(json.dumps(current, indent=2), encoding="utf-8")
        temp.replace(SETTINGS)


async def _paired_mac():
    if HOST != "desktop" or config.env("PEAKCUT_HANDOFF_ENABLED") != "1":
        return {}
    from tools import peakcut_handoff

    try:
        return json.loads(await asyncio.to_thread(peakcut_handoff.run, "mac", "ping", timeout=4))
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def record_desktop_heartbeat():
    if HOST != "mac":
        raise ValueError("The desktop heartbeat belongs on the Mac.")
    temp = DESKTOP_HEARTBEAT.with_suffix(".tmp")
    temp.write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    temp.replace(DESKTOP_HEARTBEAT)


def start_monitor():
    if HOST != "desktop" or config.env("PEAKCUT_HANDOFF_ENABLED") != "1":
        return
    from tools import peakcut_handoff

    def monitor():
        while True:
            try:
                peakcut_handoff.run("mac", "heartbeat", timeout=5)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            time.sleep(30)

    threading.Thread(target=monitor, daemon=True, name="device-monitor").start()


async def snapshot():
    other = "mac" if HOST == "desktop" else "desktop"
    speech = {
        "desktop": config.env("PEAKCUT_DESKTOP_WORKER_URL", "http://127.0.0.1:9002" if HOST == "desktop" else ""),
        "mac": config.env("PEAKCUT_MAC_WORKER_URL", "http://127.0.0.1:9001" if HOST == "desktop" else "http://127.0.0.1:8901"),
    }

    async def read(client, url, path):
        if not url:
            return {}
        try:
            result = await client.get(url.rstrip("/") + path)
            result.raise_for_status()
            value = result.json()
            return value if isinstance(value, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async with httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
        peer_url = config.env("PEAKCUT_PEER_DASHBOARD_URL", "")
        desktop, mac, ollama, peer = await asyncio.gather(
            read(client, speech["desktop"], "/healthz"), read(client, speech["mac"], "/healthz"),
            read(client, ollama_url(), "/api/tags"),
            read(client, peer_url, "/healthz") if peer_url else _paired_mac(),
        )
    model = config.env("PEAKCUT_MAC_MODEL", "qwen3.5:4b")
    ready_model = any(isinstance(m, dict) and m.get("name") == model for m in (ollama.get("models") or []))
    info = []
    heartbeat = _read(DESKTOP_HEARTBEAT).get("at", 0)
    heartbeat_recent = isinstance(heartbeat, (int, float)) and 0 <= time.time() - heartbeat < 75
    for name, status in (("desktop", desktop), ("mac", mac)):
        dashboard = (name == HOST or (name == other and peer.get("device") == other and bool(peer.get("ok")))
                     or (name == "desktop" and HOST == "mac" and heartbeat_recent))
        online = dashboard or bool(status.get("ok")) or (name == "mac" and bool(ollama))
        monitored = name == HOST or speech[name] or peer_url or (name == "desktop" and heartbeat)
        info.append(dict(id=name, name=LABELS[name], online=online, monitored=bool(monitored), dashboard=dashboard,
                         coordinator=name == coordinator(), speech=bool(status.get("ok")),
                         pieces=status.get("pieces", 0),
                         model=model if name == "mac" and ready_model else ""))
    return dict(host=HOST, coordinator=coordinator(), devices=info, routing=routing(),
                speech_switch=bool(config.env("PEAKCUT_WORKER_ROUTER", "")), mac_ai=bool(ollama_url()),
                model_ready=ready_model, handoff=HOST == "desktop" and config.env("PEAKCUT_HANDOFF_ENABLED") == "1",
                transfer=_read(HANDOFF_STATUS))
