#define ProductName "Zhunt"
#define ProductVersion "0.1.0"
#define Publisher "Kindred Wildcat"
#define ExecutableName "zhunt.exe"

[Setup]
AppId={{B5AF4F37-7E5A-4D62-8F3C-3C0F7B474E79}
AppName={#ProductName}
AppVersion={#ProductVersion}
AppPublisher={#Publisher}
DefaultDirName={localappdata}\Programs\Zhunt
DefaultGroupName={#ProductName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=Zhunt-Setup-win-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
Uninstallable=yes

[Files]
Source: "..\..\.windows-build\dist\zhunt\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Zhunt Setup"; Filename: "{app}\{#ExecutableName}"; Parameters: "setup"
Name: "{group}\Zhunt Daemon"; Filename: "{app}\{#ExecutableName}"; Parameters: "serve"
Name: "{group}\Uninstall Zhunt"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\{#ExecutableName}"; Parameters: "--help"; Description: "Show Zhunt command help"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\{#ExecutableName}"; Parameters: "uninstall --all"; Flags: runhidden waituntilterminated

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
