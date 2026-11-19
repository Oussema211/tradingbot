@echo off
title Momentum Desk
cd /d "%~dp0"
python momentum_scanner.py
if errorlevel 1 py momentum_scanner.py
pause
