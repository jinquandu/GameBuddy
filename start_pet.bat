@echo off
rem dashijie desktop pet launcher (avoids the Microsoft Store python stub)
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -X utf8 -m toolbox pet
  goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
  python -X utf8 -m toolbox pet
  goto :end
)
echo [ERROR] No Python launcher found. Install from https://www.python.org/downloads/
pause
:end
