#Requires -Version 5.1
<#
.SYNOPSIS
  Matting-Flash 桌面版 staging 组装：只归档 git 跟踪代码 + 指定 runtime。

.DESCRIPTION
  产物布局（staging 根 = 安装根，壳按 <exe_dir>\app\scripts\desktop_launch.py 判定）：
    <StagingDir>\app\
      app\ static\ docs\
      scripts\desktop_launch.py
      runtime\python.exe ...        # torch/transformers 独立运行时
      pyproject.toml requirements.txt README.md LICENSE NOTICE ...

  硬约束：
  - 代码只来自 `git archive HEAD`（工作树必须先提交，保证 staging 与 commit 一致）。
  - weights/、results/、.venv*、__pycache__、模型权重文件一律不进 staging。
  - 归档用 Windows System32\tar.exe，不用 MSYS tar。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StagingDir,
    [string]$Version = '',
    [string]$Root = '',
    [string]$RuntimeDir = '',
    [switch]$SkipRuntime
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host "`n$Text" -ForegroundColor Yellow }
function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}
function Remove-TreeFast([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ($full.Length -lt 12) { throw "拒绝删除可疑路径：$full" }
    & cmd /c "rmdir /s /q `"$full`"" 2>$null | Out-Null
}
function Copy-TreeBulk([string]$Source, [string]$Dest) {
    if (-not (Test-Path -LiteralPath $Source)) { throw "源不存在：$Source" }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    $out = & robocopy $Source $Dest /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 2>&1
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（$LASTEXITCODE）：$($out | Select-Object -Last 5)" }
}
function Get-TreeStats([string]$Path) {
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = [long]0
    foreach ($f in $files) { $bytes += $f.Length }
    return [pscustomobject]@{ Files = $files.Count; Bytes = $bytes }
}

if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path -LiteralPath $Root).Path

if (-not $Version) {
    $pyproject = Join-Path $Root 'pyproject.toml'
    $match = [regex]::Match((Get-Content -LiteralPath $pyproject -Raw), '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw '无法从 pyproject.toml 解析版本号' }
    $Version = $match.Groups[1].Value
}

Write-Host '=============================================='
Write-Host " Matting-Flash staging 组装 v$Version"
Write-Host " staging：$StagingDir"
Write-Host '=============================================='

$appDir = Join-Path $StagingDir 'app'
Remove-TreeFast $StagingDir
New-Item -ItemType Directory -Path $appDir -Force | Out-Null

# ------------------------------------------------------------ 代码归档 ----
Write-Step '[归档] git archive HEAD（只含跟踪代码）'
$tarPath = Join-Path $env:TEMP ("matting-stage-{0}.tar" -f [guid]::NewGuid().ToString('N'))
$archivePaths = @(
    'app',
    'static',
    'docs',
    'scripts/desktop_launch.py',
    'pyproject.toml',
    'requirements.txt',
    'README.md',
    'LICENSE',
    'NOTICE',
    'THIRD_PARTY_NOTICES'
)
& git -C $Root archive --format=tar --output=$tarPath HEAD -- @archivePaths
if ($LASTEXITCODE -ne 0) { throw "git archive 失败（$LASTEXITCODE）" }
& "$env:SystemRoot\System32\tar.exe" -xf $tarPath -C $appDir
if ($LASTEXITCODE -ne 0) { throw "解包 git archive 失败（$LASTEXITCODE）" }
Remove-Item -LiteralPath $tarPath -Force

$launch = Join-Path $appDir 'scripts\desktop_launch.py'
if (-not (Test-Path -LiteralPath $launch)) { throw 'staging 缺少 scripts/desktop_launch.py' }

$stagedPyproject = Get-Content -LiteralPath (Join-Path $appDir 'pyproject.toml') -Raw
$match = [regex]::Match($stagedPyproject, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $match.Success -or $match.Groups[1].Value -ne $Version) {
    throw "staging pyproject.toml 版本与 $Version 不一致"
}
$initPy = Get-Content -LiteralPath (Join-Path $appDir 'app\__init__.py') -Raw
$match = [regex]::Match($initPy, '(?m)^__version__\s*=\s*"([^"]+)"')
if (-not $match.Success -or $match.Groups[1].Value -ne $Version) {
    throw "app/__init__.py __version__ 与 $Version 不一致"
}

# ---------------------------------------------------------------- runtime ----
Write-Step '[归档] 指定 Python runtime（含 torch/transformers）'
if ($SkipRuntime) {
    Write-Warning '跳过 runtime 归档（-SkipRuntime，仅开发冒烟）'
} else {
    if (-not $RuntimeDir) { throw '必须传 -RuntimeDir（含 python.exe 的独立运行时目录）' }
    $RuntimeDir = (Resolve-Path -LiteralPath $RuntimeDir).Path
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeDir 'python.exe'))) {
        throw "RuntimeDir 缺少 python.exe：$RuntimeDir"
    }
    $destRuntime = Join-Path $appDir 'runtime'
    Copy-TreeBulk $RuntimeDir $destRuntime
    if (-not (Test-Path -LiteralPath (Join-Path $destRuntime 'python.exe'))) {
        throw "runtime 复制后缺少 python.exe：$destRuntime"
    }
    $probe = & (Join-Path $destRuntime 'python.exe') -B -c "import sys; print(sys.version.split()[0])" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "staging runtime 无法执行：$probe" }
    Write-Host "    · runtime Python $probe"
}

# ------------------------------------------------------------ 缓存清理 ----
# 字节码缓存不进入安装包：安装目录必须保持只读，包内也不得携带 __pycache__/*.pyc。
$cacheDirs = @(Get-ChildItem -LiteralPath $appDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
foreach ($d in $cacheDirs) { Remove-Item -LiteralPath $d.FullName -Recurse -Force }
Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '*.pyo' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

# ------------------------------------------------------- 权重/数据零容忍 ----
Write-Step '[校验] 权重、数据与缓存零容忍'
$weightsDir = Join-Path $appDir 'weights'
if (Test-Path -LiteralPath $weightsDir) { throw 'staging 内出现 weights/（项目权重不得入包）' }
foreach ($p in @('results', '.venv', '.venv-real', '.venv-host', '.pytest_cache', '__pycache__')) {
    if (Test-Path -LiteralPath (Join-Path $appDir $p)) { throw "staging 内出现 $p" }
}
$leftCaches = @(Get-ChildItem -LiteralPath $appDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
if ($leftCaches.Count -gt 0) { throw "staging 内仍有 $($leftCaches.Count) 个 __pycache__ 目录" }
$leftPyc = @(Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue)
if ($leftPyc.Count -gt 0) { throw "staging 内仍有 $($leftPyc.Count) 个 .pyc 文件" }

# 项目权重铁律：safetensors/onnx/ckpt 一律零命中（runtime 依赖集不含 onnxruntime）。
foreach ($ext in @('*.safetensors', '*.onnx', '*.ckpt')) {
    $hits = @(Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter $ext -ErrorAction SilentlyContinue)
    if ($hits.Count -gt 0) {
        throw ("staging 内出现模型文件（{0}，{1} 个）：{2}" -f $ext, $hits.Count, ($hits | Select-Object -First 3).FullName -join '; ')
    }
}
# pt/pth/bin 在项目代码区零命中；运行时第三方包内仅允许体积很小的配置/数据文件。
$codeHits = @(Get-ChildItem -LiteralPath $appDir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Extension -in @('.pt', '.pth', '.bin') -and
        $_.FullName -notmatch '\\runtime\\Lib\\site-packages\\'
    })
if ($codeHits.Count -gt 0) {
    throw ("项目代码区出现模型文件：" + (($codeHits | Select-Object -First 5).FullName -join '; '))
}
if (-not $SkipRuntime) {
    $rtModelish = @(Get-ChildItem -LiteralPath (Join-Path $appDir 'runtime\Lib\site-packages') -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Extension -in @('.pt', '.pth', '.bin') })
    $big = @($rtModelish | Where-Object { $_.Length -ge 20MB })
    if ($big.Count -gt 0) {
        throw ("runtime 第三方包内出现超过 20MB 的模型候选：" + (($big | Select-Object -First 3).FullName -join '; '))
    }
    Write-Host "    · runtime 内 pt/pth/bin 配置类文件 $($rtModelish.Count) 个（均 < 20MB，非项目权重）"
}

# ---------------------------------------------------------------- 统计 ----
$appStats = Get-TreeStats (Join-Path $appDir 'app')
$allStats = Get-TreeStats $appDir
Write-Host ''
Write-Host ("[统计] app 合计 {0} 文件 / {1}" -f $allStats.Files, (Format-Size $allStats.Bytes))
Write-Host ("       其中应用代码 app/ {0} 文件 / {1}" -f $appStats.Files, (Format-Size $appStats.Bytes))

# ------------------------------------------------------------ manifest ----
$gitHead = (& git -C $Root rev-parse HEAD).Trim()
$manifest = [pscustomobject]@{
    schema         = 1
    product        = 'Matting-Flash-Desktop'
    app_version    = $Version
    git_head       = $gitHead
    arch           = 'win-x64'
    created_utc    = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    staging_dir    = $StagingDir
    app_files      = $allStats.Files
    app_bytes      = $allStats.Bytes
    runtime_source = if ($SkipRuntime) { '(skipped)' } else { $RuntimeDir }
    weights_policy = '项目权重 weights/model.safetensors 不入包；安装后 MATTING_WEIGHTS_DIR 指向 %LOCALAPPDATA%\\Matting-Flash\\weights'
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $StagingDir 'staging-manifest.json') -Encoding utf8

Write-Host ''
Write-Host '[完成] staging 已组装（数据卷只打包 app/）' -ForegroundColor Green
