; Stdytime NSIS Installer
; Builds a proper Windows installer from dist_release payload

Unicode true

!include "MUI2.nsh"
!include "FileFunc.nsh"

!define APP_NAME "Stdytime"
!define COMPANY_NAME "Stdytime"
!ifndef APP_VERSION
  !define /file APP_VERSION "VERSION"
!endif
!searchreplace APP_VERSION_SAFE "${APP_VERSION}" "." "_"
!define UNINSTALL_KEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${APP_NAME}"
!define APP_REG_KEY "Software\\${APP_NAME}"
!define STARTMENU_FOLDER "${APP_NAME}"

!if /FileExists "dist_release\Stdytime.exe"
!else
  !error "dist_release\\Stdytime.exe not found. Run build_release_windows.bat first."
!endif

Name "${APP_NAME} ${APP_VERSION}"
OutFile "stdytime_installer_v${APP_VERSION_SAFE}.exe"
InstallDir "$LOCALAPPDATA\\${APP_NAME}"
InstallDirRegKey HKCU "${APP_REG_KEY}" "InstallDir"
RequestExecutionLevel user

; --- Modern UI ---
!define MUI_ABORTWARNING
!define MUI_ICON "assets\\stdytime.ico"
!define MUI_UNICON "assets\\stdytime.ico"

!define MUI_FINISHPAGE_RUN "$INSTDIR\\Stdytime.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch Stdytime"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

!insertmacro MUI_LANGUAGE "English"

Section "Stdytime (required)" SecMain
  SectionIn RO

  SetOutPath "$INSTDIR"
  File /r "dist_release\*.*"

  ; App location registry
  WriteRegStr HKCU "${APP_REG_KEY}" "InstallDir" "$INSTDIR"

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
