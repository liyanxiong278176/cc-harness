@echo off
setlocal

cd /d "%~dp0.."

set "OUTPUT=%CD%\eval\result\harbor-verified500-deepseek-v4-flash"
set "CATALOG=%CD%\eval\harbor\catalogs\swebench_verified_500.json"
set "WHEEL=%CD%\eval\result\harbor-wheel-verified500\cc_harness-0.1.0-py3-none-any.whl"

where uv >nul 2>nul
if errorlevel 1 (
    echo ERROR: uv was not found on PATH.
    exit /b 1
)
if not exist "%CATALOG%" (
    echo ERROR: frozen task catalog not found: %CATALOG%
    exit /b 1
)
if not exist "%WHEEL%" (
    echo ERROR: cc-harness wheel not found: %WHEEL%
    exit /b 1
)
if not exist "%CD%\.env" (
    echo ERROR: project .env not found: %CD%\.env
    exit /b 1
)
if not exist "%USERPROFILE%\.claude\settings.json" (
    echo ERROR: Claude settings not found: %USERPROFILE%\.claude\settings.json
    exit /b 1
)
if exist "%OUTPUT%" if not exist "%OUTPUT%\state.json" (
    dir /b "%OUTPUT%" 2>nul | findstr . >nul
    if not errorlevel 1 (
        echo ERROR: output exists and is nonempty but has no state.json: %OUTPUT%
        exit /b 1
    )
)

set "PYTHONUTF8=1"

if /i not "%~1"=="--check" goto live
uv run python scripts\run_harbor_verified500.py --check
exit /b %ERRORLEVEL%

:live
echo Starting or resuming the frozen SWE-bench Verified 500 comparison.
echo Press Ctrl+C to stop safely. Run this same command again to resume.
echo Results: %OUTPUT%

uv run python scripts\run_harbor_verified500.py --confirm-live %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo Evaluation exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%
