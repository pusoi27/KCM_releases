@echo off
REM Stdytime Release Helper
REM - Bumps VERSION
REM - Commits and pushes to GitHub
REM - Builds NSIS installer
REM - Generates SHA256, ZIP, and minisign signature
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

set "INSTALLER_FILE=stdytime_installer_v%APP_VERSION_SAFE%.exe"
set "SHA_FILE=%INSTALLER_FILE%.sha256"
set "ZIP_FILE=%INSTALLER_FILE:.exe=.zip%"
set "ZIP_SHA_FILE=%ZIP_FILE%.sha256"
set "ZIP_MINISIG_FILE=%ZIP_FILE%.minisig"
set "LOCAL_INSTALLER_OUTPUT=%CD%\%INSTALLER_FILE%"
set "LOCAL_ZIP_OUTPUT=%CD%\%ZIP_FILE%"
set "SECONDARY_ZIP_OUTPUT=%SECONDARY_INSTALLER_DIR%\%ZIP_FILE%"
set "SECONDARY_ZIP_SHA_OUTPUT=%SECONDARY_INSTALLER_DIR%\%ZIP_SHA_FILE%"
set "SECONDARY_ZIP_MINISIG_OUTPUT=%SECONDARY_INSTALLER_DIR%\%ZIP_MINISIG_FILE%"

if not exist "%INSTALLER_FILE%" (
  echo [ERROR] Expected installer not found: %INSTALLER_FILE%
  exit /b 1
)

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

echo.
echo [6/8] Generating checksums, ZIP, minisig...

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

if exist "%SECONDARY_INSTALLER_DIR%" (
  copy /Y "%ZIP_FILE%" "%SECONDARY_ZIP_OUTPUT%" >nul
  if exist "%SECONDARY_ZIP_OUTPUT%" (
    echo [INFO] ZIP copied to: %SECONDARY_ZIP_OUTPUT%
  ) else (
    echo [WARNING] Failed copying ZIP to: %SECONDARY_INSTALLER_DIR%
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

if exist "%SECONDARY_INSTALLER_DIR%" (
  copy /Y "%ZIP_SHA_FILE%" "%SECONDARY_ZIP_SHA_OUTPUT%" >nul
  if exist "%SECONDARY_ZIP_SHA_OUTPUT%" (
    echo [INFO] ZIP SHA256 copied to: %SECONDARY_ZIP_SHA_OUTPUT%
  ) else (
    echo [WARNING] Failed copying ZIP SHA256 to: %SECONDARY_INSTALLER_DIR%
  )
)

call :generate_zip_minisig
if errorlevel 1 (
  echo [ERROR] Failed to generate minisign signature for ZIP.
  exit /b 1
)

if exist "%SECONDARY_INSTALLER_DIR%" (
  copy /Y "%ZIP_MINISIG_FILE%" "%SECONDARY_ZIP_MINISIG_OUTPUT%" >nul
  if exist "%SECONDARY_ZIP_MINISIG_OUTPUT%" (
    echo [INFO] ZIP minisig copied to: %SECONDARY_ZIP_MINISIG_OUTPUT%
  ) else (
    echo [WARNING] Failed copying ZIP minisig to: %SECONDARY_INSTALLER_DIR%
  )
)

echo.
echo [TRACE] ZIP artifact ready: %ZIP_FILE%
echo [TRACE] ZIP checksum file: %ZIP_SHA_FILE%
echo [TRACE] ZIP minisig file: %ZIP_MINISIG_FILE%

echo.
echo [7/8] Publishing ZIP to releases repository...
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
echo Local Output: %LOCAL_INSTALLER_OUTPUT%
echo Local ZIP Output: %LOCAL_ZIP_OUTPUT%
echo OneDrive Output: %SECONDARY_INSTALLER_DIR%\%INSTALLER_FILE%
echo OneDrive ZIP Output: %SECONDARY_ZIP_OUTPUT%
echo OneDrive ZIP SHA256 Output: %SECONDARY_ZIP_SHA_OUTPUT%
echo OneDrive ZIP minisig Output: %SECONDARY_ZIP_MINISIG_OUTPUT%
echo Installer: %INSTALLER_FILE%
echo ZIP: %ZIP_FILE%
echo ZIP SHA256: %ZIP_SHA_FILE%
echo ZIP minisig: %ZIP_MINISIG_FILE%
echo ==============================================================

endlocal
exit /b 0

:publish_zip_release_repo
if not exist "%ZIP_FILE%" (
  echo [ERROR] ZIP file not found; refusing to publish: %ZIP_FILE%
  exit /b 1
)
if not exist "%ZIP_SHA_FILE%" (
  echo [ERROR] ZIP checksum file not found; refusing to publish: %ZIP_SHA_FILE%
  exit /b 1
)
if not exist "%ZIP_MINISIG_FILE%" (
  echo [ERROR] ZIP minisig file not found; refusing to publish: %ZIP_MINISIG_FILE%
  exit /b 1
)

set "RELEASES_REPO_URL=https://github.com/pusoi27/stdytime_releases.git"
set "RELEASES_REPO_BASE=%TEMP%\stdytime_release_cache"
set "RELEASES_REPO_DIR=%RELEASES_REPO_BASE%\stdytime_releases"

if not exist "%RELEASES_REPO_BASE%" mkdir "%RELEASES_REPO_BASE%" >nul 2>nul

if not exist "%RELEASES_REPO_DIR%\.git" (
  echo [INFO] Cloning releases repository...
  git clone "%RELEASES_REPO_URL%" "%RELEASES_REPO_DIR%"
  if errorlevel 1 (
    echo [ERROR] Failed to clone releases repository: %RELEASES_REPO_URL%
    exit /b 1
  )
)

pushd "%RELEASES_REPO_DIR%" >nul
if errorlevel 1 (
  echo [ERROR] Failed to enter releases repository folder: %RELEASES_REPO_DIR%
  exit /b 1
)

git lfs version >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Git LFS is not installed or not available in PATH.
  popd >nul
  exit /b 1
)

git lfs install --local >nul
if errorlevel 1 (
  echo [ERROR] Failed to initialize Git LFS in releases repository.
  popd >nul
  exit /b 1
)
echo [INFO] Git LFS hooks initialized in releases repository.

git lfs track "*.zip" >nul
if errorlevel 1 (
  echo [ERROR] Failed to configure Git LFS tracking for ZIP files.
  popd >nul
  exit /b 1
)
echo [INFO] Git LFS tracking rule ensured: *.zip

set "RELEASES_REPO_EMPTY=0"
git rev-parse --verify HEAD >nul 2>nul
if errorlevel 1 (
  set "RELEASES_REPO_EMPTY=1"
  echo [INFO] Releases repository is empty; preparing initial main branch commit.
  git checkout -B main >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Failed to initialize local main branch in releases repository.
    popd >nul
    exit /b 1
  )
) else (
  git fetch origin
  if errorlevel 1 (
    echo [ERROR] Failed to fetch releases repository updates.
    popd >nul
    exit /b 1
  )

  git checkout main >nul 2>nul
  if errorlevel 1 (
    git checkout master >nul 2>nul
    if errorlevel 1 (
      echo [ERROR] Could not checkout either main or master in releases repository.
      popd >nul
      exit /b 1
    )
  )

  git pull --rebase
  if errorlevel 1 (
    echo [ERROR] Failed to pull latest changes in releases repository.
    popd >nul
    exit /b 1
  )
)

