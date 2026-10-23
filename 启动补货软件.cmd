@echo off
cd /d "%~dp0"
python "%~dp0app\launcher.py"
if errorlevel 1 pause
