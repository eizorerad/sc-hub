# Mirror your sc-hub dashboard to this Windows laptop and keep it fresh.
# Needs only the built-in OpenSSH client and a browser.
#   .\schub-view.cmd            refresh every 60 s (Ctrl-C to stop)
#   .\schub-view.cmd -Once      refresh once and open
# (the .cmd wrapper runs this script even where PowerShell scripts are blocked)
param([switch]$Once)
$ErrorActionPreference = 'Stop'
$Alias = if ($env:SCHUB_ALIAS) { $env:SCHUB_ALIAS } else { 'mbzuai-schub' }
# No trailing slash: the download folder must sit next to the view, not inside it.
$Dest = "$(if ($env:SCHUB_VIEW_DIR) { $env:SCHUB_VIEW_DIR } else { Join-Path $HOME 'sc-hub-view' })".TrimEnd('\', '/')
$Every = [Math]::Max(30, [int]($(if ($env:SCHUB_VIEW_EVERY) { $env:SCHUB_VIEW_EVERY } else { 60 })))
$Marker = Join-Path $Dest '.schub-view'
$Incoming = "$Dest.incoming"
$ImageSet = ''

# Only ever replace a folder this script created.
if ((Test-Path $Dest) -and -not (Test-Path $Marker) -and (Get-ChildItem $Dest -Force | Select-Object -First 1)) {
    throw "$Dest exists and is not an sc-hub view folder; set SCHUB_VIEW_DIR to an empty folder"
}
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
New-Item -ItemType File -Force -Path $Marker | Out-Null

function Invoke-Native([scriptblock]$Command) {
    # Judge native tools by exit code: in PowerShell 5.1 their stderr would throw.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

function Say($Text) { Write-Host "$Text at $(Get-Date -Format HH:mm); will retry" }

function Update-View {
    # Rebuild on the cluster and list its images: the page (~100 KB) is fetched
    # every time, the images (most of the bytes) only when that list changes.
    $list = 'schub/bin/schub dashboard >/dev/null && cd schub/view && find img -type f -printf ''%p %s %T@\n'' 2>/dev/null | sort | cksum'
    $images = (Invoke-Native { ssh -o BatchMode=yes $Alias $list } | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $images) { Say 'refresh failed'; return }
    $all = $images -ne $script:ImageSet -or -not (Test-Path (Join-Path $Dest 'img'))
    # Download next to the view, then swap: the open page never sees a half copy.
    # scp copies files as bytes (PowerShell 5 pipes would re-encode binary data).
    try {
        if (Test-Path $Incoming) { Remove-Item -Recurse -Force $Incoming }
        New-Item -ItemType Directory -Path $Incoming | Out-Null
        $source = if ($all) { "${Alias}:schub/view/*" } else { "${Alias}:schub/view/index.html" }
        Invoke-Native { scp -q -r -o BatchMode=yes $source $Incoming }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $Incoming 'index.html'))) { Say 'download failed'; return }
        if ($all) {
            # Images first, the page last: an auto-reload in between still finds a page.
            Get-ChildItem $Dest -Force | Where-Object { $_.Name -notin '.schub-view', 'index.html' } | Remove-Item -Recurse -Force
            Get-ChildItem $Incoming -Force | Where-Object { $_.Name -ne 'index.html' } | Move-Item -Destination $Dest
        }
        Move-Item -Force (Join-Path $Incoming 'index.html') (Join-Path $Dest 'index.html')
        Remove-Item -Recurse -Force $Incoming
        if ($all) { $script:ImageSet = $images }
    } catch {
        # A file briefly locked (antivirus, indexer, Explorer): keep the loop alive.
        Say "update failed ($($_.Exception.Message))"
    }
}

Update-View
Start-Process (Join-Path $Dest 'index.html')
if ($Once) { return }
Write-Host "sc-hub view: $Dest\index.html (refreshing every ${Every}s, Ctrl-C to stop)"
while ($true) { Start-Sleep -Seconds $Every; Update-View }
