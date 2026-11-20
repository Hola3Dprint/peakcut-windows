"""Connect the desktop to two Mac loopback services through a restricted SSH key."""
import asyncio
import os
import re
import socket
import subprocess
import sys
import threading
from contextlib import suppress

SERVICES = {"peakcut-ollama": (11435, 11434), "peakcut-dashboard": (18765, 8866)}


def forward():
    service = SERVICES.get(os.environ.get("SSH_ORIGINAL_COMMAND"))
    if service is None:
        raise SystemExit(126)
    with socket.create_connection(("127.0.0.1", service[1]), timeout=5) as connection:
        connection.settimeout(None)

        def send():
            try:
                while block := os.read(0, 65536):
                    connection.sendall(block)
            except OSError:
                pass
            finally:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_WR)

        threading.Thread(target=send, daemon=True).start()
        try:
            while block := connection.recv(65536):
                sys.stdout.buffer.write(block)
                sys.stdout.buffer.flush()
        except OSError:
            pass


async def pump(reader, writer):
    while block := await reader.read(65536):
        writer.write(block)
        await writer.drain()
    if not writer.is_closing() and writer.can_write_eof():
        writer.write_eof()


async def serve():
    from studio import config

    secrets = config.ROOT / "secrets"
    target = (secrets / "mac-asr-target.txt").read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+", target):
        raise ValueError("Invalid paired Mac address.")
    args = [
        "ssh.exe", "-F", "none", "-4", "-T", "-i", str(secrets / "mac-device-ed25519"),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={secrets / 'mac-asr-known-hosts'}",
        "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        target,
    ]
    slots = asyncio.Semaphore(16)

    def handle(service):
        async def relay(reader, writer):
            try:
                async with slots:
                    process = await asyncio.create_subprocess_exec(
                        *args, service, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    tasks = [asyncio.create_task(pump(reader, process.stdin)),
                             asyncio.create_task(pump(process.stdout, writer))]
                    try:
                        await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        if process.returncode is None:
                            with suppress(ProcessLookupError):
                                process.terminate()
                        await process.wait()
            except OSError:
                pass
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
        return relay

    servers = []
    try:
        for service, (port, _remote) in SERVICES.items():
            servers.append(await asyncio.start_server(handle(service), "127.0.0.1", port))
        await asyncio.gather(*(server.serve_forever() for server in servers))
    finally:
        for server in servers:
            server.close()
            await server.wait_closed()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        asyncio.run(serve())
    elif sys.argv[1:] == ["forward"]:
        forward()
    else:
        raise SystemExit(126)
