$ErrorActionPreference = 'Stop'
$gatewayPort = 18794
$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $gatewayPort -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $expected = Join-Path $PSScriptRoot 'tools\node-v24.22.3-win-x64\node.exe'
    if ($process.ExecutablePath -ne $expected -or $process.CommandLine -notlike '*openclaw.mjs*gateway run*') {
        throw 'Port 18794 belongs to another process. PeakCut left it running.'
    }
    Stop-Process -Id $process.ProcessId -ErrorAction Stop
    Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
}
