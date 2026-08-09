@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
echo Starting or resuming the 8-pair default Safety comparison.
uv run python scripts\run_safety_parity.py --track default --confirm-live %*
exit /b %ERRORLEVEL%
