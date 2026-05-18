@echo off
REM Stdytime Installer/Updater Script
REM - Installs codebase to versioned folder
REM - Keeps database at fixed location
REM - Archives old codebase folders

setlocal EnableDelayedExpansion

REM --- CONFIG ---
set BASE_DIR=%LOCALAPPDATA%\Stdytime
set ARCHIVE_DIR=%LOCALAPPDATA%\Stdytime_archive
set VERSION_FILE=VERSION

REM --- Read version ---
set VERSION=
for /f "usebackq delims=" %%v in ("%VERSION_FILE%") do set VERSION=%%v
if "%VERSION%"=="" (
  echo [ERROR] Could not read VERSION file.
  exit /b 1
)
set SAFE_VERSION=%VERSION:.=_%
set CODE_DIR=%LOCALAPPDATA%\Stdytime_%SAFE_VERSION%

REM --- Create archive dir if needed ---
if not exist "%ARCHIVE_DIR%" mkdir "%ARCHIVE_DIR%"

REM --- Archive old codebase folders ---
for /d %%F in ("%LOCALAPPDATA%\Stdytime_*") do (
  if /I not "%%F"=="%CODE_DIR%" move "%%F" "%ARCHIVE_DIR%" >nul
)

REM --- Create code dir if needed ---
if not exist "%CODE_DIR%" mkdir "%CODE_DIR%"

REM --- Copy codebase files (customize as needed) ---
REM Example: xcopy dist_release\* "%CODE_DIR%" /E /I /Y

REM --- Ensure database folder exists ---
if not exist "%BASE_DIR%" mkdir "%BASE_DIR%"

REM --- Done ---
echo Stdytime v%VERSION% installed to: %CODE_DIR%
echo Database location: %BASE_DIR%\Stdytime.db
echo Old versions archived in: %ARCHIVE_DIR%

endlocal
