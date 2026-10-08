param([switch]$Offline, [string]$Listen='127.0.0.1:8080')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path $PSScriptRoot -Parent
$pythonPath=Join-Path $projectRoot 'agent/.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $pythonPath)) { throw '请先按 README 创建 agent/.venv 并安装依赖。' }
if (!(Test-Path -LiteralPath (Join-Path $projectRoot 'web/static/ui.css'))) { throw '请先运行 pnpm install 与 pnpm build。' }
if (!(Get-Command go -ErrorAction SilentlyContinue)) { throw 'Go is not installed or is missing from PATH.' }
if (!$env:AGENT_SERVICE_TOKEN) {
 $tokenBytes=New-Object byte[] 32
 $tokenGenerator=[Security.Cryptography.RandomNumberGenerator]::Create()
 try { $tokenGenerator.GetBytes($tokenBytes) } finally { $tokenGenerator.Dispose() }
 $env:AGENT_SERVICE_TOKEN=[BitConverter]::ToString($tokenBytes).Replace('-','')
}
$env:AGENT_SERVICE_URL='http://127.0.0.1:8090'
$env:MONEYROUTER_SERVICE_DIR=Join-Path $projectRoot 'data-web/agent'
$env:MARKET_DATA_CACHE_DIR=Join-Path $projectRoot 'data-web/market-cache'
$env:PYTHONPATH=Join-Path $projectRoot 'agent/src'
if ($Offline) { $env:MONEYROUTER_OFFLINE='1' } else { Remove-Item Env:MONEYROUTER_OFFLINE -ErrorAction SilentlyContinue }
if (!$Offline) {
 & $pythonPath -c "from moneyrouter_agent.config import Settings; Settings.from_env().require_api_key()"
 if ($LASTEXITCODE -ne 0) { throw 'DeepSeek API key configuration is missing. Set DEEPSEEK_API_KEY or .env/deepseek_api.key.' }
}
$webPort=[int]($Listen.Split(':')[-1])
if ($webPort -eq 8090) { throw '网页端口不能与 Python Agent 的 8090 相同。' }
foreach ($port in @($webPort,8090)) {
 $probe=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$port)
 $probe.Server.ExclusiveAddressUse=$true
 try {
  $probe.Start()
 } catch {
  throw "Cannot bind port ${port}: $($_.Exception.GetBaseException().Message). 若为上次未退出的残留服务，请先运行 stop.cmd。查看占用：netstat -ano | findstr :$port"
 } finally { $probe.Stop() }
}

# 受管作业对象：本脚本所在进程一结束（正常退出、按 Ctrl+C、关闭窗口、被强杀），
# 系统都会连带结束作业内的子进程，不会把 Python / Go 服务留在后台。
. (Join-Path $PSScriptRoot 'job-object.ps1')
$jobHandle=New-KillOnCloseJob
$agentProcess=$null
$webProcess=$null
$userQuit=$false
Push-Location $projectRoot
try {
 New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'bin') | Out-Null
 go build -o bin/moneyrouter-web.exe .
 if ($LASTEXITCODE -ne 0) { throw 'Go 构建失败' }
 $agentProcess=Start-Process -FilePath $pythonPath -ArgumentList '-m','uvicorn','moneyrouter_agent.api.server:app','--host','127.0.0.1','--port','8090' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'agent-service.log') -RedirectStandardError (Join-Path $projectRoot 'agent-service-error.log')
 $null=Add-ProcessToJobObject -Process $agentProcess -JobHandle $jobHandle -Label 'Python 服务'
 try {
  $ready=$false
  for($attempt=0;$attempt -lt 30;$attempt++) {
   if($agentProcess.HasExited){throw 'Python 服务启动失败，检查 agent-service-error.log'}
   try { $null=Invoke-RestMethod 'http://127.0.0.1:8090/readyz' -TimeoutSec 2 -Headers @{Authorization="Bearer $env:AGENT_SERVICE_TOKEN"};$ready=$true;break } catch { Start-Sleep -Milliseconds 500 }
  }
  if(!$ready){throw 'Python 服务未就绪'}
  $webProcess=Start-Process -FilePath (Join-Path $projectRoot 'bin/moneyrouter-web.exe') -ArgumentList '-addr',$Listen,'-data','data-web/go' -WorkingDirectory $projectRoot -NoNewWindow -PassThru
  $null=Add-ProcessToJobObject -Process $webProcess -JobHandle $jobHandle -Label 'Go 网页服务'
  # 接管 Ctrl+C：交给下面的按键循环统一处理，避免半途中断留下未清理的子进程。
  $keyQuit=$false
  $controlCReclaimed=$false
  try {
   $null=[Console]::KeyAvailable
   $controlCReclaimed=$true
   [Console]::TreatControlCAsInput=$true
   $keyQuit=$true
  } catch {
   $controlCReclaimed=$false
   $keyQuit=$false
   try { [Console]::TreatControlCAsInput=$false } catch { }
  }
  Write-Host "稳序已启动：http://$Listen"
  if($keyQuit){ Write-Host '按 Q / Esc / Ctrl+C 停止服务；直接关闭本窗口也会同时停止两个进程。' }
  else { Write-Host '直接关闭本窗口会同时停止两个进程，也可运行 stop.cmd 停止。' }
  while($true){
   if($webProcess.WaitForExit(200)){ break }
   if($keyQuit){
    try {
     if([Console]::KeyAvailable){
      $key=[Console]::ReadKey($true)
      $isCtrlC=($key.Key -eq [ConsoleKey]::C -and ($key.Modifiers -band [ConsoleModifiers]::Control)) -or ([int]$key.KeyChar -eq 3)
      if($key.Key -eq [ConsoleKey]::Q -or $key.Key -eq [ConsoleKey]::Escape -or $isCtrlC){ $userQuit=$true; break }
     }
    } catch { $keyQuit=$false }
   }
  }
  if($userQuit){ Write-Host '正在停止服务…' }
  elseif($webProcess.ExitCode -ne 0){ throw "Go web service exited with code $($webProcess.ExitCode). 若刚执行过 stop.cmd，可忽略此提示。" }
  else { Write-Host 'Go 网页服务已退出，正在停止 Python 服务。' }
 } finally {
  if($controlCReclaimed){ try { [Console]::TreatControlCAsInput=$false } catch { } }
  if($webProcess -and !$webProcess.HasExited){ Stop-Process -Id $webProcess.Id -Force -ErrorAction SilentlyContinue }
  if($agentProcess -and !$agentProcess.HasExited){ Stop-Process -Id $agentProcess.Id -Force -ErrorAction SilentlyContinue }
 }
} finally {
 Pop-Location
 Close-JobObject -JobHandle $jobHandle
 if($userQuit){ Write-Host "已停止：网页服务 $Listen 与 Python 服务 8090。" }
}
