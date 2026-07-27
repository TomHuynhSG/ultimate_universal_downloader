@echo off
setlocal
title Ultimate Universal Downloader

pushd "%~dp0" || (
    echo ERROR: Cannot open the project directory: %~dp0
    pause
    exit /b 1
)

if not exist "main.py" (
    echo ERROR: main.py is missing from %CD%
    pause
    popd
    exit /b 1
)

where python >nul 2>&1 || (
    echo ERROR: Python is not available on PATH.
    pause
    popd
    exit /b 1
)

python main.py
set "exitCode=%ERRORLEVEL%"
if not "%exitCode%"=="0" (
    echo.
    echo Ultimate Universal Downloader exited with code %exitCode%.
    pause
)

popd
exit /b %exitCode%
