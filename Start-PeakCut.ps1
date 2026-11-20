param([switch]$DashboardOnly)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$gateway = 'http://127.0.0.1:18794'
$dashboard = 'http://127.0.0.1:8765'


function Wait-Http($Uri, $Headers, $Process, $Log) {
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Uri -Headers $Headers -TimeoutSec 2 | Out-Null
            return
        } catch {
            if ($Process.HasExited) { break }
            Start-Sleep -Seconds 1
        }
    }
    throw "Could not reach $Uri. Check $Log."
}

function Start-PeakCutService($Port, $Module, $Log, $Arguments) {
    try {
        if (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { return }
        $modulePattern = ' -m ' + [regex]::Escape($Module) + '(\s+\d+)?\s*$'
        $starting = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
            $_.CommandLine -match $modulePattern
        }
        if (-not $starting) {
            Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList (@('-u','-m',$Module) + @($Arguments)) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput "$PSScriptRoot\$Log.log" -RedirectStandardError "$PSScriptRoot\$Log-error.log"
        }
    } catch {
        Write-Warning "The PeakCut service $Module could not start. Check $Log-error.log."
    }
}

$asrPort = 9000
if ((Test-Path -LiteralPath "$PSScriptRoot\tools\peakcut_asr_router.py") -and
    (Test-Path -LiteralPath "$PSScriptRoot\secrets\mac-asr-target.txt")) {
    $asrPort = 9002
    Start-PeakCutService 9000 'tools.peakcut_asr_router' 'asr-router' @()
}
if (Test-Path -LiteralPath "$PSScriptRoot\tools\asr-cuda\onnxruntime_providers_cuda.dll") {
    Start-PeakCutService $asrPort 'tools.peakcut_asr_cuda' 'asr-cuda' @($asrPort)
}
if (Test-Path -LiteralPath "$PSScriptRoot\secrets\mac-device-ed25519") {
    Start-PeakCutService 11435 'tools.peakcut_mac_bridge' 'mac-device' @()
}

try {
    $page = Invoke-WebRequest -UseBasicParsing $dashboard -TimeoutSec 8
    if ($page.Content -notmatch 'PeakCut') { throw 'Unexpected dashboard response.' }
} catch {
    if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 8765 is already in use. Check the PeakCut server logs.'
    }
    $server = Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" -ArgumentList '-m','studio.server' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput "$PSScriptRoot\server.log" -RedirectStandardError "$PSScriptRoot\server-error.log" -PassThru
    Wait-Http $dashboard @{} $server 'server.log and server-error.log'
}
Write-Host "PeakCut is running at $dashboard"

$lanAddress = '192.168.1.16'
$lanPort = 8766
$lanPython = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if ((Test-Path -LiteralPath $lanPython) -and
    (Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Wi-Fi' -IPAddress $lanAddress -ErrorAction SilentlyContinue) -and
    -not (Get-NetTCPConnection -LocalAddress $lanAddress -LocalPort $lanPort -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $lanPython -ArgumentList 'tools\lan_dashboard.py', "$lanAddress/24" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
if ($DashboardOnly) { return }

$token = (Get-Content (Join-Path $PSScriptRoot 'secrets\gateway-token.txt') -Raw).Trim()
$headers = @{ Authorization = "Bearer $token" }
try {
    $models = Invoke-RestMethod "$gateway/v1/models" -Headers $headers -TimeoutSec 2
} catch {
    if (Get-NetTCPConnection -LocalPort 18794 -State Listen -ErrorAction SilentlyContinue) {
        throw 'Port 18794 is already in use. Check the PeakCut gateway logs.'
    }
    $gatewayProcess = Start-Process -FilePath "$PSScriptRoot\OpenClaw-PeakCut.cmd" -ArgumentList 'gateway','run' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput "$PSScriptRoot\gateway.log" -RedirectStandardError "$PSScriptRoot\gateway-error.log" -PassThru
    Wait-Http "$gateway/v1/models" $headers $gatewayProcess 'gateway.log and gateway-error.log'
    $models = Invoke-RestMethod "$gateway/v1/models" -Headers $headers -TimeoutSec 2
}
if ('openclaw/peakcut' -notin $models.data.id) {
    throw 'The gateway did not advertise the dedicated PeakCut agent.'
}
