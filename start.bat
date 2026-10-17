@echo off
REM mech-qbank launcher. ASCII only: CMD garbles Chinese text.
REM Primary path: FastAPI stack (app/api.py) in .venv-build; the venv is bootstrapped
REM on first run. If fastapi cannot be installed (no network), falls back to the
REM zero-dependency app/server.py (same core data layer, same UI).
REM The browser is opened by api.py itself (set QBANK_NO_BROWSER=1 to disable).
REM If the port is busy, the server auto-increments and prints the actual URL.
cd /d "%~dp0"
if "%QBANK_PORT%"=="" set QBANK_PORT=8640
set "PY=python"
if exist .venv-build\Scripts\python.exe set "PY=.venv-build\Scripts\python.exe"
%PY% -c "import fastapi" >nul 2>nul
if errorlevel 1 (
  echo First run: creating .venv-build and installing fastapi/uvicorn...
  where py >nul 2>nul && (py -3 -m venv .venv-build) || (python -m venv .venv-build)
  .venv-build\Scripts\python.exe -m pip install --quiet fastapi uvicorn
  set "PY=.venv-build\Scripts\python.exe"
)
%PY% -c "import fastapi" >nul 2>nul
if errorlevel 1 (
  echo fastapi unavailable - falling back to the zero-dependency app\server.py
  python app\server.py
  pause
  exit /b 1
)
echo Starting mech-qbank ^(FastAPI^) on http://127.0.0.1:%QBANK_PORT%/
echo Close this window to stop the server.
%PY% app\api.py
pause
