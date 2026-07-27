@echo off
setlocal
set "ROOT=%~dp0"
set "MINISIGN_SECRET_KEY="
echo ROOT=[%ROOT%]
if exist "%ROOT%.env" (
  echo ENV_EXISTS=YES
  for /f "usebackq tokens=1,* delims==" %%L in ("%ROOT%.env") do (
    if /I "%%L"=="SW_UPDATE_MINISIGN_SECRET_KEY" (
      echo MATCH_KEY=[%%L]
      echo MATCH_VALUE=[%%M]
      set "MINISIGN_SECRET_KEY=%%M"
    )
  )
) else (
  echo ENV_EXISTS=NO
)
echo FINAL_KEY=[%MINISIGN_SECRET_KEY%]
endlocal
