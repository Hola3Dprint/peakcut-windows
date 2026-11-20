@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-PeakCutGateway.ps1"
if errorlevel 1 exit /b 1
echo This connects only the dedicated PeakCut OpenClaw profile.
if /i "%~1"=="--phone" (
    echo Open the official OpenAI link on your phone and enter the displayed code.
    call "%~dp0OpenClaw-PeakCut.cmd" models auth login --provider openai --method device-code --agent peakcut --profile-id openai:peakcut-device
) else (
    echo Choose your ChatGPT account in the browser and approve token sharing.
    call "%~dp0OpenClaw-PeakCut.cmd" models auth login --provider openai --method siwc --agent peakcut --profile-id openai:peakcut-siwc
)
if errorlevel 1 (
    echo ChatGPT sign-in did not complete.
    exit /b 1
)
echo ChatGPT sign-in finished. Return to Codex to verify model access.
call "%~dp0Start-PeakCut.cmd"
