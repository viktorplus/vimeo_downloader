@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Не найден Python в .venv
  echo Создайте окружение и установите зависимости.
  pause
  exit /b 1
)

echo [INFO] Проверка selenium...
.venv\Scripts\python.exe -c "import selenium" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Устанавливаю selenium...
  .venv\Scripts\python.exe -m pip install selenium
  if errorlevel 1 (
    echo [ERROR] Не удалось установить selenium
    pause
    exit /b 1
  )
)

echo [INFO] Обновляю lessons_list.txt из LMS...
.venv\Scripts\python.exe update_lessons_list.py
echo.
pause
