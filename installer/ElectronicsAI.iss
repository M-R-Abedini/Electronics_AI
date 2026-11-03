; Bundled Python dependencies retain their own licenses. User data is never uninstalled.
#ifndef SourceRoot
  #error SourceRoot must point to a verified build_installer.py stage
#endif
#ifndef OutputRoot
  #error OutputRoot is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
[Setup]
AppId={{E26155B5-B44D-43F9-9919-B75582132788}
AppName=Electronics AI
AppVersion={#AppVersion}
AppPublisher=Electronics AI contributors
AppPublisherURL=https://github.com/M-R-Abedini/Electronics_AI
DefaultDirName={localappdata}\Programs\ElectronicsAI
DefaultGroupName=Electronics AI
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputRoot}
OutputBaseFilename=ElectronicsAI-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile={#SourceRoot}\THIRD_PARTY_NOTICES.md
UninstallDisplayIcon={app}\python\python.exe
CloseApplications=no
RestartApplications=no

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Set up Electronics AI"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\installer\onboarding.ps1"""; WorkingDir: "{app}"
Name: "{group}\Third-party notices"; Filename: "{app}\THIRD_PARTY_NOTICES.md"
Name: "{group}\Uninstall Electronics AI"; Filename: "{uninstallexe}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\installer\onboarding.ps1"""; Description: "Set up Altium and your AI client"; Flags: postinstall nowait skipifsilent
