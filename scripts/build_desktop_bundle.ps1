#Requires -Version 5.1
<#
.SYNOPSIS
  Matting-Flash 桌面版完整构建：runtime → cargo 门禁 → staging → 冒烟 → 数据分卷 → NSIS → SHA256SUMS。

.DESCRIPTION
  所有 target/staging/分卷/临时解包落 D 盘构建根（默认 D:\R5Build\Matting-Flash）：
    cargo-target\              CARGO_TARGET_DIR
    tmp\                       TEMP/TMP
    runtime\                   CPython 3.12.10 base + .venv-real 依赖闭包（torch/transformers）
    staging\                   app\（代码 + runtime） + 壳 exe（冒烟用）
    dist\                      Setup / Data 分卷 / SHA256SUMS / upload-list.json
    logs\                      每步构建日志与 runtime 清单

  门禁（可用开关跳过，跳过后报告必须如实标注）：
    cargo test / cargo clippy -D warnings / cargo build --release
    runtime 导入探针（torch/transformers/timm/... + app.main）
    staging 直启 → /api/status → 关窗退出清理
    7z t 回读、Setup ProductVersion、SHA256 校验（SHA256SUMS 为 LF 行尾）
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$Root = '',
    [string]$BuildRoot = 'D:\R5Build\Matting-Flash',
    [string]$BasePython = '',
    [string]$VenvDir = '',
    [string]$SevenZipStandalone = '',
    [string]$MaxPartBytes = '1900MB',
    [int]$Level = 5,
    [switch]$SkipCargoGates,
    [switch]$SkipSmoke,
    [switch]$SkipInstaller,
    [switch]$ReuseRuntime,
    [switch]$KeepStaging
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host "`n$Text" -ForegroundColor Yellow }
function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}
function Convert-ToBytes([string]$Text) {
    if ($Text -match '^\s*(\d+(?:\.\d+)?)\s*(MB|GB|B)?\s*$') {
        $value = [double]$Matches[1]
        switch ($Matches[2]) {
            'GB' { return [long]($value * 1GB) }
            'MB' { return [long]($value * 1MB) }
            default { return [long]$value }
        }
    }
    return [long]$Text
}
function Remove-TreeFast([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ($full.Length -lt 12) { throw "拒绝删除可疑路径：$full" }
    & cmd /c "rmdir /s /q `"$full`"" 2>$null | Out-Null
}
function Get-TreeStats([string]$Path) {
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = [long]0
    foreach ($f in $files) { $bytes += $f.Length }
    return [pscustomobject]@{ Files = $files.Count; Bytes = $bytes }
}
function Invoke-Logged([string]$Name, [string]$LogDir, [string]$Exe, [string[]]$Arguments) {
    $log = Join-Path $LogDir "$Name.log"
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $output | Tee-Object -FilePath $log | Out-Null
    return [pscustomobject]@{ Code = $code; Output = @($output) }
}
function Find-Standalone7za([string]$Explicit) {
    $candidates = @(
        $Explicit,
        $env:MATTING_7ZA,
        (Join-Path $Root 'scripts\installer\7za.exe'),
        'C:\Program Files\7-Zip\7za.exe',
        'C:\Program Files (x86)\7-Zip\7za.exe',
        'C:\Users\Doro\APP\repository\SeedVR2-Turbo\scripts\installer\7za.exe'
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    throw ('未找到独立版 7za.exe。NSIS 内嵌工具必须是单文件版（7z.exe 依赖 7z.dll，嵌入后会静默失败）；' +
        '请用 -SevenZipStandalone 指定，或把 7za.exe 放到 scripts\installer\。')
}
function Get-ShellVersion([string]$ExePath) {
    return (Get-Item -LiteralPath $ExePath).VersionInfo.ProductVersion
}

# ---------------------------------------------------------------- 环境 ----
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $VenvDir) { $VenvDir = Join-Path $Root '.venv-real' }
if (-not $BasePython) {
    $cfg = Join-Path $VenvDir 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $cfg)) { throw "找不到 pyvenv.cfg：$cfg" }
    $line = (Get-Content -LiteralPath $cfg | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1)
    if (-not $line) { throw 'pyvenv.cfg 缺少 home 行' }
    $BasePython = ($line -split '=', 2)[1].Trim()
}
$BasePython = (Resolve-Path -LiteralPath $BasePython).Path
if (-not (Test-Path -LiteralPath (Join-Path $BasePython 'python.exe'))) {
    throw "BasePython 缺少 python.exe：$BasePython"
}
$venvSitePackages = Join-Path $VenvDir 'Lib\site-packages'
if (-not (Test-Path -LiteralPath $venvSitePackages)) { throw "缺少 venv site-packages：$venvSitePackages" }

if (-not $Version) {
    $match = [regex]::Match((Get-Content -LiteralPath (Join-Path $Root 'pyproject.toml') -Raw), '(?m)^version\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw '无法从 pyproject.toml 解析版本号' }
    $Version = $match.Groups[1].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "版本号必须是三段数字：$Version" }

$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
if ($BuildRoot -notmatch '^(D:\\)') { throw "构建根必须在 D 盘（当前 $BuildRoot）" }
$targetDir = Join-Path $BuildRoot 'cargo-target'
$tempDir = Join-Path $BuildRoot 'tmp'
$runtimeDir = Join-Path $BuildRoot 'runtime'
$stagingDir = Join-Path $BuildRoot 'staging'
$distDir = Join-Path $BuildRoot 'dist'
$logDir = Join-Path $BuildRoot 'logs'
$smokeDir = Join-Path $BuildRoot 'smoke'
foreach ($dir in @($targetDir, $tempDir, $stagingDir, $distDir, $logDir, $smokeDir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}

$env:CARGO_TARGET_DIR = $targetDir
$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUNBUFFERED = '1'

Write-Host '=============================================='
Write-Host " Matting-Flash 桌面版构建 v$Version"
Write-Host " 仓库：$Root"
Write-Host " 构建根：$BuildRoot"
Write-Host '=============================================='

# ------------------------------------------------------- 版本三件套 ----
Write-Step '[门禁] 版本三件套与主仓一致'
$mainVersion = [regex]::Match((Get-Content -LiteralPath (Join-Path $Root 'pyproject.toml') -Raw), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
$initVersion = [regex]::Match((Get-Content -LiteralPath (Join-Path $Root 'app\__init__.py') -Raw), '(?m)^__version__\s*=\s*"([^"]+)"').Groups[1].Value
$pkgVersion = (Get-Content -LiteralPath (Join-Path $Root 'desktop\package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
$confVersion = (Get-Content -LiteralPath (Join-Path $Root 'desktop\src-tauri\tauri.conf.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
$cargoVersion = [regex]::Match(
    (Get-Content -LiteralPath (Join-Path $Root 'desktop\src-tauri\Cargo.toml') -Raw),
    '(?m)^version\s*=\s*"([^"]+)"'
).Groups[1].Value
foreach ($v in @($mainVersion, $initVersion, $pkgVersion, $confVersion, $cargoVersion)) {
    if ($v -ne $Version) { throw "版本不一致：目标=$Version pyproject=$mainVersion app=$initVersion package.json=$pkgVersion tauri.conf=$confVersion Cargo=$cargoVersion" }
}
Write-Host "    · pyproject / app.__version__ / package.json / tauri.conf.json / Cargo.toml = $Version"

# ---------------------------------------------------------- runtime ----
Write-Step '[准备] 独立 Python runtime（base + .venv-real 依赖闭包）'
$runtimePython = Join-Path $runtimeDir 'python.exe'
if ($ReuseRuntime -and (Test-Path -LiteralPath $runtimePython)) {
    Write-Host '    [复用] 已有 runtime（-ReuseRuntime）'
} else {
    $py = Join-Path $BasePython 'python.exe'
    & $py (Join-Path $PSScriptRoot 'assemble_desktop_runtime.py') `
        --BasePython $BasePython `
        --VenvSitePackages $venvSitePackages `
        --OutDir $runtimeDir `
        --Manifest (Join-Path $logDir 'runtime-manifest.json') 2>&1 |
        Tee-Object -FilePath (Join-Path $logDir 'runtime-assemble.log') | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "runtime 组装失败（$LASTEXITCODE），日志：$logDir\runtime-assemble.log" }
}
if (-not (Test-Path -LiteralPath $runtimePython)) { throw "runtime 缺少 python.exe：$runtimeDir" }

# 导入探针：应用与真实推理路径的实际依赖。不触发 CUDA 上下文（不调用 cuda.is_available）。
$probeCode = "import sys, numpy, PIL, torch, torchvision, transformers, timm, fastapi, uvicorn, multipart, cv2, scipy, huggingface_hub, safetensors; print('runtime_ok', sys.version.split()[0], 'torch', torch.__version__, 'tv', torchvision.__version__, 'tf', transformers.__version__, 'timm', timm.__version__)"
$probe = & $runtimePython -B -c $probeCode 2>&1
if ($LASTEXITCODE -ne 0) { throw ("runtime 导入探针失败：" + ($probe -join "`n")) }
Write-Host "    · $probe"

$emptyWeights = Join-Path $tempDir 'probe-empty-weights'
New-Item -ItemType Directory -Path $emptyWeights -Force | Out-Null
$env:MATTING_WEIGHTS_DIR = $emptyWeights
$appProbe = & $runtimePython -B -c "import sys; sys.path.insert(0, r'$Root'); import app.main; print('app_ok')" 2>&1
Remove-Item Env:MATTING_WEIGHTS_DIR -ErrorAction SilentlyContinue
if ($LASTEXITCODE -ne 0) { throw ("app.main 导入失败：" + ($appProbe -join "`n")) }
Write-Host "    · $appProbe"

$rtStats = Get-TreeStats $runtimeDir
Write-Host ("    · runtime {0} 文件 / {1}" -f $rtStats.Files, (Format-Size $rtStats.Bytes))

# ------------------------------------------------------- cargo 门禁 ----
if ($SkipCargoGates) {
    Write-Warning '跳过 cargo test/clippy/build（-SkipCargoGates）'
} else {
    $manifestPath = Join-Path $Root 'desktop\src-tauri\Cargo.toml'
    $cargoExe = (Get-Command cargo -ErrorAction SilentlyContinue).Source
    if (-not $cargoExe) { throw '未找到 cargo' }

    Write-Step '[门禁] cargo test'
    $r = Invoke-Logged 'cargo-test' $logDir $cargoExe @('test', '--manifest-path', $manifestPath)
    if ($r.Code -ne 0) { throw ("cargo test 失败（$($r.Code)），日志：$logDir\cargo-test.log`n" + (($r.Output | Select-Object -Last 15) -join "`n")) }

    Write-Step '[门禁] cargo clippy --all-targets -- -D warnings'
    $r = Invoke-Logged 'cargo-clippy' $logDir $cargoExe @('clippy', '--manifest-path', $manifestPath, '--all-targets', '--', '-D', 'warnings')
    if ($r.Code -ne 0) { throw ("cargo clippy 失败（$($r.Code)），日志：$logDir\cargo-clippy.log`n" + (($r.Output | Select-Object -Last 15) -join "`n")) }

    Write-Step '[构建] cargo build --release'
    $r = Invoke-Logged 'cargo-build' $logDir $cargoExe @('build', '--manifest-path', $manifestPath, '--release')
    if ($r.Code -ne 0) { throw ("cargo build --release 失败（$($r.Code)），日志：$logDir\cargo-build.log`n" + (($r.Output | Select-Object -Last 15) -join "`n")) }
}

$shellExe = Join-Path $targetDir 'release\Matting-Flash.exe'
if (-not (Test-Path -LiteralPath $shellExe)) { throw "找不到壳产物：$shellExe" }
$shellVersion = Get-ShellVersion $shellExe
if ($shellVersion -ne $Version) { throw "壳 ProductVersion=$shellVersion 与主仓 $Version 不一致" }
Write-Host "    · 壳 ProductVersion = $shellVersion"

# ---------------------------------------------------------- staging ----
Write-Step '[组装] staging（代码 + runtime）'
& (Join-Path $PSScriptRoot 'assemble_desktop_staging.ps1') `
    -StagingDir $stagingDir -Version $Version -Root $Root -RuntimeDir $runtimeDir
if ($LASTEXITCODE -ne 0) { throw "assemble_desktop_staging.ps1 失败（$LASTEXITCODE）" }
Copy-Item -LiteralPath $shellExe -Destination (Join-Path $stagingDir 'Matting-Flash.exe') -Force
$stagedShell = Join-Path $stagingDir 'Matting-Flash.exe'
if ((Get-ShellVersion $stagedShell) -ne $Version) { throw 'staging 壳 ProductVersion 不一致' }
$stagingStats = Get-TreeStats $stagingDir
Write-Host ("    · staging {0} 文件 / {1}" -f $stagingStats.Files, (Format-Size $stagingStats.Bytes))

# ----------------------------------------------------------- 冒烟 ----
if ($SkipSmoke) {
    Write-Warning '跳过 staging 直启冒烟（-SkipSmoke）'
} else {
    Write-Step '[冒烟] staging 直启 → /api/status → 关窗退出'
    Remove-TreeFast $smokeDir
    foreach ($sub in @('logs', 'weights', 'results')) {
        New-Item -ItemType Directory -Path (Join-Path $smokeDir $sub) -Force | Out-Null
    }
    $env:MATTING_LOG_DIR = Join-Path $smokeDir 'logs'
    $env:MATTING_WEIGHTS_DIR = Join-Path $smokeDir 'weights'
    $env:MATTING_RESULTS_DIR = Join-Path $smokeDir 'results'
    $env:MATTING_STARTUP_TIMEOUT_SECS = '180'

    $shellLog = Join-Path $smokeDir 'logs\matting-flash-shell.log'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $process = Start-Process -FilePath $stagedShell -WorkingDirectory $stagingDir -PassThru
    $deadline = (Get-Date).AddSeconds(150)
    $readyLine = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if (Test-Path -LiteralPath $shellLog) {
            $readyLine = (Get-Content -LiteralPath $shellLog -Raw -Encoding UTF8 -ErrorAction SilentlyContinue |
                Select-String -Pattern 'Python 服务就绪，端口=(\d+)' | Select-Object -First 1)
            if ($readyLine) { break }
        }
        if ($process.HasExited) { throw "壳提前退出（ExitCode=$($process.ExitCode)），日志：$shellLog" }
    }
    if (-not $readyLine) { throw "150s 内未见「Python 服务就绪」日志：$shellLog" }
    $port = [int]$readyLine.Matches[0].Groups[1].Value

    $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 10
    if ($status.version -ne $Version) { throw "staging /api/status 版本 $($status.version) != $Version" }
    if (-not $status.results_dir -or $status.results_dir -ne (Join-Path $smokeDir 'results')) {
        throw "staging 结果目录未采纳环境覆盖：$($status.results_dir)"
    }
    if ($status.weights_dir -ne (Join-Path $smokeDir 'weights')) { throw "staging 权重目录未采纳环境覆盖：$($status.weights_dir)" }
    if ($status.ready) { throw 'staging 冒烟不应带入权重（ready 应为 false）' }
    if (-not ($status.missing_weights -contains 'model.safetensors')) {
        throw "NOT_READY 缺失清单异常：$($status.missing_weights -join ',')"
    }
    if ($status.state -ne 'NOT_READY') { throw "staging 状态应为 NOT_READY：$($status.state)" }

    $pythonLog = Get-ChildItem -LiteralPath (Join-Path $smokeDir 'logs') -Filter 'desktop_python_*.log' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $pythonLog) { throw '未生成 desktop_python_*.log' }
    $anchor = Select-String -LiteralPath $pythonLog.FullName -Encoding Default -Pattern '监听端口: (\d+)' | Select-Object -First 1
    if (-not $anchor -or [int]$anchor.Matches[0].Groups[1].Value -ne $port) {
        throw "端口日志锚点不一致：shell=$port，python日志=$($anchor.Line)"
    }

    Write-Host "    · 端口 $port；state=$($status.state)；缺失 $($status.missing_weights -join ',')"
    Write-Host "    · shell 日志：$shellLog"
    Write-Host "    · python 日志：$($pythonLog.FullName)"

    # 温和关闭：taskkill 不带 /F 发 WM_CLOSE，让壳走 RunEvent::Exit 自清理
    & taskkill /PID $process.Id 2>$null | Out-Null
    $graceDeadline = (Get-Date).AddSeconds(25)
    while (-not $process.HasExited -and (Get-Date) -lt $graceDeadline) { Start-Sleep -Milliseconds 500 }
    if (-not $process.HasExited) {
        Write-Warning '壳未在 25s 内退出，强制杀进程树'
        & taskkill /PID $process.Id /T /F 2>$null | Out-Null
    }
    Start-Sleep -Seconds 1

    $orphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$stagingDir*desktop_launch.py*" })
    if ($orphans.Count -gt 0) { throw "退出后仍有 $($orphans.Count) 个孤儿 python 进程" }
    $exitLog = Select-String -LiteralPath $shellLog -Encoding UTF8 -Pattern 'Python 进程树已停止|Matting-Flash 壳已退出' -ErrorAction SilentlyContinue
    if (-not $exitLog) { Write-Warning '壳日志未出现退出清理锚点（可能被强制终止）' }
    Write-Host '    · 退出后无孤儿 python 进程'
    Remove-Item Env:MATTING_LOG_DIR, Env:MATTING_WEIGHTS_DIR, Env:MATTING_RESULTS_DIR, Env:MATTING_STARTUP_TIMEOUT_SECS, Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
}

# ------------------------------------------------ 包前缓存零容忍 ----
$cacheDirs = @(Get-ChildItem -LiteralPath $stagingDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
foreach ($d in $cacheDirs) { Remove-Item -LiteralPath $d.FullName -Recurse -Force }
Get-ChildItem -LiteralPath $stagingDir -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
$leftCaches = @(Get-ChildItem -LiteralPath $stagingDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
$leftPyc = @(Get-ChildItem -LiteralPath $stagingDir -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue)
if ($leftCaches.Count -gt 0 -or $leftPyc.Count -gt 0) {
    throw "打包前 staging 仍含字节码缓存：__pycache__=$($leftCaches.Count)，pyc=$($leftPyc.Count)"
}

# --------------------------------------------------------- 数据分卷 ----
Write-Step '[打包] 7z 数据分卷（只含 staging\app）'
$sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue).Source
if (-not $sevenZip) {
    foreach ($c in @('C:\Program Files\7-Zip\7z.exe', 'C:\Program Files (x86)\7-Zip\7z.exe')) {
        if (Test-Path -LiteralPath $c) { $sevenZip = $c; break }
    }
}
if (-not $sevenZip) { throw '未找到 7z.exe' }
$maxBytes = Convert-ToBytes $MaxPartBytes
if ($maxBytes -gt 1900MB) { throw "单卷上限不得超过 1900MB（当前 $MaxPartBytes）" }

$archiveBase = Join-Path $distDir 'Matting-Flash-Data.7z'
$stale = @(Get-ChildItem -LiteralPath $distDir -Filter 'Matting-Flash-Data.7z.*' -File -ErrorAction SilentlyContinue)
foreach ($f in $stale) { Remove-Item -LiteralPath $f.FullName -Force }
Push-Location $stagingDir
try {
    $out = & $sevenZip a -t7z "-mx=$Level" "-v${maxBytes}b" $archiveBase 'app' -bso0 -bsp0 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("7z 打包失败（$LASTEXITCODE）：" + ($out -join "`n")) }
} finally {
    Pop-Location
}
$volumes = @(Get-ChildItem -LiteralPath $distDir -Filter 'Matting-Flash-Data.7z.*' -File | Sort-Object Name)
if ($volumes.Count -eq 0) { throw '未生成任何数据分卷' }
$firstVolume = $volumes[0].FullName
$out = & $sevenZip t $firstVolume -bso0 -bsp0 2>&1
if ($LASTEXITCODE -ne 0) { throw ("7z t 回读失败（$LASTEXITCODE）：" + ($out -join "`n")) }
foreach ($v in $volumes) {
    if ($v.Length -gt 1900MB) { throw "分卷 $($v.Name) 超过 1900MB" }
    Write-Host ("    · {0} {1}" -f $v.Name, (Format-Size $v.Length))
}
Write-Host '    · 7z t 回读通过'
$cacheList = & $sevenZip l $firstVolume -bso0 -bsp0 2>&1
if ($LASTEXITCODE -ne 0) { throw "7z l 失败（$LASTEXITCODE）" }
$cacheHits = @($cacheList | Select-String -Pattern '__pycache__|\.pyc$')
if ($cacheHits.Count -gt 0) { throw "数据卷含字节码缓存 $($cacheHits.Count) 项，拒绝出包" }
Write-Host '    · 数据卷无 __pycache__/*.pyc'

# ------------------------------------------------------------ NSIS ----
$setupExe = Join-Path $distDir "Matting-Flash-Setup-v$Version.exe"
if ($SkipInstaller) {
    Write-Warning '跳过 NSIS 安装器（-SkipInstaller）'
} else {
    Write-Step '[打包] NSIS 当前用户安装器（数据分卷同目录分发）'
    $sevenZipStandalone = Find-Standalone7za $SevenZipStandalone
    if ((Split-Path -Leaf $sevenZipStandalone) -ne '7za.exe') {
        throw "内嵌解压器必须是独立版 7za.exe：$sevenZipStandalone"
    }
    $makensis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
    if (-not $makensis) { throw '未找到 makensis（NSIS 3.x）' }
    $icon = Join-Path $Root 'desktop\src-tauri\icons\icon.ico'
    $nsi = Join-Path $PSScriptRoot 'installer\setup.nsi'
    if (-not (Test-Path -LiteralPath $nsi)) { throw "缺少 $nsi" }

    $estimatedKb = [long][math]::Floor($stagingStats.Bytes / 1KB)
    Write-Host "    · EstimatedSize = $estimatedKb KB"

    $out = & $makensis `
        "/DAPP_VERSION=$Version" `
        "/DSRC_DIR=$stagingDir" `
        "/DOUT_DIR=$distDir" `
        "/DSRC_SHELL=$stagedShell" `
        "/DSRC_7ZA=$sevenZipStandalone" `
        "/DAPP_ICON=$icon" `
        "/DESTIMATED_SIZE_KB=$estimatedKb" `
        $nsi 2>&1
    $out | Tee-Object -FilePath (Join-Path $logDir 'makensis.log') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ("makensis 失败（$LASTEXITCODE），日志：$logDir\makensis.log`n" + (($out | Select-Object -Last 15) -join "`n")) }
    if (-not (Test-Path -LiteralPath $setupExe)) { throw "未生成安装器：$setupExe" }
    $setupVersion = (Get-Item -LiteralPath $setupExe).VersionInfo.ProductVersion
    if ($setupVersion -ne $Version) { throw "安装器 ProductVersion=$setupVersion != $Version" }
    Write-Host ("    · {0} / {1} / ProductVersion {2}" -f (Split-Path -Leaf $setupExe), (Format-Size (Get-Item $setupExe).Length), $setupVersion)
}

