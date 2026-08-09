@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
where uv >nul 2>nul || (echo ERROR: uv was not found on PATH. & exit /b 1)
uv run --extra benchmarks python scripts\run_cc_only_benchmark.py agentdojo --confirm-live %*
exit /b %ERRORLEVEL%
