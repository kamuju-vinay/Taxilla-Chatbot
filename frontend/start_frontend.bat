@echo off
REM Double-click this file to set up and start the frontend.
REM Safe to run every time — it only runs npm install the first
REM time (or when package.json changes), then just starts the dev
REM server after that.

cd /d "%~dp0"

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

echo.
echo Starting frontend dev server...
echo Keep this window open. Close it to stop the frontend.
echo.
call npm run dev

pause
