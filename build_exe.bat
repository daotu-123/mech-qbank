@echo off
REM Build a standalone Windows exe with PyInstaller. ASCII only: CMD garbles Chinese
REM (UTF-8 bytes are read as GBK here; multibyte comments desync line parsing).
REM Target: app/api.py (FastAPI + uvicorn, RESTful). Frontend: web/dist (vite build output).
REM --windowed: no console window on double-click. Consequences:
REM   - sys.stdout/stderr are None in the frozen app -> api.py/core.py log via _log()/_log_exc()
REM     (exe-sibling qbank.log) instead of bare print()/traceback.print_exc(), which would crash.
REM   - no Ctrl+C -> quit from the page footer button -> POST /api/v1/system/shutdown
REM     (Host-guarded; sets uvicorn should_exit, main() returns, process exits).
REM Prereq: web/dist must exist (cd web && npm run build). data/public ships the public
REM subset for first run; drop your own data\qbank.sqlite next to the exe for the full library.
cd /d "%~dp0"
if not exist web\dist\index.html (
  echo [ERROR] web\dist\index.html not found. Run "cd web ^&^& npm run build" first.
  exit /b 1
)
if not exist .venv-build (
  where py >nul 2>nul && (py -3 -m venv .venv-build) || (python -m venv .venv-build)
)
.venv-build\Scripts\python.exe -m pip install --upgrade pyinstaller
REM pymupdf (PyMuPDF) is required by the PDF worker. It is NOT a runtime dep of the
REM web process - app/pdf_extract.py is imported only inside the worker process -
REM but the worker lives in THIS exe, so pymupdf must be bundled into it.
REM Without it the frozen worker fails with ImportError and every PDF parse times out.
REM --collect-all is required (not just the module): PyMuPDF ships ~105MB of
REM binary resources (fonts, .pyd) that static analysis does not discover.
.venv-build\Scripts\python.exe -m pip install "pymupdf==2.0"
REM uvicorn's dynamic imports (http/ws protocol + lifespan auto-selection) are invisible
REM to static analysis, so declare them explicitly:
REM   httptools_impl  - httptools is installed; Config without http= auto-selects it
REM   websockets_impl - websockets is installed; ws protocol auto-selects it
REM   lifespan.on     - auto lifespan (used even without lifespan events)
.venv-build\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name mech-qbank ^
  --add-data "%~dp0web\dist;web/dist" --add-data "%~dp0data\public;data/public" ^
  --hidden-import uvicorn.protocols.http.httptools_impl ^
  --hidden-import uvicorn.protocols.websockets.websockets_impl ^
  --hidden-import uvicorn.lifespan.on ^
  --collect-all pymupdf ^
  --exclude-module tkinter --exclude-module unittest ^
  --distpath dist --workpath build --specpath build app\api.py
if not exist dist\mech-qbank.exe (
  echo [ERROR] PyInstaller did not produce dist\mech-qbank.exe
  exit /b 1
)
echo.
echo Done: dist\mech-qbank.exe
echo Windowed build: double-click opens the browser, no console window.
echo Quit via the page footer button; logs go to dist\qbank.log
echo Put your own data\qbank.sqlite next to the exe to use the full library.
echo PDF worker in this exe: "mech-qbank.exe --pdf-worker IN.pdf OUT.json"
echo   (frozen cold start measured 2026-10-20: ~7.3s first run / ~6.0s warm,
echo    vs ~2.9s non-frozen - the onefile bundle must be unpacked to a temp dir.)

