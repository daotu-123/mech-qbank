@echo off
REM Build a standalone Windows exe with PyInstaller. ASCII only: CMD garbles Chinese.
cd /d "%~dp0"
if not exist .venv-build (
  where py >nul 2>nul && (py -3 -m venv .venv-build) || (python -m venv .venv-build)
)
.venv-build\Scripts\python.exe -m pip install --upgrade pyinstaller
.venv-build\Scripts\pyinstaller.exe --noconfirm --onefile --name mech-qbank ^
  --add-data "%~dp0web;web" --add-data "%~dp0data\public;data\public" ^
  --exclude-module tkinter --exclude-module unittest ^
  --distpath dist --workpath build --specpath build app\server.py
echo.
echo Done: dist\mech-qbank.exe
echo Put your own data\qbank.sqlite next to the exe to use the full library.
pause
