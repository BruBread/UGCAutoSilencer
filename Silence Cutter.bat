@echo off
title Silence Cutter
cd /d "%~dp0"

where python >nul 2>nul || (echo Python was not found. Install Python 3 and try again. & pause & exit /b 1)
where ffmpeg >nul 2>nul || (echo FFmpeg was not found. Install FFmpeg and try again. & pause & exit /b 1)

:ask
echo.
set "p="
set /p "p=Drag your video here (or paste its path) and press Enter: "
if not defined p goto ask
set "p=%p:"=%"
if not exist "%p%" (
    echo File not found: %p%
    goto ask
)

python silence_cutter.py "%p%"
echo.
pause
