@echo off
REM FlowChartCreation - share and edit diagrams together on the local network (Windows)
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py --open %*
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        python server.py --open %*
    ) else (
        echo Python 3 was not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
    )
)
pause
