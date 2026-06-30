; ===========================================================================
; VoiceFlow - Inno Setup installer script
; ---------------------------------------------------------------------------
; Wraps the PyInstaller onedir output (dist\VoiceFlow\) into a friendly,
; per-user installer. No administrator rights required: it installs into
;   %LOCALAPPDATA%\Programs\VoiceFlow
; and writes its config/logs/models to %LOCALAPPDATA%\VoiceFlow at runtime
; (handled by the app, not the installer).
;
; Build:
;   1) Build the app first:   pyinstaller --noconfirm --clean packaging\voiceflow.spec
;      -> produces dist\VoiceFlow\VoiceFlow.exe
;   2) Compile this script:   ISCC packaging\installer.iss
;      -> produces dist\VoiceFlow-Setup-1.0.0.exe
;   (scripts\build.bat does both in one shot.)
;
; "Start at login": optional task. When chosen, the installer adds a per-user
; HKCU ...\Run entry that launches the headless background runtime
; (VoiceFlow.exe --background) so dictation is ready right after sign-in without
; opening the window. Unchecking the task / uninstalling removes the Run entry.
; ===========================================================================

#define MyAppName "VoiceFlow"
; Version is the single source of truth in src\voiceflow\__init__.py. build.bat
; passes it in via /DMyAppVersion=<ver>; the literal below is the fallback when
; compiling the .iss directly.
#ifndef MyAppVersion
#define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "VoiceFlow Project"
#define MyAppURL "https://github.com/voiceflow/voiceflow"
#define MyAppExeName "VoiceFlow.exe"
; Path (relative to this .iss) to the PyInstaller onedir output.
#define MyAppDist "..\dist\VoiceFlow"

[Setup]
; A fresh, stable GUID identifies this product across upgrades/uninstall.
AppId={{D005655E-441D-4F18-8F64-2A602E877640}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoCompany={#MyAppPublisher}

; --- Per-user install: no admin prompt -------------------------------------
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
; Install for the current user only; show under the user's Apps list.
UsedUserAreasWarning=no

; --- Output installer ------------------------------------------------------
OutputDir=..\dist
; Must match the website download path + latest.json `url` (OpenVerba-Setup-<ver>.exe).
OutputBaseFilename=OpenVerba-Setup-{#MyAppVersion}
SetupIconFile=..\assets\voiceflow.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Close/restart our app cleanly on upgrade.
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startatlogin"; Description: "Start VoiceFlow automatically when I sign in (runs in the background, ready to dictate)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; Recursively copy the entire PyInstaller onedir output. recursesubdirs +
; createallsubdirs keeps the nested _internal layout intact.
Source: "{#MyAppDist}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu (per-user programs folder).
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
; Optional desktop shortcut.
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; "Start at login" -> per-user Run key launching the headless background runtime.
; Flags uninsdeletevalue removes it on uninstall; the entry is only written when
; the startatlogin task is selected.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "VoiceFlow"; ValueData: """{app}\{#MyAppExeName}"" --background"; Flags: uninsdeletevalue; Tasks: startatlogin

[Run]
; Offer to launch the GUI right after install (so the user hits onboarding /
; first-run model download immediately).
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Best-effort: stop any running VoiceFlow before files are removed so the
; onedir delete doesn't fail on a locked exe/DLL. taskkill is present on Win10/11.
Filename: "{cmd}"; Parameters: "/C taskkill /IM ""{#MyAppExeName}"" /F"; Flags: runhidden; RunOnceId: "KillVoiceFlow"

[UninstallDelete]
; We do NOT delete the user data dir (%LOCALAPPDATA%\VoiceFlow) on uninstall so
; downloaded models + config survive a reinstall. Users can remove it manually.
; (Intentionally left empty.)

[Code]
{ Before copying files, make sure no VoiceFlow instance is holding the onedir's
  exe/DLLs open (Windows share-deny-write would otherwise fail the in-place
  overwrite during an upgrade). The in-app updater already exits before launching
  us, but a still-open GUI or background runtime — or a manual re-run of the
  installer — would lock files. taskkill is the reliable closer here because the
  background runtime is a windowless tray/hook process that CloseApplications
  cannot reach via window messages. The installer image is OpenVerba-Setup-*.exe,
  not VoiceFlow.exe, so this never kills the installer itself. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/C taskkill /IM "{#MyAppExeName}" /F',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
