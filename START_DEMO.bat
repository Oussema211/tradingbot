@echo off
title Momentum Desk (demo)
cd /d "%~dp0"
python momentum_scanner.py --demo
if errorlevel 1 py momentum_scanner.py --demo
pause
