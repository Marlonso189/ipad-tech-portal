; Inno Setup 6. Builds desktop\output\ipad-tech-portal-setup.exe.
; The config file is installed only when it is missing, and technicians can read it but not change it.

#define AppName "iPad Tech Portal"
#define AppExe "ipad-tech-portal.exe"
#define AppVersion "1.1.0"

[Setup]
AppId={{A7E3C1D4-5B29-4F0E-9C6A-8D2F1E0B7A44}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ipad-tech-portal
DefaultDirName={autopf}\ipad-tech-portal
DefaultGroupName=iPad Tech Portal
OutputDir=output
OutputBaseFilename=ipad-tech-portal-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern

[Files]
Source: "..\dist\ipad-tech-portal\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "config.example.yaml"; DestDir: "{commonappdata}\ipad-tech-portal"; DestName: "config.yaml"; Permissions: users-readexec admins-full; Flags: onlyifdoesntexist uninsneveruninstall

[Dirs]
Name: "{commonappdata}\ipad-tech-portal"; Permissions: users-readexec admins-full

[Icons]
Name: "{group}\iPad Tech Portal"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\iPad Tech Portal"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
