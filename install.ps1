# ccsm installer.  .\install.ps1   |   .\install.ps1 -Uninstall
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = @("python", "python3", "py") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $py) { Write-Error "ccsm: needs Python 3.9+ on PATH"; exit 1 }

if ($Uninstall) {
  if (Get-Command ccsm -ErrorAction SilentlyContinue) { ccsm uninstall --yes }
  Remove-Item -Recurse -Force (Join-Path $HOME ".claude\skills\ccsm") -ErrorAction SilentlyContinue
  if (Get-Command pipx -ErrorAction SilentlyContinue) { pipx uninstall ccsm }
  else { & $py -m pip uninstall -y ccsm }
  Write-Host "ccsm removed."
  exit 0
}

if (Get-Command pipx -ErrorAction SilentlyContinue) { pipx install --force . }
else { & $py -m pip install --user --upgrade . }

# Install the /ccsm slash command for every session, not just this directory.
$skills = Join-Path $HOME ".claude\skills\ccsm"
if (Test-Path ".claude\skills\ccsm\SKILL.md") {
  New-Item -ItemType Directory -Force $skills | Out-Null
  Copy-Item ".claude\skills\ccsm\SKILL.md" (Join-Path $skills "SKILL.md") -Force
  Write-Host "Installed the /ccsm skill to $skills"
}

Write-Host ""
if (Get-Command ccsm -ErrorAction SilentlyContinue) {
  Write-Host "Installed. Run: ccsm"
} else {
  $scripts = & $py -c "import sysconfig;print(sysconfig.get_path('scripts',sysconfig.get_preferred_scheme('user')))"
  Write-Host "Installed, but 'ccsm' is not on PATH yet."
  Write-Host "  run it now:      $scripts\ccsm.exe"
  Write-Host "  or add to PATH:  $scripts"
}
