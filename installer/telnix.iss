; Telnix Inno Setup 安装脚本
; 安装时随机生成主进程名（进程伪装，见 ADR 0002/0003）

#define AppName "Telnix"
#define AppVersion "0.1.0"
#define AppPublisher "Telnix"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={pf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename=Telnix-setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\Telnix\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Telnix"; Filename: "{app}\Telnix_host.exe"
Name: "{group}\卸载 Telnix"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Telnix_host.exe"; Description: "立即启动 Telnix"; Flags: nowait postinstall skipifsilent

[Code]
const
  // 系统服务名变体池——用于进程伪装
  NamePool: array[0..11] of string = (
    'SystemMetrics', 'AudioEndpointSvc', 'NetworkGeoSvc', 'TelemetryForwarder',
    'DeviceSyncAgent', 'WindowsSearchHelper', 'UpdateOrchestrator', 'SecurityHealthService',
    'BiometricService', 'CloudExperienceHost', 'DiagnosticsHub', 'PowerCfgHelper'
  );

function GenerateMasqueradeName: string;
var
  idx: Integer;
begin
  Randomize;
  idx := Random(12);
  Result := NamePool[idx] + '.exe';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  masqName: string;
begin
  if CurStep = ssPostInstall then
  begin
    masqName := GenerateMasqueradeName;
    // 记录到注册表，供主进程读取
    RegWriteStringValue(HKCU, 'Software\Telnix', 'MasqueradeName', masqName);
    // TODO: 实际部署时需重命名 Telnix_host.exe 为 masqName
    // 当前 MVP 阶段仅记录名称，主进程启动时读取并设置窗口标题
  end;
end;
