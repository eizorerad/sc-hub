# The dashboard on Windows PowerShell 5.1 against a fake cluster (tests/windows/ssh.cmd): run by CI.
# schub-view.cmd finds a Python and runs schub_view.py: one copy through view-sum/view-pack (binary files byte
# for byte, spaces in the paths), then the server in the background, what it serves, and stopping it.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $here '..\..')).Path
$root = Join-Path $env:RUNNER_TEMP 'mirror test'
if (Test-Path $root) { Remove-Item -Recurse -Force $root }
$src = Join-Path $root 'cluster view'
New-Item -ItemType Directory -Force -Path (Join-Path $src 'jproj'), (Join-Path $src 'jfig\a') | Out-Null
Set-Content -Path (Join-Path $src 'index.html') -Value '<html><body>page</body></html>' -NoNewline
Set-Content -Path (Join-Path $src 'jproj\versions.json') -Value '{}' -NoNewline
Set-Content -Path (Join-Path $src 'jproj\a.js') -Value 'x' -NoNewline
$bytes = [byte[]]@((0..255) + @(13, 10, 0, 26, 255))
[IO.File]::WriteAllBytes((Join-Path $src 'jfig\a\c1.png'), $bytes)

$env:FAKE_VIEW_SRC = $src
$env:PATH = "$here;$env:PATH"
$env:SCHUB_VIEW_DIR = Join-Path $root 'sc hub view'
$launcher = Join-Path $repo 'scripts\schub-view.cmd'

& $launcher --once
if ($LASTEXITCODE -ne 0) { throw "schub-view --once exited with $LASTEXITCODE" }
$dest = $env:SCHUB_VIEW_DIR
foreach ($name in 'index.html', 'jproj\versions.json', 'jproj\a.js', 'jfig\a\c1.png', '.schub-view') {
    if (-not (Test-Path (Join-Path $dest $name))) { throw "the copy has no $name" }
}
$copied = [IO.File]::ReadAllBytes((Join-Path $dest 'jfig\a\c1.png'))
if ([Convert]::ToBase64String($copied) -ne [Convert]::ToBase64String($bytes)) { throw 'a binary file changed on the way' }
if (Test-Path "$dest.incoming") { throw 'the download folder was left behind' }
if (Get-ChildItem (Join-Path $HOME '.sc-hub') -Filter 'view-*.tar' -ErrorAction SilentlyContinue) { throw 'the archive was left behind' }
& $launcher -Once  # the older mirror's switch still works
if ($LASTEXITCODE -ne 0) { throw "schub-view -Once exited with $LASTEXITCODE" }

# In the background: it must answer after the launcher has returned, serve the copy, and stop when asked.
$out = & $launcher start --no-open --json
if ($LASTEXITCODE -ne 0) { throw "schub-view start exited with $LASTEXITCODE" }
$info = ($out | Select-Object -Last 1) | ConvertFrom-Json
try {
    $page = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$($info.port)/"
    if ($page.Content -notmatch 'page') { throw 'the server did not serve the copy' }
    if ($page.Headers['Content-Security-Policy'] -notmatch "frame-ancestors 'none'") { throw 'no policy on the page' }
    $png = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$($info.port)/jfig/a/c1.png"
    if ([Convert]::ToBase64String($png.Content) -ne [Convert]::ToBase64String($bytes)) { throw 'the server changed a file' }
    $status = & $launcher status --json | Select-Object -Last 1 | ConvertFrom-Json
    if ($status.port -ne $info.port) { throw 'status does not see the running server' }
} finally {
    & $launcher stop
}
Start-Sleep -Seconds 1
& $launcher status | Out-Null
if ($LASTEXITCODE -eq 0) { throw 'the server is still running after stop' }
Write-Host 'schub-view on Windows against the fake cluster: ok'
