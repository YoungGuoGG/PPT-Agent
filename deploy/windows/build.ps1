param(
  [switch]$SkipInstall,
  [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Ui = Join-Path $Root "apps\ppt-agent-ui"
$Desktop = Join-Path $Root "apps\ppt-agent-desktop"
$Dist = Join-Path $PSScriptRoot "dist"
$Build = Join-Path $PSScriptRoot "build"

if (-not $SkipInstall) {
  python -m pip install -e $Root -r (Join-Path $Root "vendor\ppt_master_runtime\skills\ppt-master\requirements.txt") -r (Join-Path $PSScriptRoot "requirements-build.txt")
  npm --prefix $Ui install
  npm --prefix $Desktop install
}

New-Item -ItemType Directory -Path $Build -Force | Out-Null
python -m PyInstaller --clean --noconfirm --distpath $Dist --workpath $Build (Join-Path $PSScriptRoot "ppt-agent-sidecar.spec")
$SidecarDirectory = Join-Path $Dist "ppt-agent-v2-sidecar"
$SidecarArchive = Join-Path $Dist "ppt-agent-v2-sidecar.zip"
if (Test-Path -LiteralPath $SidecarArchive) {
  Remove-Item -LiteralPath $SidecarArchive -Force
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
  $SidecarDirectory,
  $SidecarArchive,
  [System.IO.Compression.CompressionLevel]::Optimal,
  $true
)
$RuntimeZip = [System.IO.Compression.ZipFile]::OpenRead($SidecarArchive)
try {
  $SidecarEntry = $RuntimeZip.Entries | Where-Object {
    $_.FullName.Replace('\', '/') -eq 'ppt-agent-v2-sidecar/ppt-agent-v2-sidecar.exe'
  } | Select-Object -First 1
  if (-not $SidecarEntry -or $RuntimeZip.Entries.Count -lt 2) {
    throw "Bundled runtime archive is incomplete: $SidecarArchive"
  }
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
    $ExpectedEntry = "ppt-agent-v2-sidecar/_internal/ppt_master_service/prompts/$Prompt"
    $PromptEntry = $RuntimeZip.Entries | Where-Object {
      $_.FullName.Replace('\', '/') -eq $ExpectedEntry
    } | Select-Object -First 1
    if (-not $PromptEntry) {
      throw "Bundled runtime archive is missing service prompt: $ExpectedEntry"
    }
  }
  $RequiredResources = @(
    "ppt-agent-v2-sidecar/_internal/skills/ppt-agent-planner/SKILL.md",
    "ppt-agent-v2-sidecar/_internal/skills/ppt-design/SKILL.md",
    "ppt-agent-v2-sidecar/_internal/skills/ppt-style-extractor/SKILL.md",
    "ppt-agent-v2-sidecar/_internal/style-library/neutral-professional/design.md",
    "ppt-agent-v2-sidecar/_internal/vendor/ppt_master_runtime/workflows/registry.json"
  )
  foreach ($ExpectedEntry in $RequiredResources) {
    $ResourceEntry = $RuntimeZip.Entries | Where-Object {
      $_.FullName.Replace('\', '/') -eq $ExpectedEntry
    } | Select-Object -First 1
    if (-not $ResourceEntry) {
      throw "Bundled runtime archive is missing V2 resource: $ExpectedEntry"
    }
  }
} finally {
  $RuntimeZip.Dispose()
}
npm --prefix $Ui run build

if (-not $SkipInstaller) {
  npm --prefix $Desktop run build
}

Write-Host "PPT Agent Windows build complete."
Write-Host "Sidecar: $(Join-Path $Dist 'ppt-agent-v2-sidecar\ppt-agent-v2-sidecar.exe')"
Write-Host "Bundled runtime: $SidecarArchive"
Write-Host "Installers: $(Join-Path $Desktop 'src-tauri\target\release\bundle')"
