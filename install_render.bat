@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title txt2ebook - install browser render (optional)

echo ============================================
echo   txt2ebook - browser render component
echo   (optional: needed only for JS-loaded sites)
echo ============================================
echo.

set "PYEXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "C:\Program Files\Python313\python.exe" set "PYEXE=C:\Program Files\Python313\python.exe"
if not defined PYEXE (
    python --version >nul 2>&1
    if !errorlevel! equ 0 set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] Python not found. Run start.bat first.
    pause
    exit /b 1
)

echo [1/2] Installing Playwright (small Python library)...
"%PYEXE%" -m pip install playwright
if !errorlevel! neq 0 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo [2/2] Checking for Chrome / Edge ...
"%PYEXE%" -c "import os,sys; p=[r'C:\Program Files\Google\Chrome\Application\chrome.exe',r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',r'C:\Program Files\Microsoft\Edge\Application\msedge.exe']; print('FOUND' if any(os.path.isfile(x) for x in p) else 'NONE')" > "%TEMP%\pw_chk.txt" 2>nul
set /p PWCHK=<"%TEMP%\pw_chk.txt"
del "%TEMP%\pw_chk.txt" >nul 2>&1

if "%PWCHK%"=="FOUND" (
    echo   Chrome/Edge found - no browser download needed.
    echo   Browser render is ready. Restart the service.
) else (
    echo   No Chrome/Edge found. Downloading Chromium (~150MB, one-time)...
    "%PYEXE%" -m playwright install chromium
)

echo.
echo Done. Restart the service (close and double-click start.bat).
pause
