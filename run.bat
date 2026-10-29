@echo off
REM ------------------------------------------------------------------
REM run.bat - double-click this file to start the Sovereign Workbench.
REM
REM It (1) activates the project's private Python environment (.venv),
REM and (2) starts the web server on 127.0.0.1:8000 (this computer only).
REM Then open http://127.0.0.1:8000 in your browser.
REM Press Ctrl+C in this window to stop the server.
REM ------------------------------------------------------------------

REM Move into the folder this .bat file lives in, wherever that is.
cd /d "%~dp0"

IF NOT EXIST ".venv\Scripts\activate.bat" (
    echo [!] The .venv folder was not found.
    echo     Follow Step 4 in README.md to create it first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

echo.
echo  Sovereign Workbench is starting...
echo  Open http://127.0.0.1:8000 in your browser.
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

pause
