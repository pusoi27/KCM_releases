@echo off
REM Stdytime Release Helper
REM - Bumps VERSION
REM - Commits and pushes to GitHub
REM - Builds NSIS installer
REM - Generates SHA256 and ZIP
REM - Publishes ZIP artifacts to stdytime_releases, purging older ZIP releases first

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
echo [1/8] Bumping app version...
".venv\Scripts\python.exe" scripts\version_bump.py
if errorlevel 1 (
  echo [ERROR] Version bump failed.
  exit /b 1
)

echo.
echo [2/8] Staging changes...
if exist ".tmp_release_check\.git" (
  echo [INFO] Removing temporary clone folder: .tmp_release_check
  rmdir /s /q ".tmp_release_check"
)
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
echo [3/8] Creating commit...
git commit -m "%COMMIT_MSG%"
if errorlevel 1 (
  echo [ERROR] git commit failed.
  exit /b 1
)

echo.
echo [4/8] Cleaning PyInstaller cache for a deterministic installer build...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

echo.
echo [5/8] Building NSIS installer...
call "%ROOT%build_nsis_installer.bat"
set "NSIS_BUILD_EXIT=%ERRORLEVEL%"
echo [TRACE] build_nsis_installer.bat exit code: %NSIS_BUILD_EXIT%
if not "%NSIS_BUILD_EXIT%"=="0" (
  echo [ERROR] Installer build failed.
  exit /b 1
)

echo [TRACE] Validating release artifacts...
call :validate_release_artifacts
if errorlevel 1 (
  echo [WARNING] validate_release_artifacts failed; continuing to checksum and publish steps.
)

set "APP_VERSION="
if exist "VERSION" (
  for /f "usebackq delims=" %%V in ("VERSION") do set "APP_VERSION=%%V"
)
if not defined APP_VERSION if exist "Version" (
  for /f "usebackq delims=" %%V in ("Version") do set "APP_VERSION=%%V"
)

if "%APP_VERSION%"=="" (
  echo [ERROR] Could not read VERSION for artifact generation.
  exit /b 1
)

set "APP_VERSION_SAFE=%APP_VERSION:.=_%"
set "SECONDARY_INSTALLER_DIR=C:\Users\octav\OneDrive\ADOCTA TECH LLC\StdyTime"
set "RELEASE_FOLDER_NAME=v%APP_VERSION%"
set "LOCAL_RELEASE_BASE=%CD%\releases"
set "LOCAL_RELEASE_DIR=%LOCAL_RELEASE_BASE%\%RELEASE_FOLDER_NAME%"
set "SECONDARY_RELEASE_DIR=%SECONDARY_INSTALLER_DIR%\%RELEASE_FOLDER_NAME%"

set "INSTALLER_FILE=stdytime_installer_v%APP_VERSION_SAFE%.exe"
set "SHA_FILE=%INSTALLER_FILE%.sha256"
set "ZIP_FILE=%INSTALLER_FILE:.exe=.zip%"
set "ZIP_SHA_FILE=%ZIP_FILE%.sha256"
set "LOCAL_INSTALLER_OUTPUT=%LOCAL_RELEASE_DIR%\%INSTALLER_FILE%"
set "LOCAL_SHA_OUTPUT=%LOCAL_RELEASE_DIR%\%SHA_FILE%"
set "LOCAL_ZIP_OUTPUT=%LOCAL_RELEASE_DIR%\%ZIP_FILE%"
set "LOCAL_ZIP_SHA_OUTPUT=%LOCAL_RELEASE_DIR%\%ZIP_SHA_FILE%"
set "SECONDARY_INSTALLER_OUTPUT=%SECONDARY_RELEASE_DIR%\%INSTALLER_FILE%"
set "SECONDARY_SHA_OUTPUT=%SECONDARY_RELEASE_DIR%\%SHA_FILE%"
set "SECONDARY_ZIP_OUTPUT=%SECONDARY_RELEASE_DIR%\%ZIP_FILE%"
set "SECONDARY_ZIP_SHA_OUTPUT=%SECONDARY_RELEASE_DIR%\%ZIP_SHA_FILE%"