echo [INFO] Purging previous release ZIP artifacts from releases repository...
git rm -f --ignore-unmatch "stdytime_installer_v*.zip" "stdytime_installer_v*.zip.sha256" "stdytime_installer_v*.zip.minisig" >nul 2>nul
for %%F in ("stdytime_installer_v*.zip" "stdytime_installer_v*.zip.sha256" "stdytime_installer_v*.zip.minisig") do (
  if exist "%%~F" del /f /q "%%~F" >nul 2>nul
)
echo [INFO] Previous release ZIP artifacts removed (if any).

copy /Y "%ROOT%%ZIP_FILE%" "%RELEASES_REPO_DIR%\%ZIP_FILE%" >nul
if not exist "%RELEASES_REPO_DIR%\%ZIP_FILE%" (
  echo [ERROR] Failed to copy ZIP into releases repository.
  popd >nul
  exit /b 1
)
echo [INFO] ZIP copied into releases repository working tree: %ZIP_FILE%

copy /Y "%ROOT%%ZIP_SHA_FILE%" "%RELEASES_REPO_DIR%\%ZIP_SHA_FILE%" >nul
if not exist "%RELEASES_REPO_DIR%\%ZIP_SHA_FILE%" (
  echo [ERROR] Failed to copy ZIP SHA256 into releases repository.
  popd >nul
  exit /b 1
)
echo [INFO] ZIP SHA256 copied into releases repository working tree: %ZIP_SHA_FILE%

copy /Y "%ROOT%%ZIP_MINISIG_FILE%" "%RELEASES_REPO_DIR%\%ZIP_MINISIG_FILE%" >nul
if not exist "%RELEASES_REPO_DIR%\%ZIP_MINISIG_FILE%" (
  echo [ERROR] Failed to copy ZIP minisig into releases repository.
  popd >nul
  exit /b 1
)
echo [INFO] ZIP minisig copied into releases repository working tree: %ZIP_MINISIG_FILE%

git add .gitattributes "%ZIP_FILE%" "%ZIP_SHA_FILE%" "%ZIP_MINISIG_FILE%"
if errorlevel 1 (
  echo [ERROR] Failed to stage ZIP sidecars/.gitattributes in releases repository.
  popd >nul
  exit /b 1
)
echo [INFO] Staged .gitattributes, ZIP, SHA256, and minisig for releases repository commit.

git diff --cached --quiet
if errorlevel 1 goto :release_repo_has_changes

echo [INFO] ZIP already up to date in releases repository; nothing to commit.
popd >nul
exit /b 0

:release_repo_has_changes
git commit -m "Add %ZIP_FILE%"
if errorlevel 1 (
  echo [ERROR] Failed to commit ZIP artifacts in releases repository.
  popd >nul
  exit /b 1
)

