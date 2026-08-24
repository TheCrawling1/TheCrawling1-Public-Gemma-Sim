@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM --- Locate Python -------------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if "%PY%"=="" (
    where python >nul 2>nul && set "PY=python"
)
if "%PY%"=="" (
    echo ERROR: Python is not on PATH. Install Python 3.10+ from https://www.python.org/.
    pause
    exit /b 1
)

REM --- Create venv on first run -------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo ERROR: failed to create venv.
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

REM --- Install / update dependencies --------------------------------------
echo Installing dependencies ...
python -m pip install --upgrade pip --quiet --disable-pip-version-check
python -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

REM --- Run server ---------------------------------------------------------
REM GEMMASIM_LAUNCHER tells the app to request a restart by exiting with code 42
REM (the "Update & restart" button) instead of trying to re-exec itself, which
REM doesn't work on Windows. We relaunch here whenever that code comes back.
set "GEMMASIM_LAUNCHER=1"
echo.
echo GemmaSim is starting on http://127.0.0.1:5000
echo Press CTRL+C to stop.
echo.
:runloop
python run.py
if not "%errorlevel%"=="42" goto :serverstopped
echo.
echo Restarting GemmaSim...
echo.
goto runloop
:serverstopped
pause
endlocal
