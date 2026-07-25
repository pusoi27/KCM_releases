@echo off
REM Build NSIS installer for Stdytime
REM - Builds dist_release payload
REM - Compiles stdytime_installer.nsi into versioned installer EXE

setlocal EnableDelayedExpansion

set ROOT=%~dp0
cd /d "%ROOT%"

if not exist .venv\Scripts\python.exe (
  echo [ERROR] .venv Python not found.
  exit /b 1
)

if not exist stdytime_installer.nsi (
  echo [ERROR] NSIS script not found: stdytime_installer.nsi
  exit /b 1
)

REM Build release payload first
call build_release_windows.bat
if errorlevel 1 exit /b 1

REM Read version for installer naming
set APP_VERSION=
if exist "Version" (
  for /f "usebackq delims=" %%v in ("Version") do set APP_VERSION=%%v
) else (
  for /f "usebackq delims=" %%v in ("VERSION") do set APP_VERSION=%%v
)
if "%APP_VERSION%"=="" (
  echo [ERROR] Could not read Version or VERSION file.
  exit /b 1
)

set APP_VERSION_SAFE=%APP_VERSION:.=_%

REM Locate makensis.exe
set MAKENSIS_EXE=
where makensis >nul 2>nul
if not errorlevel 1 (
  for /f "usebackq delims=" %%p in (`where makensis`) do (
    if not defined MAKENSIS_EXE set MAKENSIS_EXE=%%p
  )
)

if not defined MAKENSIS_EXE if exist "C:\Program Files (x86)\NSIS\makensis.exe" set MAKENSIS_EXE=C:\Program Files (x86)\NSIS\makensis.exe
if not defined MAKENSIS_EXE if exist "C:\Program Files\NSIS\makensis.exe" set MAKENSIS_EXE=C:\Program Files\NSIS\makensis.exe

if not defined MAKENSIS_EXE (
  echo [ERROR] makensis.exe not found.
  echo Install NSIS from https://nsis.sourceforge.io/Download
  echo Then re-run this script.
  exit /b 1
)

echo [INFO] Using NSIS: %MAKENSIS_EXE%
"%MAKENSIS_EXE%" /V3 /DAPP_VERSION=%APP_VERSION% stdytime_installer.nsi
if errorlevel 1 (
  echo [ERROR] NSIS compile failed.
  exit /b 1
)

if not exist "stdytime_installer_v%APP_VERSION_SAFE%.exe" (
	echo [ERROR] Expected installer not found: stdytime_installer_v%APP_VERSION_SAFE%.exe
	exit /b 1
)

echo.
echo NSIS installer build complete.
echo Output: %CD%\stdytime_installer_v%APP_VERSION_SAFE%.exe

set SECONDARY_INSTALLER_DIR=C:\Users\octav\OneDrive\ADOCTA TECH LLC\StdyTime
if not exist "%SECONDARY_INSTALLER_DIR%" (
  mkdir "%SECONDARY_INSTALLER_DIR%" >nul 2>nul
)

if exist "%SECONDARY_INSTALLER_DIR%" (
  copy /Y "stdytime_installer_v%APP_VERSION_SAFE%.exe" "%SECONDARY_INSTALLER_DIR%\stdytime_installer_v%APP_VERSION_SAFE%.exe" >nul
  if exist "%SECONDARY_INSTALLER_DIR%\stdytime_installer_v%APP_VERSION_SAFE%.exe" (
    echo [INFO] Installer copied to: %SECONDARY_INSTALLER_DIR%\stdytime_installer_v%APP_VERSION_SAFE%.exe
  ) else (
    echo [WARNING] Failed copying installer to: %SECONDARY_INSTALLER_DIR%
  )
) else (
  echo [WARNING] Secondary installer directory is unavailable: %SECONDARY_INSTALLER_DIR%
)

endlocal
exit /b 0