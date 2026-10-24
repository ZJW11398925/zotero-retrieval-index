# start_rerank_service.ps1 — start the optional local reranker service in the background
#
# Usage: powershell -ExecutionPolicy Bypass -File tools\start_rerank_service.ps1 [-Port 8898]
#
# Requires Python 3.10+ with torch/transformers installed (see tools/rerank_service.py).
# The model directory is taken from $env:RERANK_MODEL_DIR (default: ../rr_model).

param([int]$Port = 8898)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$svc = Join-Path $root 'rerank_service.py'
$log = Join-Path $env:TEMP 'rerank_service.log'

try {
    $h = curl.exe -sS --noproxy '*' --max-time 3 ("http://127.0.0.1:{0}/health" -f $Port) 2>$null
    if ($h -and $h -match '"ok":\s*true') { Write-Output ("Already running: " + $h); exit 0 }
} catch {}

$py = (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $py) { Write-Output 'Python not found in PATH'; exit 1 }
$cmdLine = '"{0}" "{1}" --port {2} --log "{3}"' -f $py, $svc, $Port, $log
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = $root }
if (-not $r -or $r.ReturnValue -ne 0) { Write-Output ('Failed to start: ' + $r.ReturnValue); exit 1 }
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $h = curl.exe -sS --noproxy '*' --max-time 3 ("http://127.0.0.1:{0}/health" -f $Port) 2>$null
        if ($h -and $h -match '"ok":\s*true') { Write-Output ("Started (PID " + $r.ProcessId + "): " + $h); exit 0 }
    } catch {}
}
Write-Output ('Startup timed out; see log: ' + $log)
exit 1
