@echo off
REM اجرای سرور مرکزی LocalTools روی ویندوز
REM نمونه: run_server.bat --port 8788 --code 1234 --ssl
cd /d "%~dp0"
python server.py %*
pause
