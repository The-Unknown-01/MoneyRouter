param([string]$Listen='127.0.0.1:8080', [int]$AgentPort=8090)
$ErrorActionPreference='Stop'
$projectRoot=Split-Path $PSScriptRoot -Parent
$binDir=Join-Path $projectRoot 'bin'
$webPort=[int]($Listen.Split(':')[-1])
$ports=@($webPort,$AgentPort) | Select-Object -Unique

# 只按本项目的进程特征识别，不按端口盲杀，避免误伤恰好占用 8080/8090 的其他程序。
function Test-MoneyRouterProcess {
 [CmdletBinding()]
 param([string]$Name, [string]$ExecutablePath, [string]$CommandLine)
 if ($Name -eq 'moneyrouter-web.exe') { return [bool]($ExecutablePath -like "$binDir*") }
 if ($Name -eq 'python.exe') { return [bool]($CommandLine -like '*moneyrouter_agent*') }
 return $false
}

$found=@{}
foreach ($process in (@(Get-CimInstance Win32_Process -Filter "Name='moneyrouter-web.exe'" -ErrorAction SilentlyContinue) + @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue))) {
 $owner=[int]$process.ProcessId
 if ($owner -le 4) { continue }
 if (Test-MoneyRouterProcess -Name $process.Name -ExecutablePath $process.ExecutablePath -CommandLine $process.CommandLine) {
  if (!$found.ContainsKey($owner)) { $found[$owner]=$process.Name }
 }
}

$foreign=New-Object System.Collections.Generic.List[string]
foreach ($port in $ports) {
 $owners=@(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
 foreach ($owner in $owners) {
  if ($owner -le 4 -or $found.ContainsKey([int]$owner)) { continue }
  $proc=Get-Process -Id $owner -ErrorAction SilentlyContinue
  $name=if ($proc) { $proc.ProcessName } else { '未知进程' }
  $foreign.Add("端口 $port 被非本项目进程占用：$name（PID $owner），未处理。")
 }
}

if ($found.Count -eq 0) {
 Write-Host '未发现运行中的薪安理得服务。'
} else {
 Write-Host "发现 $($found.Count) 个薪安理得服务进程，正在停止……"
 foreach ($owner in @($found.Keys)) {
  $name=$found[$owner]
  try {
   Stop-Process -Id $owner -Force -ErrorAction Stop
   Write-Host "已停止 $name（PID $owner）"
  } catch {
   Write-Host "停止 $name（PID $owner）失败：$($_.Exception.Message)"
  }
 }
 Start-Sleep -Milliseconds 500
}

$left=New-Object System.Collections.Generic.List[string]
foreach ($owner in @($found.Keys)) {
 if (Get-Process -Id $owner -ErrorAction SilentlyContinue) { $left.Add("PID $owner") }
}
if ($left.Count -gt 0) {
 Write-Host ('未能停止：' + ($left -join '、') + '。可再执行一次，或检查是否以管理员权限运行。')
 exit 1
}
if ($found.Count -gt 0) { Write-Host '网页服务与 Python 服务已停止。' }

$busy=New-Object System.Collections.Generic.List[string]
foreach ($port in $ports) {
 if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { $busy.Add([string]$port) }
}
foreach ($line in $foreign) { Write-Host $line }
if ($busy.Count -gt 0) {
 Write-Host "端口 $($busy -join '、') 仍在监听，占用进程见上；本项目已无进程在运行。"
 exit 1
}
exit 0
