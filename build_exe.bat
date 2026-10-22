@echo off
rem GameBuddy build (core, no ASR voice stack; torch/funasr excluded;
rem pipeline auto-skips asr/speaker/voice, OCR knockdown/pickup/push intact).
rem
rem Output:
rem   dist\GameBuddy\GameBuddy.exe      (onedir; ship the whole folder)
rem   dist\GameBuddy-core.zip           (portable ZIP, forward-slash entries)
rem   dist\release-manifest.txt         (entries/size/sha256/flags)
rem
rem REQ-20261022-009 changes vs the old bat:
rem   - build_exe.spec carries the EXE version resource + icon, so r5 vs r7
rem     can be told apart in Explorer.
rem   - the ZIP is written by tools\make_release.py (zipfile) because
rem     PowerShell Compress-Archive writes backslash separators, which
rem     violates the ZIP spec and breaks non-Windows extractors. Entries are
rem     validated: no backslash, no absolute path, no "..", no duplicates.
rem   - the user-facing quick-start doc is added to the archive.
rem
rem NOTE: keep this file ASCII-only. cmd.exe reads .bat as the OEM codepage
rem (GBK here); UTF-8 Chinese in rem/echo lines is shredded into commands.
rem
rem GOTCHA 1: close every process touching dist before building (running app,
rem log viewers). COLLECT wipes the dir; a locked file aborts it midway.
rem GOTCHA 2: the final echo is unconditional - verify product mtimes.
rem GOTCHA 3: never ship data\ (device identity, session, queue, logs).
rem The zip is built before any dev data is restored; the leak scan is the
rem hard gate (see docs\PACKAGING.md section 3).
cd /d "%~dp0"

rem Back up dev data (device identity/login) if present, restore after zip.
if exist dist\GameBuddy\data (
  if exist dist\_data_backup rmdir /s /q dist\_data_backup
  move dist\GameBuddy\data dist\_data_backup >nul
)

py -3 -m PyInstaller --noconfirm --clean build_exe.spec
if errorlevel 1 goto :fail

rem Ship a sanitized config (derived from config.example.yaml, no tenant key).
copy /y config.example.yaml dist\GameBuddy\config.yaml >nul

rem Build the ZIP with standard forward slashes + user README.
py -3 tools\make_release.py --src dist\GameBuddy --out dist\GameBuddy-core.zip
if errorlevel 1 goto :fail

rem Restore dev data after zipping (keeps device identity across rebuilds
rem without shipping it).
if exist dist\_data_backup (
  if not exist dist\GameBuddy\data move dist\_data_backup dist\GameBuddy\data >nul
  if exist dist\GameBuddy\data rmdir /s /q dist\_data_backup
)

echo.
echo Output: dist\GameBuddy\GameBuddy.exe + dist\GameBuddy-core.zip
echo Verify: mtimes, zip leak scan, PYZ contents, frozen smoke test.
echo See docs\PACKAGING.md for the four acceptance gates.
goto :eof

:fail
echo BUILD FAILED - check the log above.
if exist dist\_data_backup (
  if not exist dist\GameBuddy\data move dist\_data_backup dist\GameBuddy\data >nul
)
exit /b 1
