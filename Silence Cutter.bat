@echo off
title Silence Cutter
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul || (echo Python was not found. Install Python 3 and try again. & pause & exit /b 1)
where ffmpeg >nul 2>nul || (echo FFmpeg was not found. Install FFmpeg and try again. & pause & exit /b 1)

rem A video dropped onto this file arrives as %1
set "p=%~1"
if defined p if exist "%p%" goto run

:ask
echo.
set "p="
set /p "p=Drag your video here (or paste its path) and press Enter: "
if not defined p goto ask
set "p=%p:"=%"
if not exist "%p%" (
    echo File not found: "%p%"
    goto ask
)

:run
python silence_cutter.py "%p%"
rem Close on success or cancel; stay open on an error so it can be read
if errorlevel 1 pause
