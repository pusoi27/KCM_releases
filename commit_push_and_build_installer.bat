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

set "COMMIT_MSG=%~1"
if "%COMMIT_MSG%"=="" (
  set /p COMMIT_MSG=Enter commit message: 
)

if "%COMMIT_MSG%"=="" (
  echo [ERROR] Commit message is required.
  exit /b 1
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
echo [4/5] Pushing to GitHub...
git push
if errorlevel 1 (
  echo [ERROR] git push failed.
  exit /b 1
)

echo.
echo [5/5] Building NSIS installer...
call build_nsis_installer.bat
if errorlevel 1 (
  echo [ERROR] Installer build failed.
  exit /b 1
)

set "APP_VERSION="
if exist "VERSION" (
  set /p APP_VERSION=<"VERSION"
) else if exist "Version" (
  set /p APP_VERSION=<"Version"
)

if "%APP_VERSION%"=="" (
  echo [WARNING] Could not read VERSION for checksum generation.
  goto :done
)

set "APP_VERSION_SAFE=%APP_VERSION:.=_%"
set "INSTALLER_FILE=stdytime_installer_v%APP_VERSION_SAFE%.exe"
set "SHA_FILE=%INSTALLER_FILE%.sha256"

if not exist "%INSTALLER_FILE%" (
  echo [WARNING] Installer file not found for checksum: %INSTALLER_FILE%
  goto :done
)

REM Primary hash path: Python writes SHA256 file directly (most reliable in this repo setup)
".venv\Scripts\python.exe" -c "import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); out=pathlib.Path(sys.argv[2]); h=hashlib.sha256(p.read_bytes()).hexdigest(); out.write_text(f'{h}  {p.name}\\n', encoding='ascii')" "%INSTALLER_FILE%" "%SHA_FILE%"
if errorlevel 1 (
  echo [WARNING] Python SHA256 generation failed, trying certutil fallback...
  set "SHA_HASH="
  for /f "tokens=1 delims= " %%H in ('certutil -hashfile "%INSTALLER_FILE%" SHA256 ^| findstr /R /I "^[0-9a-f][0-9a-f]*$"') do (
    if not defined SHA_HASH set "SHA_HASH=%%H"
  )
  if not defined SHA_HASH (
    echo [WARNING] Failed to generate SHA256 hash (python and certutil failed).
    goto :done
  )
  > "%SHA_FILE%" echo %SHA_HASH%  %INSTALLER_FILE%
  if errorlevel 1 (
    echo [WARNING] Failed writing SHA256 file: %SHA_FILE%
    goto :done
  )
)

echo [INFO] SHA256 written: %SHA_FILE%

:done
echo.
echo ==============================================================
echo Completed successfully.
echo Installer: %INSTALLER_FILE%
echo ==============================================================

endlocal
exit /b 0
