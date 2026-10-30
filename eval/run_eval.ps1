# 评测辅助脚本：在隔离评测栈上跑离线指标（不调用模型）。
# 用法：pwsh ./eval/run_eval.ps1 [--meeting-id 1] [--limit 20]
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $PassThroughArgs)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $repo "eval\compose.eval.yaml"
$envFile = Join-Path $repo "eval\.env"
if (-not (Test-Path $envFile)) {
  throw "缺少 eval/.env：请先 cp eval/.env.example eval/.env 并填写 Key/口令"
}
$argsList = @(
  "compose", "--project-directory", (Join-Path $repo "eval"),
  "--env-file", $envFile, "-f", $compose,
  "exec", "-T", "backend", "python", "-m", "evaluation.run_eval"
)
$argsList += $PassThroughArgs
& docker @argsList
exit $LASTEXITCODE
