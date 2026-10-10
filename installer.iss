; installer.iss
; Inno Setup 6 script that packages the PyInstaller build (dist\AditsVoiceStudio\)
; into a single Windows Setup.exe.
;
; Build both in one step (version, publisher and URL come from core\version.py):
;   .\venv_311\Scripts\python.exe build_exe.py --installer
; or, after a build, compile only the installer:
;   & "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer.iss
;
; Code signing (off by default). With a certificate, define a sign tool and
; pass /DUseSignTool, for example:
;   ISCC "/Ssigntool=signtool.exe sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a $f" /DUseSignTool installer.iss
;
; Size: a single Setup.exe can hold up to about 4.2 GB compressed. If the
; build ever grows past that, set DiskSpanning=yes and DiskSliceSize=max
; and ship the resulting Setup.exe together with its .bin file.

; Defaults when ISCC is run directly; build_exe.py passes these with /D.
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef MyAppPublisher
  #define MyAppPublisher "iftiaj.com"
#endif
#ifndef MyAppURL
  #define MyAppURL "https://github.com/iftiaj-com/AditsVoiceStudio"
#endif
#ifndef MyAppTitle
  #define MyAppTitle "Adits Voice Studio"
#endif
#ifndef MyAppName
  #define MyAppName "AditsVoiceStudio"
#endif
#ifndef MyAppExeBase
  #define MyAppExeBase "AditsVoiceStudio"
#endif
#define MyAppExeName MyAppExeBase + ".exe"
#define MyAppSourceDir "dist\" + MyAppExeBase
; Must match _MUTEX_NAME in tts_app.py.
#define MyAppMutex "AditsVoiceStudioSingleInstance"
; Before the rebrand (VoiceCraft TTS Studio). An upgrade over such an install
; removes the old EXE and shortcuts; the app moves the old data folder itself.
#define LegacyExeName "TTS_Studio.exe"
#define LegacyTitle "VoiceCraft TTS Studio"

[Setup]
; Never change AppId: upgrades find the existing install through it (it
; also carried the VoiceCraft installs over to the new name).
AppId={{B6E3F5C2-6E4B-4B7B-9C1A-2F3A6F8C5D91}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppTitle} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppTitle} Setup
VersionInfoProductName={#MyAppTitle}
VersionInfoProductVersion={#MyAppVersion}

; Per-user install to %LocalAppData%\Programs\AditsVoiceStudio: no admin prompt.
; An upgrade keeps the folder of the existing install.
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest

LicenseFile=LICENSE.txt
SetupIconFile=assets\adits_voice_studio.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppTitle}
WizardStyle=modern
DisableWelcomePage=no

OutputDir=installer_output
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
; About 7 GB of mostly DLLs. ultra64 needs ~0.75 GB RAM per thread to compress.
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=2

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Room for models downloaded on first use (MeloTTS English is ~620 MB).
ExtraDiskSpaceRequired=1500000000

; Refuse to overwrite a running copy, and close it through Restart Manager.
AppMutex={#MyAppMutex}
CloseApplications=yes

#ifdef UseSignTool
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; An upgrade must not keep files the new build no longer ships: torch loads
; every DLL it finds in torch\lib, so stale ones are not harmless. No user
; data lives here (it is in %LocalAppData%\AditsVoiceStudio and Documents).
; Only when the folder holds our EXE, so another app's _internal is safe.
Type: filesandordirs; Name: "{app}\_internal"; Check: IsOurInstall
; Pre-rebrand EXE and shortcuts.
Type: files; Name: "{app}\{#LegacyExeName}"; Check: IsOurInstall
Type: files; Name: "{group}\{#LegacyTitle}.lnk"
Type: files; Name: "{group}\{cm:UninstallProgram,{#LegacyTitle}}.lnk"
Type: files; Name: "{autodesktop}\{#LegacyTitle}.lnk"

[Files]
#ifdef UseSignTool
Source: "{#MyAppSourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion signonce
Source: "{#MyAppSourceDir}\*"; Excludes: "\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#else
Source: "{#MyAppSourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Icons]
Name: "{group}\{#MyAppTitle}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Licenses and third-party notices"; Filename: "{app}\THIRD_PARTY_NOTICES.txt"
Name: "{group}\{cm:UninstallProgram,{#MyAppTitle}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppTitle}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppTitle}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Python may write __pycache__ folders into the bundle at run time.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
// ASCII-path copies of espeak-ng made for non-ASCII user names
// (core/paths.py ascii_path): <AppName>-ascii-<version>-<hash> folders,
// also VoiceCraft-ascii-* ones from before the rebrand.
procedure DeleteAsciiCopiesNamed(Base, AppName: String);
var
  FindRec: TFindRec;
begin
  if FindFirst(Base + '\' + AppName + '-ascii-*', FindRec) then
  try
    repeat
      if FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY <> 0 then
        DelTree(Base + '\' + FindRec.Name, True, True, True);
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure DeleteAsciiCopies(Base: String);
begin
  DeleteAsciiCopiesNamed(Base, '{#MyAppName}');
  DeleteAsciiCopiesNamed(Base, 'VoiceCraft');
end;

function IsOurInstall(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\{#MyAppExeName}')) or
            FileExists(ExpandConstant('{app}\{#LegacyExeName}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DeleteAsciiCopies(ExpandConstant('{commonappdata}'));
  DeleteAsciiCopies(GetEnv('PUBLIC'));
  DataDir := ExpandConstant('{localappdata}\{#MyAppName}');
  if not DirExists(DataDir) or UninstallSilent() then
    Exit;
  if MsgBox('Also delete downloaded voice models, caches and logs?' + #13#10#13#10 +
            DataDir + #13#10#13#10 +
            'Audio you saved (by default in Documents\{#MyAppName}) is not touched.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
