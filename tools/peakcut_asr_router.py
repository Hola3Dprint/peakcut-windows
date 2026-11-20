"""Route PeakCut's loopback transcription requests to an available worker."""

import asyncio
import hmac
import logging
import subprocess
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from time import monotonic

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from studio import config
from studio.asr_worker import MAX_BODY, MAX_PIECE, TOKEN


@dataclass
class Worker:
    url: str
    token: str
    busy: bool = False
    retry_at: float = 0
    pieces: int = 0


workers = {
    "windows": Worker("http://127.0.0.1:9002", TOKEN),
    "mac": Worker("http://127.0.0.1:9001", ""),
}
available = asyncio.Condition()
log = logging.getLogger("uvicorn.error")


async def supervise_mac_tunnel(target):
    secrets = config.ROOT / "secrets"
    args = [
        "ssh.exe", "-4", "-N", "-T", "-i", str(secrets / "mac-transcribe-ed25519"),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={secrets / 'mac-asr-known-hosts'}",
        "-o", "KexAlgorithms=curve25519-sha256", "-o", "ConnectTimeout=5",
        "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        "-L", "127.0.0.1:9001:127.0.0.1:8901", target,
    ]
    while True:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await process.wait()
        except OSError as exc:
            log.warning("The Mac transcription tunnel could not start: %s", exc)
        finally:
            if process and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                await process.wait()
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app):
    secrets = config.ROOT / "secrets"
    workers["mac"].token = (secrets / "mac-asr-token.txt").read_text().strip()
    target = (secrets / "mac-asr-target.txt").read_text().strip()
    tunnel = asyncio.create_task(supervise_mac_tunnel(target))
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            app.state.client = client
            yield
    finally:
        tunnel.cancel()
        with suppress(asyncio.CancelledError):
            await tunnel


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def acquire(deadline):
    async with available:
        while True:
            now = monotonic()
            if now >= deadline:
                raise HTTPException(503, "Transcription workers are busy")
            for worker in workers.values():
                if not worker.busy and worker.retry_at <= now:
                    worker.busy = True
                    return worker
            if not any(worker.busy for worker in workers.values()):
                raise HTTPException(503, "Transcription workers are unavailable")
            try:
                await asyncio.wait_for(available.wait(), deadline - now)
            except TimeoutError as exc:
                raise HTTPException(503, "Transcription workers are busy") from exc


@app.get("/healthz")
def health():
    return {
        "ok": True,
        "provider": "parallel",
        "pieces": sum(worker.pieces for worker in workers.values()),
        "workers": {
            name: {
                "pieces": worker.pieces,
                "busy": worker.busy,
                "retrying": worker.retry_at > monotonic(),
            }
            for name, worker in workers.items()
        },
    }


@app.post("/decode")
async def decode(request: Request):
    if not TOKEN or not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + TOKEN):
        raise HTTPException(401, "no")
    lengths = request.headers.get("x-lens", "")
    try:
        counts = [int(part) for part in lengths.split(",") if part]
    except ValueError as exc:
        raise HTTPException(400, "bad lengths") from exc
    if not counts or len(counts) > 256 or any(n <= 0 or n > MAX_PIECE for n in counts):
        raise HTTPException(400, "bad lengths")
    expected = 2 * sum(counts)
    if expected > MAX_BODY:
        raise HTTPException(413, "too much")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > expected:
            raise HTTPException(400, "lengths do not match the audio")
        body.extend(chunk)
    if len(body) != expected:
        raise HTTPException(400, "lengths do not match the audio")
    audio = bytes(body)

    deadline = monotonic() + 108
    while True:
        worker = await acquire(deadline)
        try:
            remaining = max(0.1, deadline - monotonic())
            async with asyncio.timeout(remaining):
                response = await request.app.state.client.post(
                    worker.url + "/decode",
                    content=audio,
                    headers={"Authorization": "Bearer " + worker.token, "X-Lens": lengths},
                    timeout=httpx.Timeout(remaining, connect=2),
                )
            response.raise_for_status()
            decoded = response.json()
            if len(decoded["pieces"]) != len(counts):
                raise ValueError("Incomplete transcription")
            worker.pieces += len(counts)
            return decoded
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            worker.retry_at = monotonic() + 15
        finally:
            async with available:
                worker.busy = False
                available.notify_all()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9000, log_level="warning")
