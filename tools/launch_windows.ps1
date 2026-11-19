param(
    [Parameter(Mandatory=$true)][string]$BatchRoot,
    [Parameter(Mandatory=$true)][string]$Manifest,
    [string]$Python,
    [string]$User,
    [switch]$InstallOnly,
    [switch]$MonitorOnly,
    [switch]$ShowMonitor
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$OutputEncoding = [Console]::OutputEncoding
$BatchRoot = (Resolve-Path -LiteralPath $BatchRoot).Path
if (-not $Python) { $Python = (Get-Command python.exe -CommandType Application).Source }
if (-not $User) { $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name }
$Python = (Resolve-Path -LiteralPath $Python).Path
if (-not [IO.Path]::IsPathRooted($Manifest)) { $Manifest = Join-Path $BatchRoot $Manifest }
$Manifest = (Resolve-Path -LiteralPath $Manifest).Path
$settingsDoc = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$results = Join-Path $BatchRoot $settingsDoc.results
$statePath = Join-Path $results 'campaign_state.json'
$tools = $PSScriptRoot
if ($ShowMonitor) {
    $Host.UI.RawUI.WindowTitle = "GTR $([IO.Path]::GetFileName($BatchRoot)) 实验进度"
    & $Python -X utf8 -B (Join-Path $tools 'monitor.py') --root $BatchRoot --state $statePath --instance (Join-Path $results 'monitor_instance.json')
    exit $LASTEXITCODE
}
function Quote-Arg([string]$Value) {
    if ($Value.Contains('"')) { throw 'Double quotes are unsupported in launch paths' }
    return '"' + $Value + '"'
}
$hash = [Security.Cryptography.SHA256]::Create()
$key = ($BatchRoot.ToLowerInvariant() + '|' + $Manifest.ToLowerInvariant())
$suffix = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($key)))).Replace('-','').Substring(0,12)
$controllerName = "GTR-Runner-$suffix"
$monitorName = "GTR-View-$suffix"
if ($MonitorOnly) {
    $existing = Get-ScheduledTask -TaskName $monitorName -ErrorAction Stop
    if ($existing.State -ne 'Running') { Start-ScheduledTask -TaskName $monitorName }
    @{monitor_task=$monitorName;action='monitor_only'} | ConvertTo-Json
    exit 0
}
$plan = & $Python -X utf8 -B (Join-Path $tools 'runner.py') --root $BatchRoot --manifest $Manifest --python $Python
if ($LASTEXITCODE -ne 0) { throw 'Runtime manifest validation failed' }
$planDoc = ($plan | Select-Object -Last 1) | ConvertFrom-Json
$capacity = [int]$planDoc.slot_capacity
$slotRoot = $settingsDoc.slot_root
if (-not $slotRoot) { $slotRoot = Join-Path (Split-Path -Parent $BatchRoot) '_compute_slots' }
$policyPath = Join-Path $slotRoot 'capacity.json'
if (Test-Path -LiteralPath $policyPath) {
    $policy = Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([int]$policy.capacity -ne $capacity) { throw 'Shared machine capacity differs; an explicit scheduler handover is required' }
    $capacity = [int]$policy.capacity
}
$pythonw = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "Missing pythonw.exe next to configured worker: $pythonw" }
$deployment = Join-Path $BatchRoot 'deployments'
New-Item -ItemType Directory -Force -Path $deployment | Out-Null
$recordPath = Join-Path $deployment 'launched.json'
if (Test-Path -LiteralPath $recordPath) {
    $previous = Get-Content -LiteralPath $recordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($previous.runtime -ne 'gtr-remote-runner-1.3.0') { throw 'Batch belongs to another controller; use an explicit handover' }
}
$existingController = Get-ScheduledTask -TaskName $controllerName -ErrorAction SilentlyContinue
if ($existingController -and $existingController.State -eq 'Running') { throw 'Controller is already running' }
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$controllerArgs = '-X utf8 -B ' + (Quote-Arg (Join-Path $tools 'controller_entry.py')) + ' --log-root ' + (Quote-Arg (Join-Path $results 'controllers')) + ' --root ' + (Quote-Arg $BatchRoot) + ' --manifest ' + (Quote-Arg $Manifest) + ' --python ' + (Quote-Arg $Python) + ' --execute'
$controllerAction = New-ScheduledTaskAction -Execute $pythonw -Argument $controllerArgs -WorkingDirectory $BatchRoot
Register-ScheduledTask -TaskName $controllerName -Action $controllerAction -Principal $principal -Settings $taskSettings -Description 'GTR pinned runtime controller; detached from passive console' -Force | Out-Null
$existingMonitor = Get-ScheduledTask -TaskName $monitorName -ErrorAction SilentlyContinue
if (-not $existingMonitor -or $existingMonitor.State -ne 'Running') {
    $monitorArgs = '-NoProfile -ExecutionPolicy Bypass -File ' + (Quote-Arg $PSCommandPath) + ' -BatchRoot ' + (Quote-Arg $BatchRoot) + ' -Manifest ' + (Quote-Arg $Manifest) + ' -Python ' + (Quote-Arg $Python) + ' -ShowMonitor'
    $monitorAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $monitorArgs -WorkingDirectory $BatchRoot
    Register-ScheduledTask -TaskName $monitorName -Action $monitorAction -Principal $principal -Settings $taskSettings -Description 'Passive GTR experiment console' -Force | Out-Null
}
$record = @{runtime='gtr-remote-runner-1.3.0';controller_task=$controllerName;monitor_task=$monitorName;root=$BatchRoot;manifest=$Manifest;python=$Python;state_file=$statePath;runtime_tools=$tools;user=$User;maximum_compute_slots=$capacity;maximum_supported_capacity=[int]$planDoc.maximum_supported_capacity;slot_root=$slotRoot;execution_quota=$planDoc.execution_quota;installed_only=[bool]$InstallOnly}
$json = $record | ConvertTo-Json
$temporary = $recordPath + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
[IO.File]::WriteAllText($temporary,$json,[Text.UTF8Encoding]::new($false))
if (Test-Path -LiteralPath $recordPath) { [IO.File]::Replace($temporary,$recordPath,$null) } else { [IO.File]::Move($temporary,$recordPath) }
if (-not $InstallOnly) {
    Start-ScheduledTask -TaskName $controllerName
    if ((Get-ScheduledTask -TaskName $monitorName).State -ne 'Running') { Start-ScheduledTask -TaskName $monitorName }
}
$json
