@echo off
setlocal
title VR Log Inspector
cd /d "%~dp0"

rem ---- find Python 3.8+: the bundled one (portable package) first, then the installed one
set "PYEXE="
set "PYARGS="
if exist "%~dp0python\python.exe" (
  set "PYEXE=%~dp0python\python.exe"
  goto run
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PYEXE=py"
  set "PYARGS=-3"
  goto run
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 (
  set "PYEXE=python"
  goto run
)

echo.
echo   Python 3.8 or newer was not found on this computer.
echo.
echo   Either install it from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH" in the installer), then run this file again,
echo   or use the portable package of VR Log Inspector, which already includes Python.
echo.
start "" "https://www.python.org/downloads/"
pause
exit /b 1

:run
echo Starting VR Log Inspector. Keep this window open while you use it; close it to stop the tool.
echo.
"%PYEXE%" %PYARGS% "%~dp0vr_log_inspector.py" %*
if errorlevel 1 (
  echo.
  echo   VR Log Inspector stopped with an error. Read the message above.
  pause
)
endlocal
