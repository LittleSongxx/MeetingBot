# 部署封装（Windows PowerShell）：与 scripts/compose.sh 等价。
# 固定仓库根目录与 .env 位置，避免在别的目录执行 docker compose 时
# 找不到 .env 或 compose.yaml。
#
# 用法：
#   .\scripts\compose.ps1 up -d --build
#   .\scripts\compose.ps1 ps
#   .\scripts\compose.ps1 logs -f backend
#   .\scripts\compose.ps1 --profile test run --rm test
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ComposeArgs
)

$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $projectDir '.env'
$composeFile = Join-Path $projectDir 'compose.yaml'

if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Error "找不到 $envFile。请先复制 .env.example 为 .env 并按 DEPLOYMENT.md 填写必填项。"
    exit 1
}

& docker compose --project-directory $projectDir --env-file $envFile -f $composeFile @ComposeArgs
exit $LASTEXITCODE
