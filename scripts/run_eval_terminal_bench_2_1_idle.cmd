@echo off
setlocal
cd /d "%~dp0.."
wsl.exe -d Ubuntu --cd "%CD%" -- bash -lc "source ./scripts/terminal_bench_wsl_env.sh && exec uv run --frozen python scripts/terminal_bench_idle_schedule.py %*"
exit /b %ERRORLEVEL%
