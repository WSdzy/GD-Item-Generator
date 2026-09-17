@echo off
setlocal
cd /d "%~dp0"

set "PYTHONW=D:\Program Files (x86)\Python\pythonw.exe"
if not exist "%PYTHONW%" set "PYTHONW=pythonw.exe"

start "" "%PYTHONW%" -X utf8 "%~dp0gd_gui.py"
start "" "%PYTHONW%" -X utf8 "%~dp0visibility_probe.py"
endlocal
