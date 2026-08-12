@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title txt2ebook - update (git pull)

echo ============================================
echo   txt2ebook update
echo ============================================
echo.

REM stop the running service first so files are not locked
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like '%%python%%'\" | Where-Object { $_.CommandLine -match 'server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] git not found. Update manually: replace server.py and the
    echo         sources/ folder with the latest from GitHub, keeping
    echo         library/ output/ config.json history.json.
    pause
    exit /b 1
)

echo Pulling latest code from GitHub...
git pull
if errorlevel 1 (
    echo.
    echo [WARN] git pull failed. Possible reasons:
    echo   - this folder is not a git clone (e.g. unzipped from a package)
    echo   - local changes conflict
    echo.
    echo Manual update: download the latest from
    echo   github.com/2849506174-eng/txt2ebook-TXT-Kindle-
    echo and replace server.py + sources/, keep library/ output/ config.json.
    pause
    exit /b 1
)

echo.
echo Update OK. Your data (library/output/history) is untouched.
echo Start the service: double-click start.bat
pause
