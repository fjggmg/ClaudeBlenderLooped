@echo off
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

REM --- First-run setup: create venv + install if missing -------------------
if not exist "%PY%" (
    echo [blendahbot] First run: creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [blendahbot] Could not create the virtual environment.
        echo             Make sure Python 3.10+ is installed and on PATH.
        goto :end
    )
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -e .
)

REM Ensure the package is importable; reinstall if something is off.
"%PY%" -c "import blendahbot" 1>nul 2>nul
if errorlevel 1 (
    echo [blendahbot] Installing blendahbot...
    "%PY%" -m pip install -e .
)

REM --- Auth mode: start.bat auth -> one-time subscription login -------------
if /i "%~1"=="auth" (
    "%PY%" -m blendahbot --auth
    goto :end
)

REM --- Pass-through mode: start.bat "a red car" --budget 5 ------------------
if not "%~1"=="" (
    "%PY%" -m blendahbot %*
    goto :end
)

REM --- Interactive mode: ask for the request, then run ---------------------
echo.
echo ===================== blendahbot =====================
echo  Describe what to build. While it works you can type
echo  MORE instructions any time and press Enter to steer
echo  the agent. Type  /stop  to finish early.
echo ======================================================
echo.
set "REQUEST="
set /p "REQUEST=What should blendahbot build? "

if "%REQUEST%"=="" (
    echo No request entered. Exiting.
    goto :end
)

echo.
"%PY%" -m blendahbot "%REQUEST%"

:end
echo.
pause
endlocal
