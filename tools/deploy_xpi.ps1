# 自动部署插件 xpi：关 Zotero → 替换 profile 内 xpi → 重启 → 等插件响应
# 用法: powershell -File deploy_xpi.ps1 <xpi路径>
$ErrorActionPreference = 'Continue'
$xpi = $args[0]
if (-not $xpi -or -not (Test-Path $xpi)) { Write-Output "用法: deploy_xpi.ps1 <xpi路径>"; exit 1 }
$prof = Join-Path $env:APPDATA 'Zotero\Zotero\Profiles\default'
$dst = Join-Path $prof 'extensions\retrieval-index@sit2023.local.xpi'
$backupDir = 'D:\workspace\dist\backup'
New-Item -ItemType Directory -Force $backupDir | Out-Null

Write-Output "=== 关闭 Zotero ==="
$p = Get-Process zotero -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($p) { $p.CloseMainWindow() | Out-Null }
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    if (-not (Get-Process zotero -ErrorAction SilentlyContinue)) { Write-Output ("已关闭 ({0}s)" -f (($i + 1) * 2)); break }
}
if (Get-Process zotero -ErrorAction SilentlyContinue) { Get-Process zotero | Stop-Process -Force; Start-Sleep -Seconds 3; Write-Output '已强制结束' }

Write-Output "=== 备份并替换 xpi ==="
if (Test-Path $dst) {
    $oldVer = $null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($dst)
        $entry = $zip.Entries | Where-Object { $_.FullName -eq 'manifest.json' }
        if ($entry) {
            $reader = New-Object IO.StreamReader($entry.Open())
            $json = $reader.ReadToEnd(); $reader.Close()
            $m = [regex]::Match($json, '"version"\s*:\s*"([^"]+)"')
            if ($m.Success) { $oldVer = $m.Groups[1].Value }
        }
        $zip.Dispose()
    } catch {}
    if ($oldVer) { Copy-Item $dst (Join-Path $backupDir ("retrieval-index-{0}-installed.xpi" -f $oldVer)) -Force }
}
Copy-Item $xpi $dst -Force
Write-Output ("已替换: {0} bytes" -f (Get-Item $dst).Length)

Write-Output "=== 重启 Zotero ==="
Start-Process 'C:\Program Files\Zotero\Zotero.exe'
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    $r = curl.exe -sS --noproxy '*' --max-time 3 http://127.0.0.1:23119/retrieval/ping 2>$null
    if ($r -and $r -notmatch 'No endpoint') {
        Write-Output ("插件响应 ({0}s): {1}" -f (($i + 1) * 2), $r.Substring(0, [Math]::Min(160, $r.Length)))
        exit 0
    }
}
Write-Output '等待超时：插件未响应'
exit 1
