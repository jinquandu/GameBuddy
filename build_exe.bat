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
echo Output: dist\DashijiePet\DashijiePet.exe
