@echo off
REM mech-qbank launcher. ASCII only: CMD garbles Chinese text.
REM Requires Python 3.10+ on PATH (python or py launcher).
REM The browser is opened by app\server.py itself (set QBANK_NO_BROWSER=1 to disable).
REM If the port is busy, the server auto-increments and prints the actual URL.
cd /d "%~dp0"
if "%QBANK_PORT%"=="" set QBANK_PORT=8640
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
echo Starting mech-qbank on http://127.0.0.1:%QBANK_PORT%/
echo Close this window to stop the server.
%PY% app\server.py
pause