# ------------------------------------------------- SHA256 / 清单 ----
Write-Step '[产出] SHA256SUMS（LF）与 upload-list.json'
$artifacts = @()
if (Test-Path -LiteralPath $setupExe) {
    $artifacts += Get-Item -LiteralPath $setupExe
}
$artifacts += $volumes
$sumLines = @()
$upload = @()
foreach ($file in $artifacts) {
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower()
    $sumLines += "$hash  $($file.Name)"
    $upload += [pscustomobject]@{ file = $file.Name; bytes = $file.Length; sha256 = $hash }
}
# Git Bash `sha256sum -c` 只认 LF 行尾：必须用 .NET 写入 "\n"，不能依赖 Set-Content 的 CRLF。
$sumsPath = Join-Path $distDir 'SHA256SUMS'
[System.IO.File]::WriteAllText($sumsPath, (($sumLines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding($false)))
$upload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $distDir 'upload-list.json') -Encoding utf8
$upload | Format-Table -AutoSize | Out-String -Width 200 | Write-Host

if (-not $KeepStaging) { Write-Host '    · staging 保留在 D 盘审计（可用 -KeepStaging 显式声明）' }

Write-Host ''
Write-Host ("[完成] v{0} 产物目录：{1}" -f $Version, $distDir) -ForegroundColor Green
Get-ChildItem -LiteralPath $distDir | Select-Object Name, Length | Format-Table -AutoSize | Out-String -Width 120 | Write-Host
