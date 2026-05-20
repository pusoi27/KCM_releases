@echo off
REM Stdytime Installer/Updater Script
REM - Option A: Download installer + SHA256, verify integrity, then run installer
REM - Option B: Local payload install (legacy behavior)

setlocal EnableDelayedExpansion

REM --- CONFIG ---
set BASE_DIR=%LOCALAPPDATA%\Stdytime
set ARCHIVE_DIR=%LOCALAPPDATA%\Stdytime_archive
set VERSION_FILE=Version
if not exist "%VERSION_FILE%" set VERSION_FILE=VERSION

REM --- Optional download mode ---
REM Usage:
REM   install_or_update_stdytime.bat <installer_url> [sha256_url]
REM Example:
REM   install_or_update_stdytime.bat https://example.com/stdytime_installer_v00_08_101.exe
REM   install_or_update_stdytime.bat https://example.com/stdytime_installer_v00_08_101.exe https://example.com/stdytime_installer_v00_08_101.exe.sha256

if /I not "%~1"=="" (
  echo %~1 | findstr /I /R "^https\?://" >nul
  if not errorlevel 1 (
    set "INSTALLER_URL=%~1"
    set "SHA256_URL=%~2"
    if "%SHA256_URL%"=="" set "SHA256_URL=%INSTALLER_URL%.sha256"

    set "DL_DIR=%TEMP%\stdytime_download"
    if not exist "%DL_DIR%" mkdir "%DL_DIR%"
    if errorlevel 1 (
      echo [ERROR] Failed to create temp download folder: %DL_DIR%
      exit /b 1
    )

    set "INSTALLER_NAME=%~nx1"
    set "INSTALLER_PATH=%DL_DIR%\%INSTALLER_NAME%"
    set "SHA_PATH=%INSTALLER_PATH%.sha256"

    call :download_file "%INSTALLER_URL%" "%INSTALLER_PATH%"
    if errorlevel 1 goto :download_fail

    call :download_file "%SHA256_URL%" "%SHA_PATH%"
    if errorlevel 1 goto :download_fail

    call :verify_sha256 "%INSTALLER_PATH%" "%SHA_PATH%"
    if errorlevel 1 goto :sumcheck_fail

    echo [OK] Integrity verified for %INSTALLER_NAME%.
    echo [INFO] Launching installer...
    start /wait "" "%INSTALLER_PATH%"
    set "RUN_EXIT=%ERRORLEVEL%"
    if not "%RUN_EXIT%"=="0" (
      echo [ERROR] Installer exited with code %RUN_EXIT%.
      exit /b %RUN_EXIT%
    )
    echo [OK] Installer completed successfully.
    exit /b 0
  )
)

REM --- Read version ---
set VERSION=
for /f "usebackq delims=" %%v in ("%VERSION_FILE%") do set VERSION=%%v
if "%VERSION%"=="" (
  echo [ERROR] Could not read Version or VERSION file.
  exit /b 1
)
set CODE_DIR=%LOCALAPPDATA%\Stdytime

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
exit /b 0

:download_file
set "_URL=%~1"
set "_OUT=%~2"
echo [INFO] Downloading: %_URL%
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri '%_URL%' -OutFile '%_OUT%' -UseBasicParsing -ErrorAction Stop; exit 0 } catch { Write-Host $_.Exception.Message; exit 1 }" >nul
if errorlevel 1 (
  echo [ERROR] Download failed: %_URL%
  exit /b 1
)
if not exist "%_OUT%" (
  echo [ERROR] Download finished but file is missing: %_OUT%
  exit /b 1
)
for %%A in ("%_OUT%") do if %%~zA LSS 1 (
  echo [ERROR] Downloaded file is empty: %_OUT%
  exit /b 1
)
exit /b 0

:verify_sha256
set "_FILE=%~1"
set "_SHAFILE=%~2"

if not exist "%_SHAFILE%" (
  echo [ERROR] Missing checksum file: %_SHAFILE%
  exit /b 1
)

set "EXPECTED_HASH="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$line=(Get-Content -LiteralPath '%_SHAFILE%' -TotalCount 1).Trim(); if($line){$line.Split()[0].ToLowerInvariant()}"`) do set "EXPECTED_HASH=%%H"
if "%EXPECTED_HASH%"=="" (
  echo [ERROR] Could not read expected SHA256 from: %_SHAFILE%
  exit /b 1
)

set "ACTUAL_HASH="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-FileHash -LiteralPath '%_FILE%' -Algorithm SHA256).Hash.ToLowerInvariant()"`) do set "ACTUAL_HASH=%%H"
if "%ACTUAL_HASH%"=="" (
  echo [ERROR] Failed to compute SHA256 for: %_FILE%
  exit /b 1
)

if /I not "%ACTUAL_HASH%"=="%EXPECTED_HASH%" (
  echo [ERROR] SHA256 mismatch for: %_FILE%
  echo [ERROR] Expected: %EXPECTED_HASH%
  echo [ERROR] Actual:   %ACTUAL_HASH%
  exit /b 1
)

exit /b 0

:download_fail
echo.
echo [FATAL] Download failed.
echo Please check your internet connection and download the installer again.
exit /b 1

:sumcheck_fail
echo.
echo [FATAL] Integrity check failed (SHA256 mismatch or invalid checksum file).
echo Please delete the downloaded files and download the installer again.
exit /b 1