if not exist "%INSTALLER_FILE%" (
  echo [ERROR] Expected installer not found: %INSTALLER_FILE%
  exit /b 1
)

if not exist "%SECONDARY_INSTALLER_DIR%" (
  mkdir "%SECONDARY_INSTALLER_DIR%" >nul 2>nul
)

if not exist "%LOCAL_RELEASE_BASE%" (
  mkdir "%LOCAL_RELEASE_BASE%" >nul 2>nul
)
if not exist "%LOCAL_RELEASE_DIR%" (
  mkdir "%LOCAL_RELEASE_DIR%" >nul 2>nul
)

if exist "%SECONDARY_INSTALLER_DIR%" (
  if not exist "%SECONDARY_RELEASE_DIR%" (
    mkdir "%SECONDARY_RELEASE_DIR%" >nul 2>nul
  )
)

copy /Y "%INSTALLER_FILE%" "%LOCAL_INSTALLER_OUTPUT%" >nul
if exist "%LOCAL_INSTALLER_OUTPUT%" (
  echo [INFO] Installer copied to: %LOCAL_INSTALLER_OUTPUT%
) else (
  echo [WARNING] Failed copying installer to local release folder: %LOCAL_RELEASE_DIR%
)

if exist "%SECONDARY_RELEASE_DIR%" (
  copy /Y "%INSTALLER_FILE%" "%SECONDARY_INSTALLER_OUTPUT%" >nul
  if exist "%SECONDARY_INSTALLER_OUTPUT%" (
    echo [INFO] Installer copied to: %SECONDARY_INSTALLER_OUTPUT%
  ) else (
    echo [WARNING] Failed copying installer to: %SECONDARY_RELEASE_DIR%
  )
) else (
  echo [WARNING] Secondary installer directory is unavailable: %SECONDARY_INSTALLER_DIR%
)

echo.
echo [6/8] Generating checksums and ZIP...

set "SHA_HASH="
for /f "tokens=1 delims= " %%H in ('certutil -hashfile "%INSTALLER_FILE%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f]*$"') do (
  if not defined SHA_HASH set "SHA_HASH=%%H"
)
if not defined SHA_HASH (
  echo [ERROR] Failed to generate SHA256 hash for installer.
  exit /b 1
)

> "%SHA_FILE%" echo %SHA_HASH%  %INSTALLER_FILE%
if errorlevel 1 (
  echo [ERROR] Failed writing SHA256 file: %SHA_FILE%
  exit /b 1
)
echo [INFO] SHA256 written: %SHA_FILE%

copy /Y "%SHA_FILE%" "%LOCAL_SHA_OUTPUT%" >nul
if exist "%LOCAL_SHA_OUTPUT%" (
  echo [INFO] Installer SHA256 copied to: %LOCAL_SHA_OUTPUT%
) else (
  echo [WARNING] Failed copying installer SHA256 to local release folder: %LOCAL_RELEASE_DIR%
)

if exist "%SECONDARY_RELEASE_DIR%" (
  copy /Y "%SHA_FILE%" "%SECONDARY_SHA_OUTPUT%" >nul
  if exist "%SECONDARY_SHA_OUTPUT%" (
    echo [INFO] Installer SHA256 copied to: %SECONDARY_SHA_OUTPUT%
  ) else (
    echo [WARNING] Failed copying installer SHA256 to: %SECONDARY_RELEASE_DIR%
  )
)

if exist "%ZIP_FILE%" del /f /q "%ZIP_FILE%" >nul 2>nul
powershell -NoProfile -Command "Compress-Archive -Path '%INSTALLER_FILE%' -DestinationPath '%ZIP_FILE%' -CompressionLevel Optimal -Force"
if errorlevel 1 (
  echo [ERROR] Failed to create ZIP archive: %ZIP_FILE%
  exit /b 1
)
if not exist "%ZIP_FILE%" (
  echo [ERROR] ZIP archive was not created: %ZIP_FILE%
  exit /b 1
)

