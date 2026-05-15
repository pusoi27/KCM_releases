@echo off
REM No-Inno Windows packaging pipeline for Stdytime
REM - Builds dist_release payload
REM - Produces a versioned local-only zip package
REM - Users launch Stdytime.exe directly after extracting

setlocal EnableDelayedExpansion

set ROOT=%~dp0
cd /d "%ROOT%"

if not exist .venv\Scripts\python.exe (
  echo [ERROR] .venv Python not found.
  exit /b 1
)

REM Build release payload
call build_release_windows.bat
if errorlevel 1 exit /b 1

REM Read version
set VERSION=
for /f "usebackq delims=" %%v in ("VERSION") do set VERSION=%%v
if "%VERSION%"=="" (
  echo [ERROR] Could not read VERSION file.
  exit /b 1
)

set SAFE_VERSION=%VERSION:.=_%
set STAGE_DIR=package_stage\stdytime_v%SAFE_VERSION%
set ZIP_NAME=stdytime_package_v%SAFE_VERSION%.zip

if exist package_stage rmdir /s /q package_stage
if exist "%ZIP_NAME%" del /q "%ZIP_NAME%"

mkdir "%STAGE_DIR%"

xcopy dist_release "%STAGE_DIR%" /E /I /Y >nul
copy /Y INSTALL_README_WINDOWS.txt "%STAGE_DIR%\" >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'package_stage\stdytime_v%SAFE_VERSION%\*' -DestinationPath '%ZIP_NAME%' -Force"
if errorlevel 1 exit /b 1

echo.
echo Package created: %CD%\%ZIP_NAME%
echo.
echo To deploy on a target machine:
echo 1) Extract the zip
echo 2) Double-click Stdytime.exe
echo 3) If you prefer, pin Stdytime.exe or create your own shortcut

endlocal
