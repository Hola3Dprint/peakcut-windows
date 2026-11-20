# Running PeakCut on Windows

Install Python 3.11 and put FFmpeg and FFprobe on your PATH. From the checkout in PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m studio.fetch_model
.\.venv\Scripts\python.exe -m studio.server
```

Open [the local dashboard](http://127.0.0.1:8765/app). Create a local `.env` from `.env.example` for settings. Keep `.env`, credentials, and account data on the computer.

`Start-PeakCut.ps1 -DashboardOnly` also starts the local dashboard and any provisioned speech workers. The full launcher and `Connect-ChatGPT.cmd` expect a separately provisioned, dedicated local OpenClaw profile, its runtime under `tools`, and its gateway credential in `secrets/gateway-token.txt`. The installed runtimes, model weights, and sign-in state are local setup items. The optional CUDA worker and Mac speech-worker router can both be configured on the local machine.

## Using two devices

Open **Settings → Devices** on the paired desktop. Select **Both devices** to distribute transcription between the two available speech workers. The coordinator manages recording, rendering and publishing.

Choose **Move to Mac** or **Move to Desktop** to move the active workspace. Keep both computers on until PeakCut confirms the move. It copies the queue, connected accounts and exported clips, and reconnects live recordings. Old source recordings stay on their original computer. When the Mac owns the workspace it can run without the desktop.

On the paired Mac, double-click `Start-PeakCut.command` to start the dashboard and existing Ollama installation. Choose **Local Mac AI** under **Settings → Devices** to use the local model for highlight selection, titles and image reviews. The model must already be installed in Ollama. To move the workspace back, open **Settings → Devices** on the paired desktop and select **Move to Desktop**.

The handoff expects a preconfigured SSH target and pinned host key, separate local control tokens, per-device coordinator files, and local SQLite storage. Keep the private node settings, workspace and backups outside Git.

On a paired desktop, **Local Mac AI** can use the Mac model even when Windows coordinates the workspace. The Windows launcher starts the bridge when the separate device key is present. The Mac must authorize that key with a restricted command that runs `tools/peakcut_mac_bridge.py forward`. The bridge listens on Windows loopback ports 11435 and 18765 for Ollama and the Mac dashboard. Point `PEAKCUT_MAC_OLLAMA_URL` and `PEAKCUT_PEER_DASHBOARD_URL` to those local ports.

```powershell
.\.venv\Scripts\python.exe tests/run.py
```