copy /Y "%ZIP_FILE%" "%LOCAL_ZIP_OUTPUT%" >nul
if exist "%LOCAL_ZIP_OUTPUT%" (
  echo [INFO] ZIP copied to: %LOCAL_ZIP_OUTPUT%
) else (
  echo [WARNING] Failed copying ZIP to local release folder: %LOCAL_RELEASE_DIR%
)

if exist "%SECONDARY_RELEASE_DIR%" (
  copy /Y "%ZIP_FILE%" "%SECONDARY_ZIP_OUTPUT%" >nul
  if exist "%SECONDARY_ZIP_OUTPUT%" (
    echo [INFO] ZIP copied to: %SECONDARY_ZIP_OUTPUT%
  ) else (
    echo [WARNING] Failed copying ZIP to: %SECONDARY_RELEASE_DIR%
  )
)

set "ZIP_SHA_HASH="
for /f "tokens=1 delims= " %%H in ('certutil -hashfile "%ZIP_FILE%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f]*$"') do (
  if not defined ZIP_SHA_HASH set "ZIP_SHA_HASH=%%H"
)
if not defined ZIP_SHA_HASH (
  echo [ERROR] Failed to generate SHA256 hash for ZIP.
  exit /b 1
)

> "%ZIP_SHA_FILE%" echo %ZIP_SHA_HASH%  %ZIP_FILE%
if errorlevel 1 (
  echo [ERROR] Failed writing ZIP SHA256 file: %ZIP_SHA_FILE%
  exit /b 1
)
echo [INFO] ZIP SHA256 written: %ZIP_SHA_FILE%

copy /Y "%ZIP_SHA_FILE%" "%LOCAL_ZIP_SHA_OUTPUT%" >nul
if exist "%LOCAL_ZIP_SHA_OUTPUT%" (
  echo [INFO] ZIP SHA256 copied to: %LOCAL_ZIP_SHA_OUTPUT%
) else (
  echo [WARNING] Failed copying ZIP SHA256 to local release folder: %LOCAL_RELEASE_DIR%
)

if exist "%SECONDARY_RELEASE_DIR%" (
  copy /Y "%ZIP_SHA_FILE%" "%SECONDARY_ZIP_SHA_OUTPUT%" >nul
  if exist "%SECONDARY_ZIP_SHA_OUTPUT%" (
    echo [INFO] ZIP SHA256 copied to: %SECONDARY_ZIP_SHA_OUTPUT%
  ) else (
    echo [WARNING] Failed copying ZIP SHA256 to: %SECONDARY_RELEASE_DIR%
  )
)

echo.
echo [TRACE] ZIP artifact ready: %ZIP_FILE%
echo [TRACE] ZIP checksum file: %ZIP_SHA_FILE%

echo.
echo [7/8] Publishing ZIP as GitHub Release assets...
call :publish_zip_release_repo
if errorlevel 1 exit /b 1

echo.
echo [8/8] Pushing to GitHub...
git push
if errorlevel 1 (
  echo [ERROR] git push failed.
  exit /b 1
)

echo.
echo ==============================================================
echo Completed successfully.
echo Local release folder: %LOCAL_RELEASE_DIR%
echo Local installer output: %LOCAL_INSTALLER_OUTPUT%
echo Local installer SHA256 output: %LOCAL_SHA_OUTPUT%
echo Local ZIP output: %LOCAL_ZIP_OUTPUT%
echo Local ZIP SHA256 output: %LOCAL_ZIP_SHA_OUTPUT%
echo OneDrive release folder: %SECONDARY_RELEASE_DIR%
echo OneDrive installer output: %SECONDARY_INSTALLER_OUTPUT%
echo OneDrive installer SHA256 output: %SECONDARY_SHA_OUTPUT%
echo OneDrive ZIP Output: %SECONDARY_ZIP_OUTPUT%
echo OneDrive ZIP SHA256 Output: %SECONDARY_ZIP_SHA_OUTPUT%
echo Installer: %INSTALLER_FILE%
echo ZIP: %ZIP_FILE%
echo ZIP SHA256: %ZIP_SHA_FILE%
echo ==============================================================

