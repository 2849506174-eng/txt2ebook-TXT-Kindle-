@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title txt2ebook - offline update from ZIP

echo ============================================
echo   txt2ebook offline update (no GitHub needed)
echo ============================================
echo.
echo  Steps:
echo   1. Get the new version as a ZIP (from a friend / netdisk / QQ)
echo      e.g. txt2ebook-TXT-Kindle-main.zip
echo   2. Put the ZIP anywhere (this folder, Desktop, Downloads...)
echo   3. Run this script again - it will find it and update.
echo.
echo  Your data is kept: library/ output/ config.json history.json
echo.

REM stop the running service so files are not locked
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name like '%%python%%'\" | Where-Object { $_.CommandLine -match 'server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

REM look for a zip: given path > current folder > desktop > downloads
set "ZIP=%~1"
if not defined ZIP set "ZIP="
if not defined ZIP for %%f in ("%~dp0*.zip" "%~dp0txt2ebook*.zip") do if not defined ZIP if exist "%%f" set "ZIP=%%f"
if not defined ZIP if exist "%USERPROFILE%\Desktop\*.zip" for %%f in ("%USERPROFILE%\Desktop\*.zip") do if not defined ZIP if exist "%%f" set "ZIP=%%f"
if not defined ZIP if exist "%USERPROFILE%\Downloads\*.zip" for %%f in ("%USERPROFILE%\Downloads\*.zip") do if not defined ZIP if exist "%%f" set "ZIP=%%f"

if not defined ZIP (
    echo [ERROR] No ZIP found.
    echo Drag-and-drop the new ZIP onto this script to update.
    pause
    exit /b 1
)

echo Found: %ZIP%
echo Extracting...
set "TMPD=%TEMP%\txt2ebook_update_%RANDOM%"
powershell -NoProfile -Command "Expand-Archive -LiteralPath '%ZIP%' -DestinationPath '%TMPD%' -Force" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Extract failed. Make sure the file is a valid ZIP.
    pause
    exit /b 1
)

REM find server.py inside the extracted folder
set "SRC="
for /r "%TMPD%" %%f in (server.py) do if not defined SRC if exist "%%f" set "SRC=%%~dpf"
if not defined SRC (
    echo [ERROR] No server.py found inside the ZIP.
    rmdir /s /q "%TMPD%" >nul 2>&1
    pause
    exit /b 1
)

echo Copying server.py and sources/ ...
copy /y "%SRC%server.py" "%~dp0server.py" >nul
if exist "%SRC%sources" (
    if not exist "%~dp0sources" mkdir "%~dp0sources"
    copy /y "%SRC%sources\*.json" "%~dp0sources\" >nul 2>&1
)
echo Copying helper scripts (uninstall/update) ...
copy /y "%SRC%uninstall.py" "%~dp0uninstall.py" >nul 2>&1
copy /y "%SRC%uninstall.bat" "%~dp0uninstall.bat" >nul 2>&1
copy /y "%SRC%install_render.bat" "%~dp0install_render.bat" >nul 2>&1

rmdir /s /q "%TMPD%" >nul 2>&1

echo.
echo ============================================
echo   Update OK - your data is untouched.
echo   Start the service: double-click start.bat
echo ============================================
pause
