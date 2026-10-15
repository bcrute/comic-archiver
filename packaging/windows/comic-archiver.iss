; The installer for Comic Archiver on Windows. Built by scripts/build_windows_app.py --installer, which gives:
;   AppVersion       the version, as in pyproject.toml
;   SourceDir        the app folder that was built
;   OutputDir        where the installer is written
;   OutputName       its file name without .exe
;   StartParameters  what follows pythonw.exe to start the app
;
; It installs for the person running it, into their own programs folder, so it never asks for an
; administrator. Uninstalling removes the app only: the saved library and the sign-in are left alone.

#ifndef AppVersion
  #error AppVersion is not given; build with scripts/build_windows_app.py --installer
#endif
#define AppName "Comic Archiver"
#define AppProgram "{app}\python\pythonw.exe"
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
