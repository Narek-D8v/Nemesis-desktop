@echo off
REM ============================================================
REM  Nemesis (Nemyusya) launcher
REM ============================================================
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1

if not exist "venv\Scripts\python.exe" (
    echo [*] Creating virtual environment...
    py -3.13 -m venv venv
    if errorlevel 1 goto error
    venv\Scripts\python.exe -m pip install --upgrade pip
)

echo [*] Installing dependencies...
venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
if errorlevel 1 goto error

echo [*] Starting Nemesis...
venv\Scripts\python.exe -m nemus %*
if errorlevel 1 goto error
goto end

:error
echo.
echo [X] Startup failed.
echo Check that Python 3.13 and network are available.
pause

:end
endlocal
