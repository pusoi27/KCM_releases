@echo off
REM Stdytime Release Helper
REM - Bumps VERSION
REM - Commits and pushes to GitHub
REM - Builds NSIS installer
REM - Generates .sha256 for installer artifact

setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo ==============================================================
echo Stdytime Commit ^+ Push ^+ Build Installer
echo ==============================================================

where git >nul 2>nul
if errorlevel 1 (
  echo [ERROR] git is not installed or not in PATH.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv Python not found: .venv\Scripts\python.exe
  exit /b 1
)

if not exist "scripts\version_bump.py" (
  echo [ERROR] Missing script: scripts\version_bump.py
  exit /b 1
)

if not exist "build_nsis_installer.bat" (
  echo [ERROR] Missing build script: build_nsis_installer.bat
  exit /b 1
)

set "COMMIT_MSG=%*"
if "%COMMIT_MSG%"=="" (
  set "COMMIT_MSG=release"
)

echo.
echo [1/5] Bumping app version...
".venv\Scripts\python.exe" scripts\version_bump.py
if errorlevel 1 (
  echo [ERROR] Version bump failed.
  exit /b 1
)

echo.
echo [2/5] Staging changes...
git add -A
if errorlevel 1 (
  echo [ERROR] git add failed.
  exit /b 1
)

git diff --cached --quiet
if errorlevel 1 goto :has_changes

echo [ERROR] No staged changes to commit.
exit /b 1

:has_changes
echo.
echo [3/5] Creating commit...
git commit -m "%COMMIT_MSG%"
if errorlevel 1 (
  echo [ERROR] git commit failed.
  exit /b 1
)

echo.
echo [4/6] Cleaning PyInstaller cache for a deterministic installer build...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo.
echo [5/6] Building NSIS installer...
call build_nsis_installer.bat
if errorlevel 1 (
  echo [ERROR] Installer build failed.
  exit /b 1
)

call :validate_release_artifacts || exit /b 1

set "APP_VERSION="
if exist "VERSION" (
  set /p APP_VERSION=<"VERSION"
)
if not defined APP_VERSION if exist "Version" (
  set /p APP_VERSION=<"Version"
)

if "%APP_VERSION%"=="" (
  echo [WARNING] Could not read VERSION for output copy.
  goto :skip_output_copy
)

set "APP_VERSION_SAFE=%APP_VERSION:.=_%"
set "SECONDARY_INSTALLER_DIR=C:\Users\octav\OneDrive\ADOCTA TECH LLC\StdyTime"

set "LATEST_INSTALLER="
for /f "delims=" %%I in ('dir /b /a:-d /o-d "stdytime_installer_v*.exe"') do (
  if not defined LATEST_INSTALLER set "LATEST_INSTALLER=%%I"
)

if not defined LATEST_INSTALLER (
  echo [WARNING] Installer file not found for copy in: %CD%
  goto :skip_output_copy
)

set "INSTALLER_FILE=%LATEST_INSTALLER%"
set "SHA_FILE=%INSTALLER_FILE%.sha256"
set "ZIP_FILE=%INSTALLER_FILE:.exe=.zip%"
set "ZIP_SHA_FILE=%ZIP_FILE%.sha256"
set "LOCAL_INSTALLER_OUTPUT=%CD%\%INSTALLER_FILE%"
set "LOCAL_ZIP_OUTPUT=%CD%\%ZIP_FILE%"

if not exist "%SECONDARY_INSTALLER_DIR%" (
  mkdir "%SECONDARY_INSTALLER_DIR%" >nul 2>nul
)

if exist "%SECONDARY_INSTALLER_DIR%" (
  copy /Y "%INSTALLER_FILE%" "%SECONDARY_INSTALLER_DIR%\%INSTALLER_FILE%" >nul
  if exist "%SECONDARY_INSTALLER_DIR%\%INSTALLER_FILE%" (
    echo [INFO] Installer copied to: %SECONDARY_INSTALLER_DIR%\%INSTALLER_FILE%
  ) else (
    echo [WARNING] Failed copying installer to: %SECONDARY_INSTALLER_DIR%
  )
) else (
  echo [WARNING] Secondary installer directory is unavailable: %SECONDARY_INSTALLER_DIR%
)

:skip_output_copy

echo.
echo [6/6] Pushing to GitHub...
git push
if errorlevel 1 (
  echo [ERROR] git push failed.
  exit /b 1
)

if not defined APP_VERSION (
  echo [WARNING] Could not read VERSION for checksum generation.
  goto :done
)

if not exist "%INSTALLER_FILE%" (
  echo [WARNING] Installer file not found for checksum: %INSTALLER_FILE%
  goto :done
)

set "SHA_HASH="
for /f "tokens=1 delims= " %%H in ('certutil -hashfile "%INSTALLER_FILE%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f]*$"') do (
  if not defined SHA_HASH set "SHA_HASH=%%H"
)

if not defined SHA_HASH (
  echo [WARNING] Failed to generate SHA256 hash with certutil.
  goto :done
)

