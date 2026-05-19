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
  !define /file APP_VERSION "VERSION"
!endif
!ifndef APP_VERSION_SAFE
  !searchreplace APP_VERSION_SAFE "${APP_VERSION}" "." "_"
!endif
!define UNINSTALL_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}"
!define APP_REG_KEY "Software\\${APP_NAME}"
!define STARTMENU_FOLDER "${APP_NAME}"

Var PreviousConfigPath
Var SelectedGDrivePath
Var PromptForGDrivePath
Var GDrivePageDialog
Var GDrivePathInput

!if /FileExists "dist_release\Stdytime.exe"
!else
  !error "dist_release\\Stdytime.exe not found. Run build_release_windows.bat first."
!endif

Name "${APP_NAME} ${APP_VERSION}"
OutFile "stdytime_installer_v${APP_VERSION_SAFE}.exe"
InstallDir "$LOCALAPPDATA\\${APP_NAME}_${APP_VERSION_SAFE}"
RequestExecutionLevel user

; --- Modern UI ---
!define MUI_ABORTWARNING
!define MUI_ICON "assets\\stdytime.ico"
!define MUI_UNICON "assets\\stdytime.ico"

!define MUI_FINISHPAGE_RUN "$INSTDIR\\Stdytime.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Stdytime"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page Custom GDrivePathPageCreate GDrivePathPageLeave
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Function .onInit
  StrCpy $PreviousConfigPath ""
  StrCpy $SelectedGDrivePath ""
  StrCpy $PromptForGDrivePath "1"

  ; Keep each version in its own folder by default.
  StrCpy $INSTDIR "$LOCALAPPDATA\\${APP_NAME}_${APP_VERSION_SAFE}"

  ; Detect previous install config so upgrades can reuse existing Google Drive path.
  FindFirst $0 $1 "$LOCALAPPDATA\\Stdytime_*"
  loop_search_previous:
    IfErrors done_search_previous
    StrCmp $1 "" done_search_previous
    IfFileExists "$LOCALAPPDATA\\$1\\db_config.json" 0 +3
      StrCpy $PreviousConfigPath "$LOCALAPPDATA\\$1\\db_config.json"
      StrCpy $PromptForGDrivePath "0"
      Goto done_search_previous
    FindNext $0 $1
    Goto loop_search_previous
  done_search_previous:
  FindClose $0
FunctionEnd

Function GDrivePathPageCreate
  ${If} $PromptForGDrivePath != "1"
    Abort
  ${EndIf}

  nsDialogs::Create 1018
  Pop $GDrivePageDialog
  ${If} $GDrivePageDialog == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 28u "No previous Stdytime install was detected on this machine.$\r$\nChoose your Google Drive backup folder (for example: G:\\My Drive\\StdyTime)."
  Pop $0

  ${NSD_CreateDirRequest} 0 36u 100% 12u "$SelectedGDrivePath"
  Pop $GDrivePathInput

  nsDialogs::Show
FunctionEnd

Function GDrivePathPageLeave
  ${If} $PromptForGDrivePath != "1"
    Return
  ${EndIf}

  ${NSD_GetText} $GDrivePathInput $SelectedGDrivePath
  StrCmp $SelectedGDrivePath "" 0 +3
    MessageBox MB_OK|MB_ICONEXCLAMATION "Please choose your Google Drive backup folder to continue."
    Abort

  ; Basic location check to reduce accidental wrong folder selections.
  IfFileExists "$SelectedGDrivePath\\*.*" valid_path +3
    MessageBox MB_OK|MB_ICONEXCLAMATION "Selected folder does not exist. Please choose an existing Google Drive folder."
    Abort

valid_path:
FunctionEnd

Function WriteFreshDbConfig
  ${StrRep} $0 $LOCALAPPDATA "\\" "/"
  ${StrRep} $1 $SelectedGDrivePath "\\" "/"

  FileOpen $2 "$INSTDIR\\db_config.json" w
  FileWrite $2 "{$\r$\n"
  FileWrite $2 "  $\"_comment$\": $\"db_path = local machine path (fast, all session reads/writes go here).$\",$\r$\n"
  FileWrite $2 "  $\"_comment2$\": $\"gdrive_sync_path = Google Drive folder path used only for background sync; Stdytime.db is created there automatically.$\",$\r$\n"
  FileWrite $2 "  $\"_comment3$\": $\"sync_interval_minutes = how often local DB is pushed to Google Drive (0 = disable).$\",$\r$\n"
  FileWrite $2 "  $\"db_path$\": $\"$0/StdyTime/Stdytime.db$\",$\r$\n"
  FileWrite $2 "  $\"gdrive_sync_path$\": $\"$1$\",$\r$\n"
  FileWrite $2 "  $\"sync_interval_minutes$\": 7,$\r$\n"
  FileWrite $2 "  $\"startup_pull_from_gdrive$\": false$\r$\n"
  FileWrite $2 "}$\r$\n"
  FileClose $2
FunctionEnd

Section "Stdytime (required)" SecMain
  SectionIn RO

  ; Check if this exact version is already installed
  ReadRegStr $0 HKCU "${UNINSTALL_KEY}" "DisplayVersion"
  ${If} $0 == "${APP_VERSION}"
    MessageBox MB_OK|MB_ICONINFORMATION "Stdytime version ${APP_VERSION} is already installed.$\r$\nInstallation will now stop."
    Abort
  ${EndIf}

  ; Always install new versions into their own versioned folder.
  StrCpy $INSTDIR "$LOCALAPPDATA\\${APP_NAME}_${APP_VERSION_SAFE}"

  SetOutPath "$INSTDIR"
  File /r "dist_release\*.*"

  ; Upgrade behavior: copy previous install config into the new version folder.
  ${If} $PreviousConfigPath != ""
    CopyFiles /SILENT "$PreviousConfigPath" "$INSTDIR\\db_config.json"
    DetailPrint "Reused Google Drive config from previous install: $PreviousConfigPath"
  ${Else}
    ; Fresh install behavior: create config from installer prompt.
    Call WriteFreshDbConfig
    DetailPrint "Created db_config.json from installer Google Drive path."
  ${EndIf}

  ; Archive old codebase folders except current
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

Section "Uninstall"
  Delete "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Stdytime.lnk"
  Delete "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Readme.lnk"
  Delete "$SMPROGRAMS\\${STARTMENU_FOLDER}\\Uninstall Stdytime.lnk"
  RMDir "$SMPROGRAMS\\${STARTMENU_FOLDER}"

  Delete "$INSTDIR\\Uninstall.exe"
  RMDir /r "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${APP_REG_KEY}"
SectionEnd
