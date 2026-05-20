@echo off
REM One-click Windows build/release script for Stdytime
REM Builds .exe and copies all required files to dist_release

setlocal
set PYTHON_EXE=python
set VENV_DIR=.venv
set DIST_DIR=dist
set RELEASE_DIR=dist_release

REM Activate venv if present
if exist %VENV_DIR%\Scripts\activate.bat call %VENV_DIR%\Scripts\activate.bat

REM Clean previous builds
if exist %DIST_DIR% rmdir /s /q %DIST_DIR%
if exist %RELEASE_DIR% rmdir /s /q %RELEASE_DIR%

REM Build executable with PyInstaller
%PYTHON_EXE% -m PyInstaller stdytime.spec
if errorlevel 1 exit /b 1

REM Copy release files
mkdir %RELEASE_DIR%
copy %DIST_DIR%\Stdytime.exe %RELEASE_DIR%\
copy .env %RELEASE_DIR%\
copy db_config.json.example %RELEASE_DIR%\
copy INSTALL_README_WINDOWS.txt %RELEASE_DIR%\
if exist Version (
	copy Version %RELEASE_DIR%\VERSION
) else (
	copy VERSION %RELEASE_DIR%\
)
REM Copy folders
xcopy templates %RELEASE_DIR%\templates /E /I /Y
xcopy static %RELEASE_DIR%\static /E /I /Y
xcopy assets %RELEASE_DIR%\assets /E /I /Y
xcopy data %RELEASE_DIR%\data /E /I /Y

REM Done
@echo.
@echo Release build complete! Artifacts in %RELEASE_DIR%\
endlocal
