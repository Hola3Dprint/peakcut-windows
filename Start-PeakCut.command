#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PATH="/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:$PATH"

if ! curl -fsS --max-time 3 http://127.0.0.1:11434/api/tags >/dev/null; then
    OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_CONTEXT_LENGTH=8192 \
        nohup ollama serve </dev/null >>ollama.log 2>&1 &
    attempts=0
    until curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 15 ]; then
            printf '%s\n' 'Ollama did not start. Check ollama.log.' >&2
            exit 1
        fi
        sleep 1
    done
fi

.venv/bin/python -m tools.peakcut_handoff node start
dashboard=$(.venv/bin/python -c 'from studio import config; print(f"http://127.0.0.1:{config.PORT}/app/settings/devices")')
printf 'PeakCut is running at %s\n' "$dashboard"
if [ "${1:-}" != "--dashboard-only" ]; then
    open "$dashboard"
fi
