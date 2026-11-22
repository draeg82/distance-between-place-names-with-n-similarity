@echo off
REM Windows: one command from a fresh clone to the app in a browser:  run
REM Options: set PORT=9000 before running to change the port.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1"
