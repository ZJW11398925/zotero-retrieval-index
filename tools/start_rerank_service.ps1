# 启动本地 GPU 重排服务（无窗口，日志写入 opt_eval\t14\rerank_service.log）
# 用法：powershell -File tools\start_rerank_service.ps1 [-Port 8898]
# 说明：用 Win32_Process.Create 派生（完全脱离当前 shell 的 stdio 句柄，不会阻塞调用方/计划任务）。
param([int]$Port = 8898)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$svc = Join-Path $root 'rerank_service.py'
$log = 'D:\workspace\opt_eval\t14\rerank_service.log'
New-Item -ItemType Directory -Force (Split-Path $log -Parent) | Out-Null

# 已在运行则直接返回
try {
    $h = curl.exe -sS --noproxy '*' --max-time 3 ("http://127.0.0.1:{0}/health" -f $Port) 2>$null
    if ($h -and $h -match '"ok":\s*true') { Write-Output "已在运行: $h"; exit 0 }
} catch {}

$py = 'C:\Program Files\Python313\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$cmdLine = '"{0}" "{1}" --port {2} --log "{3}"' -f $py, $svc, $Port, $log
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = $root }
if (-not $r -or $r.ReturnValue -ne 0) {
    Write-Output ("启动失败：Win32_Process.Create 返回 " + ($r.ReturnValue))
    exit 1
}
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $h = curl.exe -sS --noproxy '*' --max-time 3 ("http://127.0.0.1:{0}/health" -f $Port) 2>$null
        if ($h -and $h -match '"ok":\s*true') { Write-Output ("已启动(PID " + $r.ProcessId + "): " + $h); exit 0 }
    } catch {}
}
Write-Output '启动超时，请查看日志: ' + $log
exit 1
