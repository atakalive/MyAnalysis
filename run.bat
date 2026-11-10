@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if not defined PYTHONPYCACHEPREFIX set "PYTHONPYCACHEPREFIX=%~dp0data\pycache"
if not exist ".venv\Scripts\pythonw.exe" goto missing
start "" ".venv\Scripts\pythonw.exe" -m installer.core --launch --install-dir "%~dp0." %*
exit /b 0
:missing
echo MyAnalysis environment is missing. Run setup.bat to install or repair it.
pause
exit /b 1
