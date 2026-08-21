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

REM Generate Windows version resource (CompanyName/ProductName/etc.) from VERSION file
%PYTHON_EXE% scripts\gen_version_info.py
if errorlevel 1 exit /b 1

REM Build executable with PyInstaller
%PYTHON_EXE% -m PyInstaller stdytime.spec
if errorlevel 1 exit /b 1

REM Copy release files
mkdir %RELEASE_DIR%
copy %DIST_DIR%\Stdytime.exe %RELEASE_DIR%\
call signing\sign_file.bat %RELEASE_DIR%\Stdytime.exe
if errorlevel 1 exit /b 1
copy app.py %RELEASE_DIR%\
copy launcher.py %RELEASE_DIR%\
copy launcher_browser.py %RELEASE_DIR%\
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
xcopy modules %RELEASE_DIR%\modules /E /I /Y
xcopy routes %RELEASE_DIR%\routes /E /I /Y

REM Copy only static data assets (exclude runtime DB, backups, WAL/SHM, temp files)
mkdir %RELEASE_DIR%\data
if exist data\award_rules.json copy data\award_rules.json %RELEASE_DIR%\data\
if exist data\grade_level_criteria.json copy data\grade_level_criteria.json %RELEASE_DIR%\data\

call :validate_release_payload
if errorlevel 1 exit /b 1

REM Done
@echo.
@echo Release build complete! Artifacts in %RELEASE_DIR%\
endlocal
goto :eof

:validate_release_payload
if not exist %RELEASE_DIR%\Stdytime.exe (
	echo [ERROR] Missing %RELEASE_DIR%\Stdytime.exe
	exit /b 1
)
if not exist %RELEASE_DIR%\templates (
	echo [ERROR] Missing %RELEASE_DIR%\templates folder
	exit /b 1
)
if not exist %RELEASE_DIR%\static (
	echo [ERROR] Missing %RELEASE_DIR%\static folder
	exit /b 1
)
if not exist %RELEASE_DIR%\assets (
	echo [ERROR] Missing %RELEASE_DIR%\assets folder
	exit /b 1
)
if not exist %RELEASE_DIR%\modules (
	echo [ERROR] Missing %RELEASE_DIR%\modules folder
	exit /b 1
)
if not exist %RELEASE_DIR%\routes (
	echo [ERROR] Missing %RELEASE_DIR%\routes folder
	exit /b 1
)
if not exist %RELEASE_DIR%\app.py (
	echo [ERROR] Missing %RELEASE_DIR%\app.py
	exit /b 1
)
if not exist %RELEASE_DIR%\launcher.py (
	echo [ERROR] Missing %RELEASE_DIR%\launcher.py
	exit /b 1
)
if not exist %RELEASE_DIR%\launcher_browser.py (
	echo [ERROR] Missing %RELEASE_DIR%\launcher_browser.py
	exit /b 1
)
if not exist %RELEASE_DIR%\modules\database.py (
	echo [ERROR] Missing %RELEASE_DIR%\modules\database.py
	exit /b 1
)
if not exist %RELEASE_DIR%\routes\api.py (
	echo [ERROR] Missing %RELEASE_DIR%\routes\api.py
	exit /b 1
)
if not exist %RELEASE_DIR%\VERSION (
	echo [ERROR] Missing %RELEASE_DIR%\VERSION
	exit /b 1
)
exit /b 0
