@echo off
title Ultimate Universal Downloader
echo ==========================================
echo Starting Ultimate Universal Downloader...
echo ==========================================

:: Navigate to the directory where this script is located
cd /d "%~dp0"

:: Run the application
python main.py

:: If the app crashes or is closed, pause so the user can see any error messages
pause
