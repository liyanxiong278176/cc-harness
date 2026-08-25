@echo off
setlocal
cd /d "%~dp0.."
wsl.exe -d Ubuntu --cd "%CD%" -- env CC_HARNESS_ALLOW_OBSERVABILITY_RESUME=1 bash ./scripts/run_eval_terminal_bench_2_1_wsl.sh --task-manifest eval/harbor/catalogs/terminal_bench_2_1_hard_holdout.json --confirm-live %*
exit /b %ERRORLEVEL%
