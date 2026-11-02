@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Start.ps1" -Production
if errorlevel 1 pause

