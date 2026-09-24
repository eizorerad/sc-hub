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
# The download lands next to the view under a name without spaces (cmd writes it; see Update-View).
$Work = Split-Path -Parent $Dest
$ArchiveName = '.sc-hub-view.tar'  # (PowerShell names ignore case: no other $archive* may exist)
$HeavySum = ''

# Only ever replace a folder this script created.
if ((Test-Path $Dest) -and -not (Test-Path $Marker) -and (Get-ChildItem $Dest -Force | Select-Object -First 1)) {
    throw "$Dest exists and is not an sc-hub view folder; set SCHUB_VIEW_DIR to an empty folder"
}
if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
    throw "tar.exe is missing (it comes with Windows 10 1803 and later): update Windows, then run this again"
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
    # The sc-hub key runs only sc-hub's own commands: `view-sum` rebuilds the dashboard and prints a
    # checksum of its figures, notebooks and reports; `view-pack light|full` sends the view as a tar
    # stream. The page and its project scripts come every time, the rest only when the checksum changed.
    $sum = (Invoke-Native { ssh -o BatchMode=yes $Alias 'schub/bin/schub view-sum' } | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $sum) { Say 'refresh failed'; return }
    $all = $sum -ne $script:HeavySum
    $kind = if ($all) { 'full' } else { 'light' }
    $archivePath = Join-Path $Work $ArchiveName
    try {
        if (Test-Path $Incoming) { Remove-Item -Recurse -Force $Incoming }
        New-Item -ItemType Directory -Path $Incoming | Out-Null
        # cmd's redirection keeps the bytes as they are (PowerShell 5 pipes would re-encode binary data).
        Push-Location $Work
        try { Invoke-Native { cmd /c "ssh -o BatchMode=yes $Alias schub/bin/schub view-pack $kind > $ArchiveName" } }
        finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { Say 'download failed'; return }
        Invoke-Native { tar -xf $archivePath -C $Incoming }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $Incoming 'index.html'))) { Say 'download failed'; return }
        # Each file or folder replaced as a whole, the page last: an open page reloading in between
        # still finds the project files it points to.
        $names = @()
        foreach ($item in Get-ChildItem $Incoming -Force | Where-Object { $_.Name -ne 'index.html' }) {
            $names += $item.Name
            $old = Join-Path $Dest $item.Name
            if (Test-Path $old) { Remove-Item -Recurse -Force $old }
            Move-Item $item.FullName -Destination $Dest
        }
        if ($all) {
            # A full copy: what the cluster no longer has goes away here too.
            Get-ChildItem $Dest -Force | Where-Object { $_.Name -notin (@('.schub-view', 'index.html') + $names) } |
                Remove-Item -Recurse -Force
        }
        Move-Item -Force (Join-Path $Incoming 'index.html') (Join-Path $Dest 'index.html')
        Remove-Item -Recurse -Force $Incoming
        if ($all) { $script:HeavySum = $sum }
    } catch {
        # A file briefly locked (antivirus, indexer, Explorer): keep the loop alive.
        Say "update failed ($($_.Exception.Message))"
    } finally {
        Remove-Item -Force $archivePath -ErrorAction SilentlyContinue
    }
}

Update-View
if (-not $env:SCHUB_VIEW_NO_OPEN) { Start-Process (Join-Path $Dest 'index.html') }
if ($Once) { return }
Write-Host "sc-hub view: $Dest\index.html (refreshing every ${Every}s, Ctrl-C to stop)"
while ($true) { Start-Sleep -Seconds $Every; Update-View }
