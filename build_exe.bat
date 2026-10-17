@echo off
REM Build a standalone Windows exe with PyInstaller. ASCII only: CMD garbles Chinese.
REM --windowed: no console window on double-click. Consequences:
REM   - sys.stdout/stderr are None in the frozen app -> server.py logs via _log()/_log_exc()
REM     (exe-sibling qbank.log) instead of bare print()/traceback.print_exc(), which would crash.
REM   - no Ctrl+C -> quit from the page's footer button -> GET /api/shutdown (Host-guarded).
cd /d "%~dp0"
if not exist .venv-build (
  where py >nul 2>nul && (py -3 -m venv .venv-build) || (python -m venv .venv-build)
)
.venv-build\Scripts\python.exe -m pip install --upgrade pyinstaller
.venv-build\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name mech-qbank ^
  --add-data "%~dp0web;web" --add-data "%~dp0data\public;data\public" ^
  --exclude-module tkinter --exclude-module unittest ^
  --distpath dist --workpath build --specpath build app\server.py
echo.
echo Done: dist\mech-qbank.exe
echo Windowed build: double-click opens the browser, no console window.
echo Quit via the page footer button; logs go to dist\qbank.log
echo Put your own data\qbank.sqlite next to the exe to use the full library.
pause
