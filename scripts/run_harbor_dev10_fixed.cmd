@echo off
setlocal

cd /d "%~dp0.."

set "OUTPUT=%CD%\eval\result\harbor-dev10-20260806"
set "BACKUP=%CD%\eval\result\harbor-dev10-20260806-pre-fix"
set "WHEEL=%CD%\eval\result\harbor-wheel-dev10-fixed\cc_harness-0.1.0-py3-none-any.whl"

if not exist "%WHEEL%" (
    echo ERROR: fixed wheel not found: %WHEEL%
    exit /b 1
)

if /i "%~1"=="--check" (
    echo Command Prompt launcher is ready.
    echo Wheel: %WHEEL%
    echo Results: %OUTPUT%
    exit /b 0
)

if not exist "%BACKUP%" (
    if not exist "%OUTPUT%" (
        echo ERROR: previous result directory not found: %OUTPUT%
        exit /b 1
    )
    echo Archiving the pre-fix result to:
    echo   %BACKUP%
    move "%OUTPUT%" "%BACKUP%" >nul
    if errorlevel 1 (
        echo ERROR: failed to archive the previous result.
        exit /b 1
    )
) else (
    if exist "%OUTPUT%" (
        if not exist "%OUTPUT%\state.json" (
            echo ERROR: output exists without state.json: %OUTPUT%
            exit /b 1
        )
    )
)

set "PYTHONUTF8=1"

echo Starting or resuming the fixed dev10 comparison.
echo Results: %OUTPUT%

uv run python scripts\run_harbor_parity.py ^
  --output-root eval\result\harbor-dev10-20260806 ^
  --wheel eval\result\harbor-wheel-dev10-fixed\cc_harness-0.1.0-py3-none-any.whl ^
  --task swe-bench/matplotlib__matplotlib-14623 ^
  --task swe-bench/astropy__astropy-12907 ^
  --task swe-bench/django__django-16938 ^
  --task swe-bench/sympy__sympy-16886 ^
  --task swe-bench/psf__requests-1142 ^
  --task swe-bench/pytest-dev__pytest-7236 ^
  --task swe-bench/scikit-learn__scikit-learn-15100 ^
  --task swe-bench/sphinx-doc__sphinx-9230 ^
  --task swe-bench/pydata__xarray-3151 ^
  --task swe-bench/pylint-dev__pylint-4604 ^
  --repetitions 1 ^
  --random-seed 20260806 ^
  --maximum-attempts 2 ^
  --cooldown-seconds 30 ^
  --suite dev ^
  --confirm-live

set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo Evaluation exited with code %EXIT_CODE%.
exit /b %EXIT_CODE%
