@echo off
rem dashijie desktop pet launcher (windowless: no persistent console)
rem pyw/pythonw = 无控制台解释器；start "" = 脱离启动，本窗口闪退即走
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
  start "" pyw -3 -X utf8 -m toolbox pet
  goto :end
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw -X utf8 -m toolbox pet
  goto :end
)
echo [ERROR] No Python launcher found. Install from https://www.python.org/downloads/
pause
:end
