@echo off
schtasks /End /TN "MomentumDesk" >nul 2>&1
schtasks /Delete /F /TN "MomentumDesk"
taskkill /F /FI "WINDOWTITLE eq Momentum Desk*" >nul 2>&1
echo Autostart removed. (If the dashboard still answers, restart Windows or end pythonw.exe in Task Manager.)
pause
