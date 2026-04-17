@echo off
setlocal enableextensions

chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Python not found in .venv
  echo Create the venv and install requirements first.
  pause
  exit /b 1
)

set "HTML_FILE=%~1"
if "%HTML_FILE%"=="" set "HTML_FILE=saved_records.html"

if not exist "%HTML_FILE%" (
  echo [ERROR] HTML file not found: %HTML_FILE%
  echo.
  echo How to get it:
  echo   1. Open https://lms.itcareerhub.de/local/airtable_schedule/records.php in any browser
  echo   2. Wait for the lessons table to fully load
  echo   3. Press Ctrl+S, choose "Webpage, HTML Only"
  echo   4. Save as saved_records.html in the project root
  echo.
  echo Usage: update_lessons_from_html.bat [path_to_html]
  pause
  exit /b 1
)

echo [INFO] Parsing local HTML: %HTML_FILE%
.venv\Scripts\python.exe update_lessons_list.py --html-file "%HTML_FILE%"
echo.
pause
