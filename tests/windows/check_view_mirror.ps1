# schub-view.ps1 on Windows PowerShell 5.1 against a fake cluster (tests/windows/ssh.cmd): run by CI.
# Checks the download through cmd's redirection and tar, binary files byte for byte, spaces in the paths.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $here '..\..')).Path
$root = Join-Path $env:RUNNER_TEMP 'mirror test'
if (Test-Path $root) { Remove-Item -Recurse -Force $root }
$src = Join-Path $root 'cluster view'
New-Item -ItemType Directory -Force -Path (Join-Path $src 'jproj'), (Join-Path $src 'jfig\a') | Out-Null
Set-Content -Path (Join-Path $src 'index.html') -Value '<html>page</html>' -NoNewline
Set-Content -Path (Join-Path $src 'versions.json') -Value '{}' -NoNewline
Set-Content -Path (Join-Path $src 'jproj\a.js') -Value 'x' -NoNewline
$bytes = [byte[]]@((0..255) + @(13, 10, 0, 26, 255))
[IO.File]::WriteAllBytes((Join-Path $src 'jfig\a\c1.png'), $bytes)

$env:FAKE_VIEW_SRC = $src
$env:PATH = "$here;$env:PATH"
$env:SCHUB_VIEW_DIR = Join-Path $root 'sc hub view'
$env:SCHUB_VIEW_NO_OPEN = '1'
& (Join-Path $repo 'scripts\schub-view.cmd') -Once
if ($LASTEXITCODE -ne 0) { throw "schub-view exited with $LASTEXITCODE" }

$dest = $env:SCHUB_VIEW_DIR
foreach ($name in 'index.html', 'versions.json', 'jproj\a.js', 'jfig\a\c1.png', '.schub-view') {
    if (-not (Test-Path (Join-Path $dest $name))) { throw "the mirror has no $name" }
}
$copied = [IO.File]::ReadAllBytes((Join-Path $dest 'jfig\a\c1.png'))
if ([Convert]::ToBase64String($copied) -ne [Convert]::ToBase64String($bytes)) { throw 'a binary file changed on the way' }
if (Test-Path "$dest.incoming") { throw 'the download folder was left behind' }
if (Test-Path (Join-Path $root '.sc-hub-view.tar')) { throw 'the archive was left behind' }
Write-Host 'schub-view.ps1 against the fake cluster: ok'
