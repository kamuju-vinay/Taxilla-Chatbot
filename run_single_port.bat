@echo off
REM ============================================================
REM  Double-click this to run the WHOLE app on ONE address:
REM    http://localhost:5000
REM  No separate setup needed - this checks for Python/Node,
REM  installs dependencies on first run, builds the frontend,
REM  and starts the backend which serves everything.
REM ============================================================

cd /d "%~dp0"

echo ============================================
echo  Checking prerequisites...
echo ============================================

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: Python was not found on PATH.
    echo Install it from https://www.python.org/downloads/
    echo IMPORTANT: on the first installer screen, tick "Add python.exe to PATH"
    echo before clicking Install - this is unticked by default and is the
    echo most common reason Python isn't found even after installing.
    echo After installing, close this window and double-click this file again.
    echo.
    pause
    exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: Node.js was not found on PATH.
    echo Install it from https://nodejs.org/ ^(LTS version^), then close this
    echo window and double-click this file again.
    echo.
    pause
    exit /b 1
)

echo Found Python and Node.js - continuing.
echo.

echo ============================================
echo  Step 1/2: Building the frontend...
echo ============================================
cd frontend

if not exist "node_modules" (
    echo Installing frontend dependencies - this only happens once...
    call npm install
    if errorlevel 1 (
        echo.
        echo npm install failed - see the error above.
        pause
        exit /b 1
    )
)

call npm run build
if errorlevel 1 (
    echo.
    echo Frontend build failed - see the error above.
    pause
    exit /b 1
)

cd ..

echo.
echo ============================================
echo  Step 2/2: Setting up the backend...
echo ============================================
cd backend

if not exist "venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

REM Some Python installs (e.g. the Windows Store / install-manager flow)
REM don't ship pip inside a fresh venv - ensurepip fixes that reliably.
python -m ensurepip --upgrade >nul 2>nul

if not exist "venv\.deps_installed" (
    echo Installing backend dependencies - this only happens once...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Dependency install failed - see the error above.
        pause
        exit /b 1
    )
    echo done > "venv\.deps_installed"
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo Created backend\.env from the example file.
        echo Remember to set an AI provider key ^(Cohere/Gemini/Groq^) in Settings
        echo once the app opens, or by editing backend\.env directly.
    )
)

echo.
echo ============================================
echo  Starting the app...
echo ============================================
echo Everything will be on ONE address: http://localhost:5000
echo Your browser will open automatically in a few seconds.
echo Keep this window open - closing it stops the app.
echo.

start "" cmd /c "timeout /t 4 >nul && start http://localhost:5000"

python app.py

pause