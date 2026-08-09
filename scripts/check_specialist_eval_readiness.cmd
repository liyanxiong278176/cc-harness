@echo off
setlocal
cd /d "%~dp0.."
set PYTHONUTF8=1
uv run python scripts\check_specialist_eval_readiness.py %*
exit /b %errorlevel%