> "%SHA_FILE%" echo %SHA_HASH%  %INSTALLER_FILE%
if errorlevel 1 (
  echo [WARNING] Failed writing SHA256 file: %SHA_FILE%
  goto :done
)

echo [INFO] SHA256 written: %SHA_FILE%

if exist "%ZIP_FILE%" del /f /q "%ZIP_FILE%" >nul 2>nul

powershell -NoProfile -Command "Compress-Archive -Path '%INSTALLER_FILE%' -DestinationPath '%ZIP_FILE%' -CompressionLevel Optimal -Force"
if errorlevel 1 (
  echo [WARNING] Failed to create ZIP archive: %ZIP_FILE%
  goto :done
)

if not exist "%ZIP_FILE%" (
  echo [WARNING] ZIP archive was not created: %ZIP_FILE%
  goto :done
)

if exist "%SECONDARY_INSTALLER_DIR%" (
  copy /Y "%ZIP_FILE%" "%SECONDARY_INSTALLER_DIR%\%ZIP_FILE%" >nul
  if exist "%SECONDARY_INSTALLER_DIR%\%ZIP_FILE%" (
    echo [INFO] ZIP copied to: %SECONDARY_INSTALLER_DIR%\%ZIP_FILE%
  ) else (
    echo [WARNING] Failed copying ZIP to: %SECONDARY_INSTALLER_DIR%
  )
)

set "ZIP_SHA_HASH="
for /f "tokens=1 delims= " %%H in ('certutil -hashfile "%ZIP_FILE%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f]*$"') do (
  if not defined ZIP_SHA_HASH set "ZIP_SHA_HASH=%%H"
)

if not defined ZIP_SHA_HASH (
  echo [WARNING] Failed to generate SHA256 hash for ZIP: %ZIP_FILE%
  goto :done
)

> "%ZIP_SHA_FILE%" echo %ZIP_SHA_HASH%  %ZIP_FILE%
if errorlevel 1 (
  echo [WARNING] Failed writing ZIP SHA256 file: %ZIP_SHA_FILE%
  goto :done
)

echo [INFO] ZIP SHA256 written: %ZIP_SHA_FILE%

:done
echo.
echo ==============================================================
echo Completed successfully.
echo Local Output: %LOCAL_INSTALLER_OUTPUT%
if defined LOCAL_ZIP_OUTPUT echo Local ZIP Output: %LOCAL_ZIP_OUTPUT%
echo OneDrive Output: %SECONDARY_INSTALLER_DIR%\%INSTALLER_FILE%
if defined ZIP_FILE echo OneDrive ZIP Output: %SECONDARY_INSTALLER_DIR%\%ZIP_FILE%
echo Installer: %INSTALLER_FILE%
if defined ZIP_FILE echo ZIP: %ZIP_FILE%
echo ==============================================================

endlocal
exit /b 0

:validate_release_artifacts
set "APP_VERSION_VERIFY="
if exist "VERSION" (
  set /p APP_VERSION_VERIFY=<"VERSION"
) else if exist "Version" (
  set /p APP_VERSION_VERIFY=<"Version"
)

if not defined APP_VERSION_VERIFY (
  echo build failed: missing VERSION file after build
  exit /b 1
)

if exist "dist_release" (
  rem continue
) else (
  echo build failed: dist_release folder was not created
  exit /b 1
)

if not exist "dist_release\Stdytime.exe" (
  echo build failed: missing dist_release\Stdytime.exe
  exit /b 1
)
if not exist "dist_release\app.py" (
  echo build failed: missing dist_release\app.py required by launcher fallback
  exit /b 1
)
if not exist "dist_release\launcher.py" (
  echo build failed: missing dist_release\launcher.py
  exit /b 1
)
if not exist "dist_release\launcher_browser.py" (
  echo build failed: missing dist_release\launcher_browser.py
  exit /b 1
)
if not exist "dist_release\modules\database.py" (
  echo build failed: missing dist_release\modules\database.py
  exit /b 1
)
if not exist "dist_release\routes\api.py" (
  echo build failed: missing dist_release\routes\api.py
  exit /b 1
)
if not exist "dist_release\VERSION" (
  echo build failed: missing dist_release\VERSION
  exit /b 1
)

set "RELEASE_VERSION="
set /p RELEASE_VERSION=<"dist_release\VERSION"
if /I not "%RELEASE_VERSION%"=="%APP_VERSION_VERIFY%" (
  echo build failed: dist_release VERSION mismatch (%RELEASE_VERSION% vs %APP_VERSION_VERIFY%)
  exit /b 1
)

set "APP_VERSION_SAFE_VERIFY=%APP_VERSION_VERIFY:.=_%"
if not exist "stdytime_installer_v%APP_VERSION_SAFE_VERIFY%.exe" (
  echo build failed: missing installer stdytime_installer_v%APP_VERSION_SAFE_VERIFY%.exe
  exit /b 1
)

exit /b 0
