@echo off
setlocal enableextensions

chcp 65001 >nul
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Не найден Python venv: "%PY%"
  echo Создайте окружение: py -m venv .venv
  pause
  exit /b 1
)

echo Проверяю зависимости (flask, yt-dlp, selenium, curl_cffi)...
"%PY%" -c "import flask, yt_dlp, selenium, curl_cffi" >nul 2>&1
if errorlevel 1 (
  echo Устанавливаю зависимости...
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install flask yt-dlp selenium curl-cffi
)

echo.
echo Запускаю локальное меню: http://127.0.0.1:5050
echo Скачанные файлы: %CD%\videos\^<ПРЕДМЕТ^>\
echo.

"%PY%" lessons_menu.py --host 127.0.0.1 --port 5050
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo Меню завершилось с ошибкой, код: %EXITCODE%
)

pause
endlocal & exit /b %EXITCODE%
