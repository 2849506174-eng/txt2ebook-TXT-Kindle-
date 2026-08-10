@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title txt2ebook

echo ============================================
echo   txt2ebook - TXT to Kindle Converter
echo ============================================
echo.

REM ---------- 1. Check / install Python ----------
set "PYEXE="
python --version >nul 2>&1
if !errorlevel! equ 0 set "PYEXE=python"

REM PATH may not include a freshly installed Python, so also check the
REM standard install locations before falling back to winget.
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "C:\Program Files\Python313\python.exe" set "PYEXE=C:\Program Files\Python313\python.exe"

if not defined PYEXE (
    echo [1/2] Python not found. Installing via winget...
    winget install --id Python.Python.3.13 -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity
    REM PATH won't refresh in this window, so use the known install path.
    if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    if not defined PYEXE (
        echo.
        echo [ERROR] Python install failed or not detected.
        echo Please install Python 3.13+ from https://www.python.org/downloads/
        echo and tick Add Python to PATH, then run this file again.
        pause
        exit /b 1
    )
    echo Python installed.
) else (
    echo [1/2] Python found.
)

REM ---------- 2. Check / install Calibre (ebook-convert) ----------
set "CALOK="
where ebook-convert >nul 2>&1
if !errorlevel! equ 0 set "CALOK=1"
if not defined CALOK if exist "C:\Program Files\Calibre2\ebook-convert.exe" set "CALOK=1"
if not defined CALOK if exist "D:\Apps\Calibre2\ebook-convert.exe" set "CALOK=1"

if not defined CALOK (
    echo [2/2] Calibre not found. Installing via winget...
    winget install --id calibre.calibre -e --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity
    if !errorlevel! neq 0 (
        echo.
        echo [ERROR] Calibre install failed.
        echo Please install manually from https://calibre-ebook.com/download_windows
        pause
        exit /b 1
    )
    echo Calibre installed.
) else (
    echo [2/2] Calibre found.
)

echo.
echo ============================================
echo   Starting server...
echo   Open http://127.0.0.1:8765 in your browser
echo   Press Ctrl+C to stop
echo   (Optional: KFX format needs Amazon Kindle Previewer 3)
echo ============================================
echo.

REM Open the browser shortly after the server starts.
start "" /min cmd /c "timeout /t 2 >nul & start "" http://127.0.0.1:8765"
"%PYEXE%" server.py
pause
