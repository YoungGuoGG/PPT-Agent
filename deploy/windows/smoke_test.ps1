$ErrorActionPreference = "Stop"
$Sidecar = Join-Path $PSScriptRoot "dist\ppt-agent-v2-sidecar\ppt-agent-v2-sidecar.exe"
if (-not (Test-Path -LiteralPath $Sidecar)) {
  throw "Sidecar not found: $Sidecar"
}

$PromptDirectory = Join-Path $PSScriptRoot "dist\ppt-agent-v2-sidecar\_internal\ppt_master_service\prompts"
$RequiredPrompts = @(
  "brand_style_prompt.md",
  "executor_prompt.md",
  "reviewer_prompt.md",
  "revision_prompt.md",
  "router_prompt.md",
  "strategist_prompt.md",
  "svg_repair_patch_prompt.md",
  "template_fill_prompt.md",
  "topic_research_prompt.md"
)
foreach ($Prompt in $RequiredPrompts) {
  $PromptPath = Join-Path $PromptDirectory $Prompt
  if (-not (Test-Path -LiteralPath $PromptPath -PathType Leaf)) {
    throw "Packaged service prompt not found: $PromptPath"
  }
}
$RuntimeRoot = Join-Path $PSScriptRoot "dist\ppt-agent-v2-sidecar\_internal"
$RequiredResources = @(
  "skills\ppt-agent-planner\SKILL.md",
  "skills\ppt-design\SKILL.md",
  "vendor\ppt_master_runtime\workflows\registry.json"
)
foreach ($RelativePath in $RequiredResources) {
  $ResourcePath = Join-Path $RuntimeRoot $RelativePath
  if (-not (Test-Path -LiteralPath $ResourcePath -PathType Leaf)) {
    throw "Packaged V2 resource not found: $ResourcePath"
  }
}
$ProjectManager = Join-Path $RuntimeRoot "vendor\ppt_master_runtime\skills\ppt-master\scripts\project_manager.py"
$ToolSmokeRoot = Join-Path $PSScriptRoot "smoke-data-v2\tool-dispatch"
$ToolProjectName = "dispatch-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $ToolSmokeRoot -Force | Out-Null
$BeforeToolProjects = @(Get-ChildItem -LiteralPath $ToolSmokeRoot -Directory | ForEach-Object { $_.FullName })
$ToolProcess = Start-Process -FilePath $Sidecar -ArgumentList @(
  $ProjectManager, "init", $ToolProjectName, "--format", "ppt169", "--dir", $ToolSmokeRoot
) -PassThru -Wait -WindowStyle Hidden
$NewToolProjects = @(Get-ChildItem -LiteralPath $ToolSmokeRoot -Directory | Where-Object { $_.FullName -notin $BeforeToolProjects })
if ($ToolProcess.ExitCode -ne 0 -or $NewToolProjects.Count -ne 1) {
  throw "Packaged sidecar could not dispatch an approved bundled tool"
}
Remove-Item -LiteralPath $NewToolProjects[0].FullName -Recurse -Force

$Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$Listener.Start()
$Port = ([System.Net.IPEndPoint]$Listener.LocalEndpoint).Port
$Listener.Stop()
$Token = [guid]::NewGuid().ToString("N")
$Environment = @{
  PPT_AGENT_PORT = [string]$Port
  PPT_AGENT_AUTH_TOKEN = $Token
  PPT_AGENT_DATA_ROOT = (Join-Path $PSScriptRoot "smoke-data-v2")
}
foreach ($Entry in $Environment.GetEnumerator()) {
  [System.Environment]::SetEnvironmentVariable($Entry.Key, $Entry.Value, "Process")
}
$Process = Start-Process -FilePath $Sidecar -PassThru -WindowStyle Hidden
try {
  $Ready = $false
  foreach ($Attempt in 1..60) {
    try {
      $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Headers @{ "X-PPT-Agent-Token" = $Token } -TimeoutSec 2
      if ($Health.ok) { $Ready = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 500
  }
  if (-not $Ready) { throw "Sidecar health check did not become ready" }
  $Preflight = Invoke-WebRequest -UseBasicParsing -Method Options -Uri "http://127.0.0.1:$Port/health" -Headers @{
    Origin = "http://tauri.localhost"
    "Access-Control-Request-Method" = "GET"
    "Access-Control-Request-Headers" = "x-ppt-agent-token"
  } -TimeoutSec 10
  if ($Preflight.StatusCode -ne 200 -or $Preflight.Headers["Access-Control-Allow-Origin"] -ne "http://tauri.localhost") {
    throw "Packaged sidecar rejected the Windows Tauri CORS preflight"
  }
  $Workflows = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/workflows" -Headers @{ "X-PPT-Agent-Token" = $Token } -TimeoutSec 10
  $Executable = @($Workflows | Where-Object { $_.executable } | ForEach-Object { $_.id } | Sort-Object)
  $Expected = @("document_to_ppt", "image_heavy", "mirror_template", "template_guided_svg")
  if (($Executable -join ',') -ne ($Expected -join ',')) {
    throw "Unexpected packaged workflow capabilities: $($Executable -join ',')"
  }
  Write-Host "Sidecar smoke test passed."
} finally {
  Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
}
