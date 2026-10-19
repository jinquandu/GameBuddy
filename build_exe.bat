@echo off
rem Core-only exe build (no ASR voice stack: torch/funasr excluded;
rem pipeline auto-skips asr/speaker/voice, OCR knockdown/pickup/push intact).
rem Output: dist\DashijiePet\DashijiePet.exe (onedir - ship the whole folder,
rem put config.yaml next to the exe).
cd /d "%~dp0"
py -3 -m PyInstaller --noconfirm --clean --windowed --name DashijiePet ^
  --collect-data rapidocr_onnxruntime ^
  --hidden-import pystray._win32 ^
  --hidden-import toolbox._scan_worker ^
  --hidden-import toolbox._loadout_batch_worker ^
  --hidden-import toolbox._pickup_scan_batch_worker ^
  --hidden-import toolbox._lobby_batch_worker ^
  --exclude-module torch --exclude-module torchaudio --exclude-module torchvision ^
  --exclude-module funasr --exclude-module modelscope ^
  --exclude-module librosa --exclude-module numba --exclude-module llvmlite ^
  --exclude-module scipy --exclude-module sklearn --exclude-module pandas ^
  --exclude-module datasets --exclude-module addict ^
  launch_pet.py
echo.
rem Distribution hygiene (field test 2026-10-15 P2-2): embed a sanitized
rem config.yaml derived from config.example.yaml -- no tenant key, so the
rem server tenant stays "default" until the end user sets it (pet UI).
rem Never ship the build machine's own config.yaml (tenant data mix-up risk,
rem plus it carried BOM/mojibake comments). dist config is a build artifact
rem and is overwritten on every rebuild.
copy /y config.example.yaml dist\DashijiePet\config.yaml >nul
rem zip the FOLDER (not its contents): keeps the top-level DashijiePet\
rem dir inside the archive so extraction does not scatter files.
rem pre-delete: Compress-Archive -Force occasionally dies with
rem "stream not readable" replacing an existing zip; fresh dest is reliable.
if exist dist\DashijiePet-core.zip del /f dist\DashijiePet-core.zip
powershell -NoProfile -Command "Compress-Archive -Path 'dist\DashijiePet' -DestinationPath 'dist\DashijiePet-core.zip'"
echo Output: dist\DashijiePet\DashijiePet.exe + dist\DashijiePet-core.zip
rem 2026-10-19 contract 1.3.0/1.4.0: config.example.yaml ships with
rem upload_url preset (HTTPS customer channel) - ops fills upload_token
rem at install (server upload_token.py issue); internal rsync machines
rem comment upload_url out. Account login shows service-not-ready until
rem the account service declares pc_device_auth_contract=1.0.
