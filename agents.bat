@echo off
REM FlowChartCreation - manage agents (local AI assistants)
REM Examples:  agents.bat add "Planner"      agents.bat list      agents.bat grant "Planner" <diagram-id>
REM            agents.bat log "Planner"      agents.bat revoke "Planner" all      agents.bat remove "Planner"
chcp 65001 >nul
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY ( where python >nul 2>nul && set PY=python )
if not defined PY (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
    pause
    exit /b 1
)
if "%~1"=="" (
    %PY% server.py agent --help
    echo.
    %PY% server.py agent list
    echo.
    echo Example: agents.bat add "Planner"
    pause
    exit /b 0
)
%PY% server.py agent %*
