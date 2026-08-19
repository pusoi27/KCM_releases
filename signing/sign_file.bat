@echo off
REM Signs a file using Azure Trusted Signing (account "SdtyTime", profile "StdyTime").
REM Usage: signing\sign_file.bat <path-to-exe>
REM Requires: az login (with Artifact Signing Certificate Profile Signer role on the account)

setlocal
set ROOT=%~dp0
set TARGET=%~1

if "%TARGET%"=="" (
	echo [ERROR] Usage: sign_file.bat ^<path-to-file^>
	exit /b 1
)
if not exist "%TARGET%" (
	echo [ERROR] File not found: %TARGET%
	exit /b 1
)

set DLIB=%ROOT%dlib_x64\Azure.CodeSigning.Dlib.dll
set DMDF=%ROOT%metadata.json

if not exist "%DLIB%" (
	echo [ERROR] Signing dlib not found: %DLIB%
	exit /b 1
)

set SIGNTOOL_EXE=
where signtool >nul 2>nul
if not errorlevel 1 (
	for /f "usebackq delims=" %%p in (`where signtool`) do (
		if not defined SIGNTOOL_EXE set SIGNTOOL_EXE=%%p
	)
)
if not defined SIGNTOOL_EXE if exist "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe" set SIGNTOOL_EXE=C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe
if not defined SIGNTOOL_EXE (
	echo [ERROR] signtool.exe not found. Install the Windows SDK.
	exit /b 1
)

echo [INFO] Signing "%TARGET%" via Azure Trusted Signing...
"%SIGNTOOL_EXE%" sign /v /fd SHA256 /tr http://timestamp.acs.microsoft.com /td SHA256 /dlib "%DLIB%" /dmdf "%DMDF%" "%TARGET%"
if errorlevel 1 (
	echo [ERROR] Signing failed for %TARGET%
	exit /b 1
)

endlocal
exit /b 0
