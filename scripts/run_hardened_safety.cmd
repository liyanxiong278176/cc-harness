@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
echo Starting or resuming the 8-task hardened Safety conformance run.
uv run python scripts\run_safety_parity.py --track hardened --confirm-live %*
exit /b %ERRORLEVEL%
