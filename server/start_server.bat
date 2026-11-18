@echo off
cd /d "%~dp0"
set PORT=%1
if "%PORT%"=="" set PORT=3030

rem Password for the hidden "with user list" download (5 clicks on the title).
rem Change this to something harder to guess, or set it blank to turn that download off.
set WIFI_ADMIN_PASSWORD=password

python server.py --port %PORT%
pause
