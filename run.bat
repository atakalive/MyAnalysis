@echo off
rem MyAnalysis GUI launcher
setlocal

rem Move to this script's directory so double-click launch works reliably
cd /d "%~dp0"

rem Use .venv if present, otherwise fall back to global python
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

python tool.py
set EXITCODE=%ERRORLEVEL%

rem On error, keep the window open so the message can be read
if not "%EXITCODE%"=="0" (
    echo.
    echo [run.bat] tool.py exited with code %EXITCODE%.
    pause
)

endlocal
exit /b %EXITCODE%
