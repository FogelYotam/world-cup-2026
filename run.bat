@echo off
REM מריץ את מערכת המונדיאל עם סביבת ה-venv ופלט עברית תקין
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" main.py %*
