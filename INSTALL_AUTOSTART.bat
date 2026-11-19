@echo off
rem Starts Momentum Desk silently (no window) every time you log in to Windows.
rem Remove it again with UNINSTALL_AUTOSTART.bat. Dashboard: http://localhost:8765  Log: desk.log
cd /d "%~dp0"
set "PYW="
for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%p"
if not defined PYW (
  echo Could not find pythonw.exe. Reinstall Python from python.org with "Add to PATH" ticked.
  pause & exit /b 1
)
schtasks /Create /F /TN "MomentumDesk" /SC ONLOGON /RL LIMITED /TR "\"%PYW%\" \"%~dp0momentum_scanner.py\" --no-browser" >nul
if errorlevel 1 ( echo Failed to create the task. & pause & exit /b 1 )
schtasks /Run /TN "MomentumDesk" >nul
echo.
echo  Done. Momentum Desk now runs in the background and starts with Windows.
echo  Dashboard: http://localhost:8765
echo  Tip: in Windows power settings, set "Sleep" to Never while plugged in, or alerts stop when the PC sleeps.
pause
