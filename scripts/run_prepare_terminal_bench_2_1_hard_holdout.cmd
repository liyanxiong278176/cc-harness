@echo off
setlocal
cd /d "%~dp0.."
call scripts\run_prepare_terminal_bench_2_1.cmd
if errorlevel 1 exit /b %ERRORLEVEL%
wsl.exe -d Ubuntu --cd "%CD%" -- bash -lc "source ./scripts/terminal_bench_wsl_env.sh && exec uv run --frozen python scripts/prewarm_terminal_bench_2_1.py --task-manifest eval/harbor/catalogs/terminal_bench_2_1_hard_holdout.json %*"
exit /b %ERRORLEVEL%
