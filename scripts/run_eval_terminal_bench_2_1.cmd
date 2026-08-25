@echo off
setlocal
cd /d "%~dp0.."
wsl.exe -d Ubuntu --cd "%CD%" -- bash ./scripts/run_eval_terminal_bench_2_1_wsl.sh %*
exit /b %ERRORLEVEL%
