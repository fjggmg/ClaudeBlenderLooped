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

REM --- Shortcut modes: start.bat auth | settings | "a red car" --budget 5 ---
if /i "%~1"=="auth"     ( "%PY%" -m blendahbot --auth     & goto :end )
if /i "%~1"=="settings" ( "%PY%" -m blendahbot --settings & goto :end )
if not "%~1"=="" ( "%PY%" -m blendahbot %* & goto :end )

REM --- Interactive menu ----------------------------------------------------
:menu
echo.
echo ===================== blendahbot =====================
echo   [1] Build something
echo   [2] Settings  (API key, budget, model, quality...)
echo   [3] Log in    (Claude subscription)
echo   [Q] Quit
echo ======================================================
set "CHOICE="
set /p "CHOICE=Choose: "
if /i "%CHOICE%"=="1" goto :build
if /i "%CHOICE%"=="2" goto :settings
if /i "%CHOICE%"=="3" goto :doauth
if /i "%CHOICE%"=="q" goto :end
goto :menu

:settings
"%PY%" -m blendahbot --settings
goto :menu

:doauth
"%PY%" -m blendahbot --auth
goto :menu

:build
echo.
echo Describe what to build. While it works you can type MORE instructions
echo any time and press Enter to steer the agent. Type  /stop  to finish early.
echo.
set "REQUEST="
set /p "REQUEST=What should blendahbot build? "
if "%REQUEST%"=="" goto :menu
echo.
"%PY%" -m blendahbot "%REQUEST%"

:end
echo.
pause
endlocal
