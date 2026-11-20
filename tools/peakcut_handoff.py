"""Move a local PeakCut workspace between the paired desktop and Mac over SSH."""
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import Request, urlopen

from studio import config, devices

ROOT = config.ROOT
DATA = config.DATA_DIR.resolve()
BASE = f"http://127.0.0.1:{config.PORT}"
RESERVE = 5 * 1024 ** 3
MEDIA = {".mp4", ".mkv", ".webm", ".mov", ".ts", ".jpg"}
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def http(path, action=None):
    headers = {"Content-Type": "application/json", "X-PeakCut-Control": devices.control_token()}
    body = json.dumps({"action": action}).encode() if action else None
    with urlopen(Request(BASE + path, data=body, headers=headers), timeout=8) as response:
        return json.load(response)


def health():
    try:
        data = http("/healthz")
    except (URLError, TimeoutError):
        return None
    if data.get("device") != devices.HOST:
        raise RuntimeError("The local port belongs to another application.")
    return data


def shut_down():
    if health() is None:
        return
    path = "/api/devices/transfer"
    http(path, "drain")
    until = time.monotonic() + 120
    while http(path, "status")["busy"]:
        if time.monotonic() >= until:
            raise RuntimeError("Recording or uploading did not finish in time.")
        time.sleep(2)
    http(path, "exit")
    until = time.monotonic() + 30
    while health() is not None:
        if time.monotonic() >= until:
            raise RuntimeError("PeakCut is still listening.")
        time.sleep(1)


def boot():
    if health() is not None:
        return
    flags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    environment = os.environ.copy()
    if sys.platform == "darwin":
        ffmpeg_bin = config.env("PEAKCUT_MAC_FFMPEG_BIN", "/opt/homebrew/opt/ffmpeg-full/bin")
        environment["PATH"] = os.pathsep.join((ffmpeg_bin, "/opt/homebrew/bin", environment.get("PATH", "")))
    with (ROOT / "handoff-server.log").open("ab") as log:
        process = subprocess.Popen([sys.executable, "-u", "-m", "studio.server"], cwd=ROOT,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=flags, start_new_session=True, env=environment)
    until = time.monotonic() + 90
    while health() is None:
        if process.poll() is not None or time.monotonic() >= until:
            raise RuntimeError("The destination dashboard has not started.")
        time.sleep(1)


def is_local_database():
    return config.DATABASE_URL == f"sqlite:///{(config.DATA_DIR / 'peakcut.db').as_posix()}"


def portable_files():
    for folder, dirs, files in os.walk(DATA):
        place = Path(folder)
        rel = place.relative_to(DATA)
        if not rel.parts:
            dirs[:] = [name for name in dirs if name not in ("models", "tmp", "fonts")]
        dirs[:] = [name for name in dirs if not (place / name).is_symlink()]
        if "buffer" in dirs:
            job = devices._read(place / "job.json")
            if job.get("status") not in ("running", "live") and not job.get("recover"):
                dirs.remove("buffer")
        for name in files:
            path = place / name
            if (name in ("peakcut.db-wal", "peakcut.db-shm", "ai.json", ".transfer-complete")
                    or name.endswith((".tmp", ".src.ts"))
                    or path.is_symlink() or not path.is_file()):
                continue
            if path.resolve().is_relative_to(DATA):
                yield path, rel / name


def incoming(transfer):
    if not re.fullmatch(r"[0-9a-f]{32}", transfer):
        raise ValueError("Invalid transfer identifier.")
    return DATA.with_name(DATA.name + ".incoming-" + transfer)


def send_workspace():
    if health() is not None or devices.active():
        raise RuntimeError("Close the source before taking a workspace snapshot.")
    with sqlite3.connect(DATA / "peakcut.db") as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("The source database did not pass its integrity check.")
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for path, rel in portable_files():
            member = archive.gettarinfo(str(path), arcname=rel.as_posix())
            if path.suffix.lower() in MEDIA:
                member.pax_headers["peakcut.sha256"] = digest(path)
            with path.open("rb") as content:
                archive.addfile(member, content)


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def clone_existing(destination, rel, member):
    checksum = member.pax_headers.get("peakcut.sha256")
    if sys.platform != "darwin" or not checksum:
        return False
    source = DATA.joinpath(*rel.parts)
    if (not source.is_file() or not source.resolve().is_relative_to(DATA)
            or source.stat().st_size != member.size or digest(source) != checksum):
        return False
    # APFS can copy an unchanged media file without allocating a second copy.
    result = subprocess.run(["/bin/cp", "-c", str(source), str(destination)], capture_output=True)
    if result.returncode and destination.exists():
        raise RuntimeError("The destination could not make a complete local clone.")
    return result.returncode == 0


