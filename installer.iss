; Stdytime Inno Setup Installer Script
; Shows a 30–40 second progress bar regardless of actual speed
; Requires Inno Setup (free): https://jrsoftware.org/isinfo.php

#define AppVersion Trim(FileRead("VERSION"))
#define AppVersionSafe StringChange(AppVersion, ".", "_")

[Setup]
AppName=Stdytime
AppVersion={#AppVersion}
AppId=Stdytime_{#AppVersionSafe}
DefaultDirName={autopf}\Stdytime\{#AppVersion}
DefaultGroupName=Stdytime {#AppVersion}
UninstallDisplayIcon={app}\Stdytime.exe
SetupIconFile=assets\stdytime.ico
InfoAfterFile=INSTALL_README_WINDOWS.txt
Compression=lzma
SolidCompression=yes
OutputDir=.
OutputBaseFilename=stdytime_installer_v{#AppVersionSafe}
DisableFinishedPage=no
DisableWelcomePage=no
CloseApplications=yes
CloseApplicationsFilter=Stdytime.exe
RestartApplications=no

[Files]
Source: "dist_release\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Stdytime"; Filename: "{app}\Stdytime.exe"
Name: "{group}\Stdytime Readme"; Filename: "{app}\INSTALL_README_WINDOWS.txt"
Name: "{group}\Uninstall Stdytime"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\INSTALL_README_WINDOWS.txt"; Description: "View setup and Google Drive backup instructions"; Flags: postinstall shellexec skipifsilent unchecked
Filename: "{app}\Stdytime.exe"; Description: "Launch Stdytime"; Flags: nowait postinstall skipifsilent

[Code]
var
  ProgressDurationMs: Integer;


procedure CloseRunningStdytimeInstances();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'), '/C taskkill /IM Stdytime.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM Stdytime.exe /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;


function InitializeSetup(): Boolean;
begin
  CloseRunningStdytimeInstances();
  Result := True;
end;

procedure InitializeWizard();
begin
  // Fixed perceived-install duration: 35 seconds
  // (within requested 30–40 second window)
  ProgressDurationMs := 35000;
end;

procedure RunPerceivedInstallDelay();
var
  i: Integer;
  steps: Integer;
  stepDelayMs: Integer;
begin
  steps := 100;
  stepDelayMs := ProgressDurationMs div steps;

  WizardForm.StatusLabel.Caption := 'Finalizing installation...';
  WizardForm.ProgressGauge.Min := 0;
  WizardForm.ProgressGauge.Max := steps;
  WizardForm.ProgressGauge.Position := 0;

  for i := 1 to steps do
  begin
    WizardForm.ProgressGauge.Position := i;
    WizardForm.StatusLabel.Caption := Format('Finalizing installation... %d%%', [i]);
    WizardForm.Update;
    Sleep(stepDelayMs);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    CloseRunningStdytimeInstances();
  end;

  if CurStep = ssPostInstall then
  begin
    RunPerceivedInstallDelay;
  end;
end;
