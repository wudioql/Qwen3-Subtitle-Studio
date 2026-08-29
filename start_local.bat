@echo off
REM Windows local one-click startup script (activate .venv first, then launch GUI)
REM Requirements: Python 3.12 installed + .venv created (py -3.12 -m venv .venv)
REM Usage: double-click this file, or run .\start_local.bat in project root

setlocal

REM 1. Activate virtual environment (relative to script directory)
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else (
    echo [ERROR] .venv not found. Run: py -3.12 -m venv .venv
    echo [ERROR] Then install dependencies: pip install -r requirements.txt
    pause
    exit /b 1
)

REM 2. Quick dependency check (optional, does not block startup)
echo === Quick environment check ===
python -c "import torch; print('torch:', torch.__version__)" 2>nul || echo [WARN] torch not loaded correctly (does not affect subtitle-only mode)
python -c "import PySide6; print('PySide6: OK')" 2>nul || echo [WARN] PySide6 missing
python -c "import pyqtgraph; print('pyqtgraph: OK')" 2>nul || echo [WARN] pyqtgraph missing

echo === Starting Qwen3-Subtitle-Studio ===
REM Use pythonw.exe (no console window) for GUI; better for double-click experience.
REM For debugging (see console logs), change pythonw.exe to python.exe
.venv\Scripts\pythonw.exe main.py
if errorlevel 1 (
    echo [FALLBACK] pythonw failed, trying python (with console)...
    .venv\Scripts\python.exe main.py
)

endlocal
pause
