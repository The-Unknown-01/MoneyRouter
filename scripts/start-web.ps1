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
foreach ($port in @(([int]($Listen.Split(':')[-1])),8090)) {
 $probe=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,$port)
 $probe.Server.ExclusiveAddressUse=$true
 try {
  $probe.Start()
 } catch {
  throw "Cannot bind port ${port}: $($_.Exception.GetBaseException().Message). Run: netstat -ano | findstr :$port"
 } finally { $probe.Stop() }
}
Push-Location $projectRoot
try {
 New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'bin') | Out-Null
 go build -o bin/moneyrouter-web.exe .
 if ($LASTEXITCODE -ne 0) { throw 'Go 构建失败' }
 $agentProcess=Start-Process -FilePath $pythonPath -ArgumentList '-m','uvicorn','moneyrouter_agent.api.server:app','--host','127.0.0.1','--port','8090' -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'agent-service.log') -RedirectStandardError (Join-Path $projectRoot 'agent-service-error.log')
 try {
  $ready=$false
  for($attempt=0;$attempt -lt 30;$attempt++) {
   if($agentProcess.HasExited){throw 'Python 服务启动失败，检查 agent-service-error.log'}
   try { $null=Invoke-RestMethod 'http://127.0.0.1:8090/readyz' -TimeoutSec 2 -Headers @{Authorization="Bearer $env:AGENT_SERVICE_TOKEN"};$ready=$true;break } catch { Start-Sleep -Milliseconds 500 }
  }
  if(!$ready){throw 'Python 服务未就绪'}
  Write-Host "稳序已启动：http://$Listen"
  & ./bin/moneyrouter-web.exe -addr $Listen -data data-web/go
  if ($LASTEXITCODE -ne 0) { throw 'Go web service exited with an error.' }
 } finally { if(!$agentProcess.HasExited){Stop-Process -Id $agentProcess.Id} }
} finally { Pop-Location }
