@echo off
REM Family Office - Windows launcher.
REM Double-click this file. A command window opens, sets everything up the first
REM time, asks for your password, and opens the app in your browser.

setlocal enableextensions
REM Works whether this launcher sits at the app root or inside launchers\.
if exist "%~dp0pyproject.toml" ( cd /d "%~dp0" ) else ( cd /d "%~dp0\.." )

echo Family Office - starting up...

REM 1. Ensure uv (fast Python installer) is available (installs to %USERPROFILE%\.local\bin).
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 (
  echo Installing uv ^(one-time^)...
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

REM 2. Create the virtual environment and install the exact locked dependencies.
echo Preparing environment ^(first run may take a few minutes^)...
uv sync --frozen
if errorlevel 1 (
  echo.
  echo Failed to prepare the environment. Make sure you have an internet connection.
  pause
  exit /b 1
)

REM 3. Launch the app (prompts for your password, opens the browser).
uv run --frozen python launchers\_run.py

echo.
echo The app has stopped. You can close this window.
pause
