param([string]$DataRoot='./data-web', [Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference='Stop'
$source=(Resolve-Path -LiteralPath $DataRoot).Path
$target=[IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) { throw '备份目标已存在，不覆盖。' }
if ($target.StartsWith($source+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw '备份目标不能位于数据目录内。' }
if (Get-NetTCPConnection -State Listen -LocalPort 8080,8090 -ErrorAction SilentlyContinue) { throw '请先停止 Go 与 Python 两个服务。' }
if (!(Test-Path -LiteralPath (Join-Path $source 'go/finance.db')) -or !(Test-Path -LiteralPath (Join-Path $source 'agent/service.sqlite'))) { throw '未找到完整双服务数据目录。' }
New-Item -ItemType Directory -Path $target | Out-Null
Copy-Item -LiteralPath $source -Destination (Join-Path $target 'data') -Recurse
$copied=Join-Path $target 'data'
$files=Get-ChildItem -LiteralPath $copied -File -Recurse | ForEach-Object { @{path=[IO.Path]::GetRelativePath($copied,$_.FullName);sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash} }
@{created_at=[DateTimeOffset]::UtcNow.ToString('o');contract_version='1';files=@($files)} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $target 'manifest.json') -Encoding utf8
Write-Host "完整备份已写入 $target"
