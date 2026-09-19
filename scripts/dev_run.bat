@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev_run.ps1" %*
exit /b %ERRORLEVEL%
