# Mirror your sc-hub dashboard to this Windows laptop and keep it fresh.
# Needs only the built-in OpenSSH client and a browser.
#   .\schub-view.ps1            refresh every 60 s (Ctrl-C to stop)
#   .\schub-view.ps1 -Once      refresh once and open
param([switch]$Once)
$ErrorActionPreference = 'Stop'
$Alias = if ($env:SCHUB_ALIAS) { $env:SCHUB_ALIAS } else { 'mbzuai-schub' }
$Dest = if ($env:SCHUB_VIEW_DIR) { $env:SCHUB_VIEW_DIR } else { Join-Path $HOME 'sc-hub-view' }
$Every = [Math]::Max(30, [int]($(if ($env:SCHUB_VIEW_EVERY) { $env:SCHUB_VIEW_EVERY } else { 60 })))
$Marker = Join-Path $Dest '.schub-view'

# Only ever replace a folder this script created.
if ((Test-Path $Dest) -and -not (Test-Path $Marker) -and (Get-ChildItem $Dest -Force | Select-Object -First 1)) {
    throw "$Dest exists and is not an sc-hub view folder; set SCHUB_VIEW_DIR to an empty folder"
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
New-Item -ItemType File -Force -Path $Marker | Out-Null

function Update-View {
    ssh -o BatchMode=yes $Alias 'schub/bin/schub dashboard >/dev/null'
    if ($LASTEXITCODE -ne 0) { Write-Host "refresh failed at $(Get-Date -Format HH:mm); will retry"; return }
    # scp copies files as bytes (PowerShell 5 pipes would re-encode binary data).
    Get-ChildItem $Dest -Force | Where-Object { $_.Name -ne '.schub-view' } | Remove-Item -Recurse -Force
    scp -q -r -o BatchMode=yes "${Alias}:schub/view/*" $Dest
}

Update-View
Start-Process (Join-Path $Dest 'index.html')
if ($Once) { return }
Write-Host "sc-hub view: $Dest\index.html (refreshing every ${Every}s, Ctrl-C to stop)"
while ($true) { Start-Sleep -Seconds $Every; Update-View }
