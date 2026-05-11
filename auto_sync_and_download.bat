@echo off
setlocal enableextensions

chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python not found in .venv
  pause
  exit /b 1
)

if not exist ".env" (
  echo [ERROR] .env file not found. Copy .env.example to .env and fill in your credentials.
  pause
  exit /b 1
)

echo [INFO] Checking dependencies (flask, yt-dlp, selenium, curl_cffi)...
.venv\Scripts\python.exe -c "import flask, yt_dlp, selenium, curl_cffi" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing missing dependencies...
  .venv\Scripts\python.exe -m pip install -r requirements.txt
)

echo [INFO] Auto sync + download new videos
.venv\Scripts\python.exe auto_sync_and_download.py
set "EXITCODE=%ERRORLEVEL%"

echo.
echo Exit code: %EXITCODE%
pause
endlocal & exit /b %EXITCODE%
