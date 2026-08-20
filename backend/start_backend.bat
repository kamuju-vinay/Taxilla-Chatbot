@echo off
REM Double-click this file to set up and start the backend.
REM Safe to run every time — it only creates the venv / installs
REM packages the first time, then just starts the server after that.

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

if not exist "venv\.deps_installed" (
    echo Installing dependencies - this only happens once...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency install failed - see the error above.
        pause
        exit /b 1
    )
    echo done > "venv\.deps_installed"
)

echo.
echo Starting backend on http://localhost:5000 ...
echo Keep this window open. Close it to stop the backend.
echo.
python app.py

pause
