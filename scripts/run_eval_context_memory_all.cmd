@echo off
setlocal
cd /d "%~dp0\.."
set "run_status=0"
call scripts\run_eval_longmemeval_context_memory.cmd %*
if errorlevel 1 set "run_status=1"
call scripts\run_eval_locomo_context_memory.cmd %*
if errorlevel 1 set "run_status=1"
call scripts\run_eval_memoryagentbench_context_memory.cmd %*
if errorlevel 1 set "run_status=1"
uv run python scripts\run_context_memory_benchmark.py aggregate %*
if errorlevel 1 set "run_status=1"
exit /b %run_status%
