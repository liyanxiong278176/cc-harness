@echo off
setlocal

cd /d "%~dp0.."

set "OUTPUT=%CD%\eval\result\specialist117-v5-deepseek-v4-flash"

where uv >nul 2>nul
if errorlevel 1 (
    echo ERROR: uv was not found on PATH.
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
uv run python scripts\run_specialist_parity.py --check
exit /b %ERRORLEVEL%

:live
echo Starting or resuming the frozen 117-task specialist comparison.
echo Press Ctrl+C to stop safely. Run this same command again to resume.
echo Results: %OUTPUT%

uv run python scripts\run_specialist_parity.py --confirm-live %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo Evaluation exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%
