@echo off
REM اجرای ایجنت روی ویندوز (با pythonw بدون پنجره کنسول؛ برای دیدن لاگ از python استفاده کنید)
REM نمونه اولین اجرا:
REM   run_agent.bat --server ws://192.168.1.10:8788/ws --code 1234
REM اجراهای بعدی فقط:  run_agent.bat
cd /d "%~dp0"
if exist agent.json (
  pythonw agent.py %*
) else (
  python agent.py %*
  pause
)