for /f "delims=" %%C in ('git rev-parse --short HEAD') do set "RELEASES_COMMIT=%%C"
if defined RELEASES_COMMIT echo [INFO] Releases repo commit created: %RELEASES_COMMIT%

echo [INFO] Git LFS tracked files in releases repo:
git lfs ls-files

if "%RELEASES_REPO_EMPTY%"=="1" (
  git push -u origin main
  if errorlevel 1 (
    echo [ERROR] Failed to push initial ZIP commit to releases repository.
    popd >nul
    exit /b 1
  )
) else (
  git push origin HEAD
  if errorlevel 1 (
    echo [ERROR] Failed to push ZIP to releases repository.
    popd >nul
    exit /b 1
  )
)

echo [INFO] ZIP published to releases repository: %RELEASES_REPO_URL%
popd >nul
exit /b 0

:generate_zip_minisig
set "ALLOW_UNSIGNED=%SW_UPDATE_ALLOW_UNSIGNED_RELEASE%"
if /I "%ALLOW_UNSIGNED%"=="1" goto :unsigned_allowed
if /I "%ALLOW_UNSIGNED%"=="true" goto :unsigned_allowed
if /I "%ALLOW_UNSIGNED%"=="yes" goto :unsigned_allowed

where minisign >nul 2>nul
if errorlevel 1 goto :minisign_bin_missing

set "MINISIGN_SECRET_KEY="
for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "$envFile = Join-Path '%ROOT%' '.env'; if (Test-Path $envFile) { $line = Get-Content $envFile | Where-Object { $_ -match '^SW_UPDATE_MINISIGN_SECRET_KEY=' } | Select-Object -First 1; if ($line) { $line.Substring($line.IndexOf('=') + 1).Trim() } }"`) do set "MINISIGN_SECRET_KEY=%%K"
if not defined MINISIGN_SECRET_KEY set "MINISIGN_SECRET_KEY=%SW_UPDATE_MINISIGN_SECRET_KEY%"
if not defined MINISIGN_SECRET_KEY set "MINISIGN_SECRET_KEY=%SW_UPDATE_MINISIGN_PRIVATE_KEY%"
if not defined MINISIGN_SECRET_KEY set "MINISIGN_SECRET_KEY=%SW_UPDATE_MINISIGN_SECRET_KEY_FILE%"
if not defined MINISIGN_SECRET_KEY set "MINISIGN_SECRET_KEY=%SW_UPDATE_MINISIGN_PRIVATE_KEY_FILE%"

if defined MINISIGN_SECRET_KEY if "%MINISIGN_SECRET_KEY:~0,1%"=="\" set "MINISIGN_SECRET_KEY=%MINISIGN_SECRET_KEY:~1%"
if defined MINISIGN_SECRET_KEY if "%MINISIGN_SECRET_KEY:~-1%"=="\" set "MINISIGN_SECRET_KEY=%MINISIGN_SECRET_KEY:~0,-1%"

if not defined MINISIGN_SECRET_KEY goto :minisign_secret_missing
if not exist "%MINISIGN_SECRET_KEY%" goto :minisign_secret_not_found

echo [INFO] Using minisign secret key: %MINISIGN_SECRET_KEY%

if exist "%ZIP_MINISIG_FILE%" del /f /q "%ZIP_MINISIG_FILE%" >nul 2>nul

minisign -S -s "%MINISIGN_SECRET_KEY%" -m "%ZIP_FILE%" -x "%ZIP_MINISIG_FILE%"
if errorlevel 1 goto :minisign_sign_failed

if not exist "%ZIP_MINISIG_FILE%" goto :minisign_output_missing

echo [INFO] ZIP minisig written: %ZIP_MINISIG_FILE%
exit /b 0

:minisign_secret_missing
echo [ERROR] Minisign secret key path is not set.
echo [ERROR] Set SW_UPDATE_MINISIGN_SECRET_KEY in .env or environment.
exit /b 1

:minisign_secret_not_found
echo [ERROR] Minisign secret key file not found: %MINISIGN_SECRET_KEY%
exit /b 1

:minisign_sign_failed
echo [ERROR] minisign signing failed for %ZIP_FILE%.
exit /b 1

:minisign_output_missing
echo [ERROR] minisign did not produce %ZIP_MINISIG_FILE%.
exit /b 1

:unsigned_allowed
echo [WARNING] SW_UPDATE_ALLOW_UNSIGNED_RELEASE is enabled; skipping minisign generation.
if exist "%ZIP_MINISIG_FILE%" goto :unsigned_with_existing_signature
echo [ERROR] No minisig file exists to publish while unsigned override is enabled.
echo [ERROR] Provide %ZIP_MINISIG_FILE% or disable SW_UPDATE_ALLOW_UNSIGNED_RELEASE.
exit /b 1

:minisign_bin_missing
echo [ERROR] minisign is not installed or not in PATH.
echo [ERROR] Install minisign or set SW_UPDATE_ALLOW_UNSIGNED_RELEASE=true to bypass (not recommended).
exit /b 1

:unsigned_with_existing_signature
echo [INFO] Existing minisig retained: %ZIP_MINISIG_FILE%
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
