@echo off
REM Build launcher executable

echo Building browser launcher executable...

cd /d "%~dp0"

call .venv\Scripts\activate.bat

pyinstaller ^
    --onefile ^
    --console ^
    --name "Run Stdytime" ^
    --icon "assets\stdytime.ico" ^
    launcher_browser.py

if errorlevel 1 (
    echo Failed to build launcher
    pause
    exit /b 1
)

echo.
echo Launcher built successfully: dist\Run Stdytime.exe
echo.
pause
