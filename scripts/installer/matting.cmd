@echo off
REM ============================================================================
REM  matting.cmd - CLI launcher for the Matting-Flash desktop installer
REM ============================================================================
REM  Installed to <InstallDir>\matting.cmd by setup.nsi and registered on the
REM  current user's PATH, so `matting ...` works in any terminal (and for AI
REM  agents that shell out). It uses the bundled offline runtime, not the
REM  system Python.
REM
REM  Layout after install:
REM    <InstallDir>\app\runtime\python.exe        (bundled interpreter)
REM    <InstallDir>\app\app\integrated_app\cli.py (CLI source, app/app two-level)
REM    <InstallDir>\app\app\           (project root: app/, scripts/, pyproject.toml)
REM    %LOCALAPPDATA%\Matting-Flash\weights\      (user-supplied BiRefNet weights)
REM    %LOCALAPPDATA%\Matting-Flash\results\      (cutout/mask output)
REM
REM  Keep this file ASCII-only: .cmd is parsed under the active code page.
REM ============================================================================
setlocal EnableExtensions

set "APPROOT=%~dp0app"
set "PYEXE=%APPROOT%\runtime\python.exe"
set "CLIPY=%APPROOT%\app\integrated_app\cli.py"

if not exist "%PYEXE%" (
    echo [matting] ERROR: bundled runtime not found: %PYEXE% >&2
    echo [matting] Reinstall the desktop app or run the CLI from a Python venv. >&2
    exit /b 3
)
if not exist "%CLIPY%" (
    echo [matting] ERROR: CLI entry not found: %CLIPY% >&2
    exit /b 3
)

REM --- Offline / local cache environment (no network on inference) ---
set "PYTHONNOUSERSITE=1"
set "PYTHONUNBUFFERED=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "HF_HUB_OFFLINE=1"
set "MODELSCOPE_OFFLINE=1"
set "HUGGINGFACE_HUB_CACHE=%APPROOT%\cache\huggingface"
set "MODELSCOPE_CACHE=%APPROOT%\cache\modelscope"
set "TORCH_HOME=%APPROOT%\cache\torch"
set "XDG_CACHE_HOME=%APPROOT%\cache"

REM --- User data lives outside the install dir; only set when installed ---
if exist "%LOCALAPPDATA%\Matting-Flash\weights" set "MATTING_WEIGHTS_DIR=%LOCALAPPDATA%\Matting-Flash\weights"
if exist "%LOCALAPPDATA%\Matting-Flash\results" set "MATTING_RESULTS_DIR=%LOCALAPPDATA%\Matting-Flash\results"

"%PYEXE%" "%CLIPY%" %*
exit /b %ERRORLEVEL%