endlocal
exit /b 0

:publish_zip_release_repo
if not exist "%LOCAL_ZIP_OUTPUT%" (
  echo [ERROR] ZIP file not found; refusing to publish: %LOCAL_ZIP_OUTPUT%
  exit /b 1
)
if not exist "%LOCAL_ZIP_SHA_OUTPUT%" (
  echo [ERROR] ZIP checksum file not found; refusing to publish: %LOCAL_ZIP_SHA_OUTPUT%
  exit /b 1
)
if "%APP_VERSION%"=="" (
  echo [ERROR] APP_VERSION is not set before release asset publishing.
  exit /b 1
)

if not exist "%ROOT%scripts\publish_github_release_assets.py" (
  echo [ERROR] Missing script: scripts\publish_github_release_assets.py
  exit /b 1
)

set "RELEASES_REPO_SLUG=pusoi27/stdytime_releases"
set "RELEASE_TAG=v%APP_VERSION%"
set "RELEASE_TITLE=Stdytime %APP_VERSION%"
set "PUBLISH_SCRIPT=%ROOT%scripts\publish_github_release_assets.py"
set "PY_EXE=%ROOT%.venv\Scripts\python.exe"
set "ZIP_ASSET_1=%LOCAL_ZIP_OUTPUT%"
set "ZIP_ASSET_2=%LOCAL_ZIP_SHA_OUTPUT%"

echo [INFO] Publishing GitHub Release assets to %RELEASES_REPO_SLUG% @ %RELEASE_TAG%...
echo [INFO] Upload in progress... this can take a few minutes for large ZIP files.
echo [INFO] Progress bar below reflects upload of each asset.
"%PY_EXE%" "%PUBLISH_SCRIPT%" --repo "%RELEASES_REPO_SLUG%" --tag "%RELEASE_TAG%" --title "%RELEASE_TITLE%" --asset "%ZIP_ASSET_1%" --asset "%ZIP_ASSET_2%"
if errorlevel 1 (
  echo [ERROR] Failed to publish GitHub Release assets.
  exit /b 1
)

echo [INFO] ZIP artifacts published as GitHub Release assets.
exit /b 0

:validate_release_artifacts
set "APP_VERSION_VERIFY="
if exist "VERSION" (
  for /f "usebackq delims=" %%V in ("VERSION") do set "APP_VERSION_VERIFY=%%V"
)
if not defined APP_VERSION_VERIFY if exist "Version" (
  for /f "usebackq delims=" %%V in ("Version") do set "APP_VERSION_VERIFY=%%V"
)

if not defined APP_VERSION_VERIFY exit /b 1
if not exist "dist_release" exit /b 1
if not exist "dist_release\Stdytime.exe" exit /b 1
if not exist "dist_release\app.py" exit /b 1
if not exist "dist_release\launcher.py" exit /b 1
if not exist "dist_release\launcher_browser.py" exit /b 1
if not exist "dist_release\modules\database.py" exit /b 1
if not exist "dist_release\routes\api.py" exit /b 1
if not exist "dist_release\VERSION" exit /b 1

set "RELEASE_VERSION="
for /f "usebackq delims=" %%V in ("dist_release\VERSION") do set "RELEASE_VERSION=%%V"
if /I not "%RELEASE_VERSION%"=="%APP_VERSION_VERIFY%" exit /b 1

set "APP_VERSION_SAFE_VERIFY=%APP_VERSION_VERIFY:.=_%"
if not exist "stdytime_installer_v%APP_VERSION_SAFE_VERIFY%.exe" exit /b 1

exit /b 0
