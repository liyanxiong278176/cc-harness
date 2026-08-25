@echo off
setlocal
cd /d "%~dp0.."
call scripts\run_eval_terminal_bench_2_1.cmd --task-manifest eval/harbor/catalogs/terminal_bench_2_1_easy_medium.json %*
exit /b %ERRORLEVEL%