def receive_workspace(transfer):
    if health() is not None or devices.active():
        raise RuntimeError("Close the destination before receiving the workspace.")
    dest = incoming(transfer)
    dest.mkdir(mode=0o700)
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
        for member in archive:
            rel = PurePosixPath(member.name)
            if (not member.isfile() or rel.is_absolute() or ".." in rel.parts
                    or any(":" in part or "\\" in part for part in rel.parts)):
                raise RuntimeError("The snapshot has an unexpected file name.")
            path = dest.joinpath(*rel.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            if clone_existing(path, rel, member):
                continue
            if shutil.disk_usage(dest).free - member.size < RESERVE:
                raise RuntimeError("The destination is running out of free space.")
            with archive.extractfile(member) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
    db = dest / "peakcut.db"
    if not db.is_file():
        raise RuntimeError("The snapshot is missing its database.")
    with sqlite3.connect(db) as conn:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RuntimeError("The received database did not pass its integrity check.")
    (dest / ".transfer-complete").touch()


def install_workspace(transfer):
    if health() is not None or devices.active():
        raise RuntimeError("Close the destination before installing the snapshot.")
    stage = incoming(transfer)
    if not (stage / ".transfer-complete").is_file():
        raise RuntimeError("The snapshot transfer is incomplete.")
    local_ai = DATA / "ai.json"
    if local_ai.is_file():
        shutil.copy2(local_ai, stage / "ai.json")
    for path in (DATA / "models").rglob("*"):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(DATA):
            continue
        dest = stage / path.relative_to(DATA)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, dest)
        except OSError:
            shutil.copy2(path, dest)
    # Reconnect old render sources already present on this computer.
    for path in DATA.rglob("*.src.ts"):
        dest = stage / path.relative_to(DATA)
        if dest.parent.is_dir() and not dest.exists() and path.resolve().is_relative_to(DATA):
            try:
                os.link(path, dest)
            except OSError:
                pass
    backup = DATA.with_name(DATA.name + ".backup-" + transfer)
    DATA.rename(backup)
    try:
        stage.rename(DATA)
    except OSError:
        backup.rename(DATA)
        raise


def node(action, *args):
    if not devices.control_token() or not is_local_database():
        raise RuntimeError("This installation needs paired handoff settings and its own SQLite database.")
    if action == "ping":
        print(json.dumps(health() or {}))
    elif action == "heartbeat":
        devices.record_desktop_heartbeat()
    elif action == "state":
        print(json.dumps(dict(host=devices.HOST, coordinator=devices.coordinator(), running=bool(health()),
                              bytes=sum(path.stat().st_size for path, _ in portable_files()),
                              free=shutil.disk_usage(DATA).free)))
    elif action == "stop":
        shut_down()
    elif action == "start":
        boot()
    elif action == "role":
        devices.set_coordinator(args[0] if args[0] != "none" else None)
    elif action == "send":
        send_workspace()
    elif action == "receive":
        receive_workspace(args[0])
    elif action == "install":
        install_workspace(args[0])
    else:
        raise ValueError("Unknown handoff operation.")


def command(host, *args):
    if host == devices.HOST:
        return [sys.executable, "-m", "tools.peakcut_handoff", "node", *args]
    secrets = Path(config.env("PEAKCUT_MAC_SSH_DIR") or ROOT / "secrets")
    target = (secrets / "mac-asr-target.txt").read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+", target):
        raise ValueError("Invalid paired Mac address.")
    path = config.env("PEAKCUT_MAC_INSTALL", "Library/Application Support/PeakCut Studio")
    remote = 'cd "$HOME"/' + shlex.quote(path) + " && " + shlex.join(
        [".venv/bin/python", "-m", "tools.peakcut_handoff", "node", *args])
    return ["ssh.exe", "-4", "-T", "-i", str(secrets / "mac-transcribe-ed25519"),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
            "-o", f"UserKnownHostsFile={secrets / 'mac-asr-known-hosts'}", target, remote]


def run(host, *args, timeout=None):
    return subprocess.run(command(host, *args), cwd=ROOT, check=True, capture_output=True, text=True,
                          creationflags=NO_WINDOW, timeout=timeout).stdout


def status(message):
    print(message, flush=True)
    devices.HANDOFF_STATUS.write_text(json.dumps({"message": message, "at": time.time()}), encoding="utf-8")


def handoff(target):
    if devices.HOST != "desktop" or target not in devices.LABELS:
        raise ValueError("Use the paired desktop to move PeakCut.")
    source = "desktop" if target == "mac" else "mac"
    transfer = uuid.uuid4().hex
    lock = ROOT / "handoff.lock"
    with lock.open("x", encoding="utf-8") as file:
        file.write(transfer)
    began = False
    try:
        src, dst = [json.loads(run(h, "state")) for h in (source, target)]
        if (src["host"], dst["host"]) != (source, target) or src["coordinator"] != source or dst["coordinator"] != source:
            raise RuntimeError("The devices disagree about which one runs PeakCut.")
        reusable = dst["bytes"] if target == "mac" else 0
        if src["bytes"] + RESERVE > dst["free"] + reusable:
            raise RuntimeError("The destination does not have room for the workspace.")
        status(f"Saving work on the {devices.LABELS[source]}.")
        began = True
        run(source, "stop")
        run(target, "stop")
        # An already stopped standby server retains its original owner marker.
        run(source, "role", "none")
        status("Transferring the queue, account settings and saved clips.")
        with subprocess.Popen(command(source, "send"), cwd=ROOT, stdout=subprocess.PIPE, creationflags=NO_WINDOW) as producer:
            try:
                subprocess.run(command(target, "receive", transfer), cwd=ROOT, stdin=producer.stdout, check=True,
                               creationflags=NO_WINDOW)
            finally:
                producer.stdout.close()
            if producer.wait() != 0:
                raise RuntimeError("The source did not finish sending the workspace.")
        run(target, "install", transfer)
        run(source, "role", target)
        run(target, "role", target)
        run(target, "start")
        run(source, "start")
        status(f"{devices.LABELS[target]} is now coordinating PeakCut.")
    except Exception:
        if began:
            try:
                dst = json.loads(run(target, "state"))
                if dst["coordinator"] != target:
                    run(target, "role", source)
                    run(source, "role", source)
                    run(source, "start")
                    run(target, "start")
                    status("The handoff stopped. PeakCut stayed on its original device.")
                else:
                    status("The destination owns the workspace. Check that its dashboard is running.")
            except Exception:
                status("The handoff stopped. Verify both devices before choosing where to resume PeakCut.")
        else:
            status("The handoff could not start. Check the pairing and the destination’s free space.")
        raise
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1] == "node":
        node(*sys.argv[2:])
    else:
        handoff(sys.argv[1])
