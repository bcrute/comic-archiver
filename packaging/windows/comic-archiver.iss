; The installer for Comic Archiver on Windows. Built by scripts/build_windows_app.py --installer, which gives:
;   AppVersion       the version, as in pyproject.toml
;   SourceDir        the app folder that was built
;   OutputDir        where the installer is written
;   OutputName       its file name without .exe
;   StartParameters  what follows pythonw.exe to start the app
;
; It installs for the person running it, into their own programs folder, so it never asks for an
; administrator. Uninstalling first has the app forget every sign-in and cloud key it saved, and remove its own
; settings; saved episodes stay. While the app is open, nothing is uninstalled. Installing over an open
; app has it close first, as its X does.

#ifndef AppVersion
  #error AppVersion is not given; build with scripts/build_windows_app.py --installer
#endif
#define AppName "Comic Archiver"
#define AppProgram "{app}\python\pythonw.exe"
#define UninstallParameters StartParameters + " --uninstall --quiet"
#define CloseParameters StartParameters + " --close-for-update"
; The same name as in comic_archive/desktop_app.py, so the taskbar keeps the window with its shortcut.
#define AppModelId "ComicArchiver.Desktop"

[Setup]
AppId={{75E4664D-7B1B-4341-8E25-27EF0C9112C3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\{#AppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
SetupIconFile={#SourceDir}\comic-archiver.ico
UninstallDisplayIcon={app}\comic-archiver.ico
UninstallDisplayName={#AppName}
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An earlier version's Python and packages, so no file of it is left among the new ones.
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\licenses"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{#AppProgram}"; Parameters: "{#StartParameters}"; WorkingDir: "{app}"; IconFilename: "{app}\comic-archiver.ico"; AppUserModelID: "{#AppModelId}"
Name: "{autodesktop}\{#AppName}"; Filename: "{#AppProgram}"; Parameters: "{#StartParameters}"; WorkingDir: "{app}"; IconFilename: "{app}\comic-archiver.ico"; AppUserModelID: "{#AppModelId}"; Tasks: desktopicon

[Run]
Filename: "{#AppProgram}"; Parameters: "{#StartParameters}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Files Python writes beside its modules after the install.
Type: filesandordirs; Name: "{app}\python"

[Code]
// Whether a program in the app's folder is running: the window, its downloader or its browser engine, which keep
// files there in use. Windows' own check for files in use (CloseApplications) follows either way.
function AppOpen(): Boolean;
var
  Locator, Service, Found, Path: Variant;
  Folder, Text: String;
  I: Integer;
begin
  Result := False;
  Folder := Lowercase(AddBackslash(ExpandConstant('{app}')));
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Found := Service.ExecQuery('SELECT ExecutablePath FROM Win32_Process');
    for I := 0 to Found.Count - 1 do
    begin
      Path := Found.ItemIndex(I).ExecutablePath;
      if not VarIsNull(Path) then
      begin
        Text := Path;
        if Pos(Folder, Lowercase(Text)) = 1 then
        begin
          Result := True;
          Exit;
        end;
      end;
    end;
  except
    Result := False;
  end;
end;

// Before any file is replaced: an open window of the installed version closes itself when asked
// (desktop_app.close_for_update), pausing a download as its X does. One from before that request existed does
// not answer, so the person closes it.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{#AppProgram}')) then
    Exec(ExpandConstant('{#AppProgram}'), '{#CloseParameters}', ExpandConstant('{app}'), SW_HIDE,
         ewWaitUntilTerminated, Code);
  while AppOpen() do
    if SuppressibleMsgBox('Comic Archiver is open. Close it, then click Retry.', mbError, MB_RETRYCANCEL,
                          IDCANCEL) = IDCANCEL then
    begin
      Result := 'Comic Archiver is open. Close it, then run this setup again.';
      Exit;
    end;
end;

// The app's own uninstall step (desktop_app.uninstall), before any file goes: 0 done, 3 the app is open.
function InitializeUninstall(): Boolean;
var
  Code: Integer;
begin
  if not Exec(ExpandConstant('{#AppProgram}'), '{#UninstallParameters}', ExpandConstant('{app}'), SW_HIDE,
              ewWaitUntilTerminated, Code) then
    Code := -1;
  Result := Code = 0;
  if Code = 3 then
    SuppressibleMsgBox('Comic Archiver is open. Close it, then uninstall again.', mbError, MB_OK, IDOK)
  else if not Result then
    Result := SuppressibleMsgBox('Comic Archiver could not remove everything it keeps for signing in. ' +
      'Restart your computer, then uninstall again.' + #13#10#13#10 + 'Uninstall anyway?',
      mbError, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES;
end;
