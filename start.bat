@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHON_EXE="
set "PYTHON_ARGS="

if exist "%PROJECT_DIR%.venv\Scripts\python.exe" goto use_project_venv
where py.exe >nul 2>nul
if not errorlevel 1 goto use_py_launcher
where python.exe >nul 2>nul
if not errorlevel 1 goto use_system_python

echo [ERROR] Python 3 was not found.
echo Install Python 3.11 or newer, or create .venv inside this project.
echo ComfyUI's Python environment is intentionally not used.
pause
exit /b 1

:use_project_venv
set "PYTHON_EXE=%PROJECT_DIR%.venv\Scripts\python.exe"
goto launch

:use_py_launcher
set "PYTHON_EXE=py.exe"
set "PYTHON_ARGS=-3"
goto launch

:use_system_python
set "PYTHON_EXE=python.exe"

:launch
echo Starting MiniMax H3 Idea2Video...
echo Reading Studio, LM Studio and ComfyUI ports from config.json...
echo The default browser will open when the local service is ready.
echo Press Ctrl+C to stop this service. Existing ComfyUI settings are not changed.
"%PYTHON_EXE%" %PYTHON_ARGS% "%PROJECT_DIR%app.py" --open-browser
set "STUDIO_EXIT_CODE=%ERRORLEVEL%"

if not "%STUDIO_EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] The service exited with code %STUDIO_EXIT_CODE%.
  pause
)

endlocal & exit /b %STUDIO_EXIT_CODE%
