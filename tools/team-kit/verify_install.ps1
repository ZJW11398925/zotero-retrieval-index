# verify_install.ps1 — 团队分发包装后自检（只读，不改任何东西）
# 用法：powershell -ExecutionPolicy Bypass -File .\verify_install.ps1 [-BaseUrl http://127.0.0.1:23119]

param(
  [string]$BaseUrl = 'http://127.0.0.1:23119'
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$root = $PSScriptRoot
$pass = 0; $fail = 0

function Check([string]$name, [bool]$ok, [string]$detail) {
  if ($ok) { $script:pass++; Write-Host ("  [PASS] {0,-22} {1}" -f $name, $detail) }
  else     { $script:fail++; Write-Host ("  [FAIL] {0,-22} {1}" -f $name, $detail) -ForegroundColor Red }
}
function CurlJson([string]$url) {
  try { return (curl.exe -sS --noproxy '*' --max-time 20 $url 2>$null | ConvertFrom-Json) } catch { return $null }
}
function Sha256([string]$path) {
  if (-not (Test-Path $path)) { return '' }
  return (Get-FileHash -Algorithm SHA256 -Path $path).Hash.ToLower()
}

Write-Host "Zotero 检索索引 · 安装自检"
Write-Host ("接口: {0}" -f $BaseUrl)
Write-Host ("目录: {0}" -f $root)
Write-Host ('=' * 72)

# ---------- 0. 清单 ----------
$manPath = Join-Path $root 'index-manifest.json'
$man = $null
if (Test-Path $manPath) { $man = Get-Content $manPath -Raw -Encoding UTF8 | ConvertFrom-Json }
Check 'manifest' ($null -ne $man) $(if ($man) { "plugin=$($man.pluginVersion) chunks=$($man.index.chunks)" } else { 'index-manifest.json 缺失' })

# ---------- 1. 接口 ----------
Write-Host "`n一、接口"
$ping = CurlJson "$BaseUrl/retrieval/ping"
Check 'ping' ($null -ne $ping -and $ping.ok -eq $true) $(if ($ping) { "version=$($ping.version) startupError=$($ping.startupError)" } else { '连不上：插件未加载或 Zotero 未运行' })
if ($ping -and $man) { Check 'version' ([string]$ping.version -eq [string]$man.pluginVersion) ("期望 {0}，实际 {1}" -f $man.pluginVersion, $ping.version) }

$stats = CurlJson "$BaseUrl/retrieval/stats"
Check 'stats' ($null -ne $stats -and $stats.loaded -eq $true) $(if ($stats) { "loaded=$($stats.loaded) chunks=$($stats.chunks) papers=$($stats.papers)" } else { 'stats 不可用' })
if ($stats -and $man) {
  Check 'chunks' ([int]$stats.chunks -eq [int]$man.index.chunks) ("期望 {0}，实际 {1}" -f $man.index.chunks, $stats.chunks)
  Check 'papers' ([int]$stats.papers -eq [int]$man.index.parents) ("期望 {0}，实际 {1}" -f $man.index.parents, $stats.papers)
  Check 'dim' ([int]$stats.dim -eq [int]$man.index.dim) ("期望 {0}，实际 {1}" -f $man.index.dim, $stats.dim)
  Check 'hasVectors' ($stats.hasVectors -eq $true) ("hasVectors=$($stats.hasVectors)")
}

# ---------- 2. 协议接口 ----------
Write-Host "`n二、Agent 协议接口"
$tmp = Join-Path $env:TEMP ('proto_' + [guid]::NewGuid().ToString('N') + '.md')
try {
  curl.exe -sS --noproxy '*' --max-time 15 -o $tmp "$BaseUrl/retrieval/protocol" 2>$null | Out-Null
  $bytes = if (Test-Path $tmp) { (Get-Item $tmp).Length } else { 0 }
  $head = if ($bytes -gt 0) { (Get-Content $tmp -TotalCount 1 -Encoding UTF8) } else { '' }
  Check 'protocol' ($bytes -ge 13000 -and $head -like '# Agent*') ("HTTP 返回 {0} 字节，首行: {1}" -f $bytes, $head)
} finally { if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue } }

# ---------- 3. 检索 ----------
Write-Host "`n三、检索"
$q = [uri]::EscapeDataString('儿童色彩偏好')
$res = CurlJson "$BaseUrl/retrieval/search?q=$q&k=3"
Check 'search' ($null -ne $res -and $res.evidence.returned -ge 1) $(if ($res) { "returned=$($res.evidence.returned) papers=$($res.evidence.papers) hints=$($res.evidence.hints -join ';')" } else { '检索失败' })
if ($res -and $res.results) {
  $r0 = $res.results[0]
  Check 'result-fields' ($null -ne $r0.itemKey -and $null -ne $r0.title -and $null -ne $r0.text) ("top1={0} p={1} source={2}" -f $r0.itemKey, $r0.page, $r0.pageSource)
}

# ---------- 4. 索引文件完整性 ----------
Write-Host "`n四、索引文件完整性（SHA256）"
if ($man) {
  # 4a 包内文件
  foreach ($rel in @('index/chunks.json', 'index/vectors.b64', 'index/meta.json')) {
    $p = Join-Path $root ($rel -replace '/', [IO.Path]::DirectorySeparatorChar)
    $want = $man.files.$rel.sha256
    $got = Sha256 $p
    Check ('包内/' + $rel.Split('/')[-1]) ($want -eq $got) $(if ($got) { 'sha256 匹配' } else { '文件缺失' })
  }
  # 4b 已部署索引（以 ping.dataDir 为准）
  $deployDir = if ($ping) { [string]$ping.dataDir } else { '' }
  if ($deployDir -and (Test-Path $deployDir)) {
    foreach ($name in @('chunks.json', 'vectors.b64', 'meta.json')) {
      $p = Join-Path $deployDir $name
      $want = $man.files.("index/$name").sha256
      $got = Sha256 $p
      Check ('已部署/' + $name) ($want -eq $got) $(if (-not (Test-Path $p)) { '文件缺失' } elseif ($want -eq $got) { 'sha256 匹配' } else { 'sha256 与包不一致（索引可能被增量更新过；若 chunks 数一致可接受）' })
    }
  } else {
    Check 'deployed-index' $false ("无法定位已部署索引目录（ping.dataDir='{0}'）" -f $deployDir)
  }
}

Write-Host ('=' * 72)
Write-Host ("汇总: PASS {0} / FAIL {1}" -f $pass, $fail)
if ($fail -eq 0) {
  Write-Host "结论: 安装成功，接口与索引均正常。"
  Write-Host "下一步: curl `"$BaseUrl/retrieval/protocol`" 获取 agent 使用协议。"
  exit 0
} else {
  Write-Host "结论: 有项目未通过，请对照上面的 [FAIL] 项排查（常见原因见《使用说明-团队分发.md》）。" -ForegroundColor Red
  exit 1
}
