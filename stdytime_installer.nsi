; Stdytime NSIS Installer
; Builds a proper Windows installer from dist_release payload

Unicode true

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "StrFunc.nsh"

${StrRep}

!define APP_NAME "Stdytime"
!define COMPANY_NAME "Stdytime"
!ifndef APP_VERSION
  !if /FileExists "Version"
    !define /file APP_VERSION "Version"
  !else
    !if /FileExists "VERSION"
      !define /file APP_VERSION "VERSION"
    !else
      !error "Neither Version nor VERSION file was found."
    !endif
  !endif
!endif
!ifndef APP_VERSION_SAFE
  !searchreplace APP_VERSION_SAFE "${APP_VERSION}" "." "_"
!endif
!define UNINSTALL_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}"
!define APP_REG_KEY "Software\\${APP_NAME}"
!define STARTMENU_FOLDER "${APP_NAME}"
!define DESKTOP_SHORTCUT_NAME "StdyTime.lnk"

Var PreviousConfigPath

!if /FileExists "dist_release\Stdytime.exe"
!else
  !error "dist_release\\Stdytime.exe not found. Run build_release_windows.bat first."
!endif

Name "${APP_NAME} ${APP_VERSION}"
OutFile "stdytime_installer_v${APP_VERSION_SAFE}.exe"
InstallDir "$LOCALAPPDATA\\${APP_NAME}"
RequestExecutionLevel user

; --- Modern UI ---
!define MUI_ABORTWARNING
!define MUI_ICON "assets\\stdytime.ico"
!define MUI_UNICON "assets\\stdytime.ico"

!define MUI_FINISHPAGE_RUN "$INSTDIR\\Stdytime.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Stdytime"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Function .onInit
  StrCpy $PreviousConfigPath ""

  ; Keep a single stable install folder for first install and all updates.
  StrCpy $INSTDIR "$LOCALAPPDATA\\${APP_NAME}"

  ; If this exact version is already installed, prompt and fully exit installer.
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "DisplayVersion"
  ${If} $0 == "${APP_VERSION}"
    MessageBox MB_OK|MB_ICONINFORMATION "Stdytime version ${APP_VERSION} is already installed.$\r$\nSetup will now close."
    Quit
  ${EndIf}

  ; Prefer current stable install config if it already exists.
  IfFileExists "$INSTDIR\\db_config.json" 0 +3
    StrCpy $PreviousConfigPath "$INSTDIR\\db_config.json"

  ; Detect legacy versioned install config so upgrades can migrate Google Drive path once.
  ${If} $PreviousConfigPath == ""
  FindFirst $0 $1 "$LOCALAPPDATA\\Stdytime_*"
  loop_search_previous:
    IfErrors done_search_previous
    StrCmp $1 "" done_search_previous
    IfFileExists "$LOCALAPPDATA\\$1\\db_config.json" 0 +3
      StrCpy $PreviousConfigPath "$LOCALAPPDATA\\$1\\db_config.json"
      Goto done_search_previous
    FindNext $0 $1
    Goto loop_search_previous
  done_search_previous:
  FindClose $0
  ${EndIf}
FunctionEnd

Function WriteFreshDbConfig
  ${StrRep} $0 $LOCALAPPDATA "\\" "/"

  FileOpen $2 "$INSTDIR\\db_config.json" w
  FileWrite $2 "{$\r$\n"
  FileWrite $2 "  $\"_comment$\": $\"db_path = local machine path (fast, all session reads/writes go here).$\",$\r$\n"
  FileWrite $2 "  $\"_comment2$\": $\"cloud_provider = onedrive (Windows OneDrive backup destination).$\",$\r$\n"
  FileWrite $2 "  $\"_comment3$\": $\"onedrive_sync_path = folder path used only for background sync; Stdytime.db is created there automatically.$\",$\r$\n"
  FileWrite $2 "  $\"_comment4$\": $\"sync_interval_minutes = fixed system-managed value (9 minutes).$\",$\r$\n"
  FileWrite $2 "  $\"db_path$\": $\"$0/StdyTime/Stdytime.db$\",$\r$\n"
  FileWrite $2 "  $\"cloud_provider$\": $\"onedrive$\",$\r$\n"
  FileWrite $2 "  $\"gdrive_sync_path$\": $\"$\",$\r$\n"
  FileWrite $2 "  $\"onedrive_sync_path$\": $\"$0/OneDrive/StdyTime$\",$\r$\n"
  FileWrite $2 "  $\"sync_interval_minutes$\": 9,$\r$\n"
  FileWrite $2 "  $\"startup_pull_from_gdrive$\": false$\r$\n"
  FileWrite $2 "}$\r$\n"
  FileClose $2
FunctionEnd

