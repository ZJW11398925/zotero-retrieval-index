# deploy_xpi.ps1 — developer helper: install a built XPI into a local Zotero profile
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\deploy_xpi.ps1 -Xpi .\retrieval-index-0.1.52.xpi
#   ... -ProfileDir "C:\Users\you\AppData\Roaming\Zotero\Zotero\Profiles\xxxx.default"
#
# The script closes Zotero, copies the XPI into <profile>\extensions\<addon-id>.xpi,
# keeps a backup of the previous XPI, restarts Zotero and waits for /retrieval/ping.

param(
  [Parameter(Mandatory = $true)][string]$Xpi,
  [string]$ProfileDir = ''
)

$ErrorActionPreference = 'Continue'
if (-not (Test-Path $Xpi)) { Write-Host "XPI not found: $Xpi" -ForegroundColor Red; exit 1 }

function Find-Profile {
  param([string]$Explicit)
  if ($Explicit) {
    if (-not (Test-Path $Explicit)) { throw "Profile directory not found: $Explicit" }
    return [System.IO.Path]::GetFullPath($Explicit)
  }
  $base = Join-Path $env:APPDATA 'Zotero\Zotero\Profiles'
  if (-not (Test-Path $base)) { throw "No Zotero profiles found at $base. Start Zotero once." }
  $cands = Get-ChildItem $base -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'prefs.js') }
  if ($cands.Count -eq 0) { throw "No profile with prefs.js under $base" }
  if ($cands.Count -gt 1) {
    throw ("Multiple profiles found; pass -ProfileDir:`n  " + (($cands | ForEach-Object { $_.FullName }) -join "`n  "))
  }
  return $cands[0].FullName
}

function Get-AddonId([string]$Path) {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $zip = [IO.Compression.ZipFile]::OpenRead($Path)
  try {
    $entry = $zip.Entries | Where-Object { $_.FullName -eq 'manifest.json' } | Select-Object -First 1
    if (-not $entry) { return 'retrieval-index@sit2023.local' }
    $reader = New-Object IO.StreamReader($entry.Open())
    $json = $reader.ReadToEnd(); $reader.Close()
    $m = [regex]::Match($json, '"id"\s*:\s*"([^"]+)"')
    if ($m.Success) { return $m.Groups[1].Value }
    return 'retrieval-index@sit2023.local'
  } finally { $zip.Dispose() }
}

$prof = Find-Profile $ProfileDir
$addonId = Get-AddonId $Xpi
$extDir = Join-Path $prof 'extensions'
New-Item -ItemType Directory -Force $extDir | Out-Null
$dst = Join-Path $extDir ($addonId + '.xpi')
$backupDir = Join-Path $prof 'extensions-backup'
New-Item -ItemType Directory -Force $backupDir | Out-Null

Write-Host "=== Closing Zotero ==="
Get-Process zotero -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 4

if (Test-Path $dst) {
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  Copy-Item $dst (Join-Path $backupDir ((Split-Path -Leaf $dst) + '.' + $stamp + '.bak')) -Force
  Write-Host "Previous XPI backed up to $backupDir"
}

Copy-Item $Xpi $dst -Force
Write-Host "Installed $addonId -> $dst"

Write-Host "=== Starting Zotero ==="
$zot = @(
  'C:\Program Files\Zotero\zotero.exe',
  (Join-Path $env:LOCALAPPDATA 'Zotero\zotero.exe')
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $zot) { Write-Host 'zotero.exe not found; start it manually.' -ForegroundColor Yellow; exit 0 }
Start-Process -FilePath $zot
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 3
  try {
    $h = curl.exe -sS --noproxy '*' --max-time 3 'http://127.0.0.1:23119/retrieval/ping' 2>$null
    if ($h -and $h -match '"ok":\s*true') { Write-Host "Plugin responding: $h"; exit 0 }
  } catch {}
}
Write-Host 'Plugin did not respond within 90s; check Zotero add-ons.' -ForegroundColor Yellow
exit 1
