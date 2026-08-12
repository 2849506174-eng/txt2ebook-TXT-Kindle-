@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title txt2ebook uninstaller

REM find Python (prefer standard install paths over the WindowsApps stub)
set "PYEXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "C:\Program Files\Python313\python.exe" set "PYEXE=C:\Program Files\Python313\python.exe"
if not defined PYEXE (
    python --version >nul 2>&1
    if !errorlevel! equ 0 set "PYEXE=python"
)
if not defined PYEXE (
    echo [ERROR] Python not found. Uninstall Python manually, then delete this folder.
    pause
    exit /b 1
)

"%PYEXE%" uninstall.py
