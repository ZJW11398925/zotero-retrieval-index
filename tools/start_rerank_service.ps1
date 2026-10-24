# 启动本地 GPU 重排服务（隐藏窗口，日志到 opt_eval\t14\rerank_service.log）
# 用法：powershell -File tools\start_rerank_service.ps1 [-Port 8898]
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

# 清理大小写重复的代理环境变量（PowerShell 5.1 Start-Process 的已知问题）
foreach ($n in @('no_proxy', 'http_proxy', 'https_proxy', 'all_proxy')) {
    try { [Environment]::SetEnvironmentVariable($n, $null) } catch {}
}

Start-Process -FilePath 'python' -ArgumentList $svc, '--port', $Port `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError ($log + '.err')
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 2
    try {
        $h = curl.exe -sS --noproxy '*' --max-time 3 ("http://127.0.0.1:{0}/health" -f $Port) 2>$null
        if ($h -and $h -match '"ok":\s*true') { Write-Output "已启动: $h"; exit 0 }
    } catch {}
}
Write-Output '启动超时，请查看日志: ' + $log
exit 1
