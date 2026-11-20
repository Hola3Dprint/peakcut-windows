@echo off
setlocal
cd /d "%~dp0"
set "OPENCLAW_HOME=%~dp0openclaw-state"
set "OPENCLAW_STATE_DIR=%~dp0openclaw-state"
set "OPENCLAW_CONFIG_PATH=%~dp0openclaw-state\openclaw.json"
set "OPENCLAW_GATEWAY_PORT=18794"
set "OPENCLAW_SKIP_CHANNELS=1"
rem PeakCut clip checks do not need per-turn diagnostic traces in the agent database.
set "OPENCLAW_TRAJECTORY=0"
set "OPENAI_API_KEY="
set "CODEX_API_KEY="
set "OPENAI_BASE_URL="
set "OPENAI_TOKEN="
set "CODEX_HOME=%~dp0openclaw-state\codex"
set /p OPENCLAW_GATEWAY_TOKEN=<"%~dp0secrets\gateway-token.txt"
set "PATH=%~dp0tools\node-v24.22.3-win-x64;%PATH%"
"%~dp0tools\node-v24.22.3-win-x64\node.exe" "%~dp0tools\openclaw\node_modules\openclaw\openclaw.mjs" %*
exit /b %errorlevel%
