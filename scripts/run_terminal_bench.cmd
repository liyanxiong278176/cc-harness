@echo off
setlocal
cd /d "%~dp0.."
wsl.exe -d Ubuntu --cd "%CD%" -- bash ./scripts/run_terminal_bench.sh %*
exit /b %ERRORLEVEL%
