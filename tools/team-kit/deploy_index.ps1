# deploy_index.ps1 — 把分发包里的索引部署到本机 Zotero profile（可回滚）
# 用法：
#   预览（不写入）： powershell -ExecutionPolicy Bypass -File .\deploy_index.ps1
#   正式写入：       powershell -ExecutionPolicy Bypass -File .\deploy_index.ps1 -Force
#   指定 profile：   ... -Force -ProfileDir "C:\Users\你\AppData\Roaming\Zotero\Zotero\Profiles\xxxx.default"

param(
  [switch]$Force,
  [switch]$NoBackup,
  [switch]$DryRun,
  [string]$ProfileDir = ''
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$src = Join-Path $PSScriptRoot 'index'

Write-Host "Zotero 检索索引 · 索引部署"
Write-Host ("包内索引: {0}" -f $src)

if (-not (Test-Path (Join-Path $src 'chunks.json'))) {
  Write-Host "❌ 找不到包内索引（index\chunks.json 缺失），请确认解压完整。" -ForegroundColor Red
  exit 2
}

# 1) Zotero 必须关闭，避免写坏正在使用的索引
$zot = Get-Process zotero -ErrorAction SilentlyContinue
if ($zot -and -not $DryRun) {
  Write-Host "❌ 检测到 Zotero 正在运行。请先完全退出 Zotero 再执行（否则索引可能写坏）。" -ForegroundColor Red
  Write-Host "   若你确定要继续，可加 -Force 跳过此检查（不推荐）。"
  if (-not $Force) { exit 3 }
  Write-Host "   ⚠ 已按 -Force 继续……" -ForegroundColor Yellow
}

# 2) 定位 profile
$profilesRoot = Join-Path $env:APPDATA 'Zotero\Zotero\Profiles'
if ($ProfileDir) {
  if (-not (Test-Path $ProfileDir)) { Write-Host "❌ 指定的 profile 目录不存在: $ProfileDir" -ForegroundColor Red; exit 2 }
} else {
  if (-not (Test-Path $profilesRoot)) { Write-Host "❌ 找不到 Zotero profile 目录: $profilesRoot（Zotero 装过并启动过吗？）" -ForegroundColor Red; exit 2 }
  $cands = Get-ChildItem $profilesRoot -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'prefs.js') }
  if ($cands.Count -eq 0) { Write-Host "❌ 在 $profilesRoot 下没有找到含 prefs.js 的 profile" -ForegroundColor Red; exit 2 }
  if ($cands.Count -gt 1) {
    Write-Host "⚠ 发现多个 profile，请用 -ProfileDir 指定其中一个："
    foreach ($c in $cands) { Write-Host ("   - " + $c.FullName) }
    exit 2
  }
  $ProfileDir = $cands[0].FullName
}
Write-Host ("目标 profile: {0}" -f $ProfileDir)

$target = Join-Path $ProfileDir 'retrieval-index'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = "$target.bak-$stamp"

# 3) 预览
Write-Host ""
if (Test-Path $target) {
  $oldChunks = Join-Path $target 'chunks.json'
  $oldInfo = if (Test-Path $oldChunks) { "现有索引 chunks.json = {0:N0} 字节" -f (Get-Item $oldChunks).Length } else { '现有目录不含 chunks.json' }
  Write-Host ("将执行：备份现有索引 → {0}" -f $oldInfo)
  Write-Host ("        备份位置: {0}" -f $backup)
} else {
  Write-Host "将执行：新建索引目录（本机当前没有索引）"
}
Write-Host ("        复制 {0} → {1}" -f $src, $target)

if ((-not $Force) -or $DryRun) {
  Write-Host ""
  Write-Host "以上为预览。确认无误后加 -Force 正式执行：" -ForegroundColor Yellow
  Write-Host "  powershell -ExecutionPolicy Bypass -File .\deploy_index.ps1 -Force"
  exit 0
}

# 4) 备份 + 复制
if ((Test-Path $target) -and (-not $NoBackup)) {
  Move-Item -Path $target -Destination $backup
  Write-Host ("✅ 已备份到 {0}" -f $backup)
} elseif (Test-Path $target) {
  Remove-Item -Path $target -Recurse -Force
  Write-Host "⚠ 已按 -NoBackup 删除旧索引（不可回滚）" -ForegroundColor Yellow
}

Copy-Item -Path $src -Destination $target -Recurse -Force
$chunks = Join-Path $target 'chunks.json'
if (-not (Test-Path $chunks)) { Write-Host "❌ 复制后仍未找到 chunks.json，请检查权限/磁盘空间" -ForegroundColor Red; exit 4 }

Write-Host ("✅ 索引已部署（{0:N0} 字节）" -f (Get-Item $chunks).Length)
Write-Host ""
Write-Host "下一步："
Write-Host "  1) 启动 Zotero（首次检索约 3 秒建 BM25）"
Write-Host "  2) 运行自检： powershell -ExecutionPolicy Bypass -File .\verify_install.ps1"
Write-Host "  3) 回滚：删除 $target，把 $(Split-Path -Leaf $backup) 改回 retrieval-index"
exit 0
