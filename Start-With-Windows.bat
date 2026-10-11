@echo off
rem Start Light Bridge automatically (minimised) when you sign in to Windows.
set "TARGET=%~dp0Start-Light-Bridge.bat"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Light Bridge.lnk'); $s.TargetPath=$env:TARGET; $s.Arguments='--autostart'; $s.WorkingDirectory=(Split-Path $env:TARGET); $s.WindowStyle=7; $s.Save()"
if errorlevel 1 (
  echo Could not create the autostart entry.
) else (
  echo Done. Light Bridge now starts automatically when you sign in to Windows.
)
pause