Function EnsureStdytimeStopped
  ; Stop any running packaged app instance so files can be overwritten.
  nsExec::ExecToLog 'taskkill /F /IM Stdytime.exe'
  Sleep 1200

  ; If executable still cannot be deleted, it is locked by another process/session.
  IfFileExists "$INSTDIR\\Stdytime.exe" 0 done_stop
  ClearErrors
  Delete "$INSTDIR\\Stdytime.exe"
  IfErrors 0 restore_exe

  MessageBox MB_OK|MB_ICONSTOP "Stdytime appears to still be running.$\r$\nPlease close all Stdytime windows (including tray/background) and run the installer again."
  Abort

restore_exe:
  ; Recreate placeholder by copying current EXE from payload during install section.
done_stop:
FunctionEnd

Function .onInstSuccess
  ; End-of-install cleanup: kill any process listening on 127.0.0.1:5000.
  nsExec::ExecToLog 'cmd /c for /f "tokens=5" %P in (''netstat -ano ^| findstr /R /C:"127.0.0.1:5000"'') do taskkill /F /PID %P >nul 2>&1'

  ; Prompt restart and first-time environment requirement.
  MessageBox MB_OK|MB_ICONINFORMATION "Installation completed.$\r$\nPlease restart your machine for the settings to take effect.$\r$\nThis installer works only in a Windows environment with OneDrive setup."
FunctionEnd

Section "Stdytime (required)" SecMain
  SectionIn RO

  ; Always install/update in one stable folder.
  StrCpy $INSTDIR "$LOCALAPPDATA\\${APP_NAME}"

  ; Upgrades must overwrite existing binaries.
  SetOverwrite on
  Call EnsureStdytimeStopped

  SetOutPath "$INSTDIR"
  ; Never overwrite user local DB/config/backup artifacts during update.
  File /r /x "*.db" /x "db_config.json" /x "data\\backups\\*.*" "dist_release\*.*"

  ; Preserve existing config in-place. Only migrate/create when missing.
  IfFileExists "$INSTDIR\\db_config.json" 0 +3
    DetailPrint "Keeping existing db_config.json in install folder."
    Goto done_config

  ${If} $PreviousConfigPath != ""
    CopyFiles /SILENT "$PreviousConfigPath" "$INSTDIR\\db_config.json"
    DetailPrint "Reused previous backup config from prior install: $PreviousConfigPath"
  ${Else}
    ; Fresh install behavior: create config with default sync path.
    Call WriteFreshDbConfig
    DetailPrint "Created db_config.json with default OneDrive sync path."
  ${EndIf}
done_config:

  ; Archive old legacy versioned codebase folders except current
  nsExec::ExecToLog 'cmd /c for /d %F in ("$LOCALAPPDATA\Stdytime_*") do if /I not "%F"=="$INSTDIR" move "%F" "$LOCALAPPDATA\Stdytime_archive" >nul'

  ; App location registry
  WriteRegStr HKCU "${APP_REG_KEY}" "InstallDir" "$INSTDIR"
  ; Ensure database folder exists
  CreateDirectory "$LOCALAPPDATA\Stdytime"

  ; Start menu shortcuts
  CreateDirectory "$SMPROGRAMS\\${STARTMENU_FOLDER}"
  CreateShortcut "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Stdytime.lnk" "$INSTDIR\\Stdytime.exe" "" "$INSTDIR\\Stdytime.exe" 0
  CreateShortcut "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Readme.lnk" "$INSTDIR\\INSTALL_README_WINDOWS.txt"
  CreateShortcut "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Uninstall Stdytime.lnk" "$INSTDIR\\Uninstall.exe" "" "$INSTDIR\\Stdytime.exe" 0

  ; Uninstaller
  WriteUninstaller "$INSTDIR\\Uninstall.exe"

  ; Windows Programs & Features entry
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${COMPANY_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\\Stdytime.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" "$\"$INSTDIR\\Uninstall.exe$\""
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
SectionEnd

Section "Create Desktop Icon" SecDesktopIcon
  CreateShortcut "$DESKTOP\\${DESKTOP_SHORTCUT_NAME}" "$INSTDIR\\Stdytime.exe" "" "$INSTDIR\\Stdytime.exe" 0
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "Install the core Stdytime application files."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktopIcon} "Create a shortcut on your desktop."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  Delete "$DESKTOP\\${DESKTOP_SHORTCUT_NAME}"
  Delete "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Stdytime.lnk"
  Delete "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Readme.lnk"
  Delete "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Uninstall Stdytime.lnk"
  RMDir "$SMPROGRAMS\\${STARTMENU_FOLDER}"

  Delete "$INSTDIR\\Uninstall.exe"
  RMDir /r "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${APP_REG_KEY}"
SectionEnd
