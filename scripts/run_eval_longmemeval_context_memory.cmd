@echo off
setlocal
cd /d "%~dp0\.."
uv run python scripts\run_context_memory_benchmark.py longmemeval --confirm-live %*
exit /b %errorlevel%
