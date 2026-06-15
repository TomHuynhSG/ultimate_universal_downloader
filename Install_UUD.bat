@echo off
title Ultimate Universal Downloader - Installer
echo ==========================================
echo Installing Ultimate Universal Downloader...
echo ==========================================
echo.

:: Navigate to the directory where this script is located
cd /d "%~dp0"

echo [1/2] Installing Python packages from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install Python packages. Please ensure Python is installed and added to PATH.
    pause
    exit /b %errorlevel%
)
echo.

echo [2/2] Installing Playwright Chromium browser...
playwright install chromium
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install Playwright browser.
    pause
    exit /b %errorlevel%
)
echo.

echo ==========================================
echo Installation Complete!
echo You can now use Start_UUD.bat to launch the application.
echo ==========================================
pause
