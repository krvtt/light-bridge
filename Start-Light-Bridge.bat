@echo off
setlocal
cd /d "%~dp0"
title Light Bridge

rem Python helper files live in %LOCALAPPDATA%, not in this folder (keeps OneDrive and backups clean).
set "VENV=%LOCALAPPDATA%\LightBridge\venv"

set "OPEN=--open-browser"
set "ARGS=%*"
if /i "%~1"=="--autostart" (
  set "OPEN="
  set "ARGS="
)

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython

rem If Python was updated or removed since the first start, prepare everything again.
if exist "%VENV%\ready.txt" (
  "%VENV%\Scripts\python.exe" -c "import bleak" >nul 2>nul
  if errorlevel 1 (
    echo  Python has changed since the last start. Preparing again...
    rmdir /s /q "%VENV%"
  )
)

if not exist "%VENV%\ready.txt" (
  echo.
  echo  First start: getting everything ready. This takes about one minute...
  echo.
  %PY% -m venv "%VENV%"
  if errorlevel 1 goto :fail
  "%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto :fail
  copy /y requirements.txt "%VENV%\requirements.txt" >nul
  echo ready> "%VENV%\ready.txt"
)

rem A new version of Light Bridge may need other helper files.
fc /b requirements.txt "%VENV%\requirements.txt" >nul 2>nul
if errorlevel 1 (
  echo  Updating helper files...
  "%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 goto :fail
  copy /y requirements.txt "%VENV%\requirements.txt" >nul
)

"%VENV%\Scripts\python.exe" -m light_bridge %OPEN% %ARGS%
echo.
pause
exit /b 0

:nopython
echo.
echo  Python is not installed yet.
echo  Please install it first: see "Setting up the PC" in the guide (PDF).
echo  Download: https://www.python.org/downloads/
echo.
pause
exit /b 1

:fail
echo.
echo  Setup did not work. Is the PC connected to the internet? Then try again.
echo  If it keeps failing, delete the folder %VENV% and start again.
echo.
pause
exit /b 1
