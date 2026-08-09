@echo off
setlocal
cd /d "%~dp0.."

set "OUTPUT=%CD%\eval\result\specialist-memory34-v6-deepseek-v4-flash"
call :preflight
if errorlevel 1 exit /b 1
set "PYTHONUTF8=1"

if /i "%~1"=="--check" (
    uv run python scripts\run_specialist_parity.py --suite memory --check
    exit /b %ERRORLEVEL%
)

echo Starting or resuming the 34-pair Memory comparison.
echo Press Ctrl+C to stop safely. Run this same command again to resume.
echo Results: %OUTPUT%
uv run python scripts\run_specialist_parity.py --suite memory --confirm-live %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo Evaluation exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%

:preflight
where uv >nul 2>nul || (echo ERROR: uv was not found on PATH. & exit /b 1)
if not exist "%CD%\.env" (echo ERROR: project .env not found. & exit /b 1)
if not exist "%USERPROFILE%\.claude\settings.json" (echo ERROR: Claude settings not found. & exit /b 1)
if exist "%OUTPUT%" if not exist "%OUTPUT%\state.json" (
    dir /b "%OUTPUT%" 2>nul | findstr . >nul
    if not errorlevel 1 (echo ERROR: output is nonempty but has no state.json: %OUTPUT% & exit /b 1)
)
exit /b 0
