# Open your running sc-hub session (JupyterLab or cellxgene) in the browser.
# The session runs on a compute node; this opens an ssh tunnel through the login
# node and the browser. Ask the assistant to start a session first.
#   .\schub-lab.cmd              JupyterLab
#   .\schub-lab.cmd cellxgene    cellxgene
param([string]$Kind = 'jupyter')
$ErrorActionPreference = 'Stop'
$Alias = if ($env:SCHUB_ALIAS) { $env:SCHUB_ALIAS } else { 'mbzuai-schub' }
if ($Kind -notin 'jupyter', 'cellxgene') { Write-Host 'usage: .\schub-lab.cmd [jupyter|cellxgene]'; exit 2 }

$previous = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $info = (ssh -o BatchMode=yes $Alias "schub/bin/schub session-info $Kind" 2>$null | Out-String).Trim() }
finally { $ErrorActionPreference = $previous }
$node, $port, $path = "$info" -split '\s+'
# The reply comes from the cluster: accept only what a session can look like.
if ("$node" -notmatch '^[A-Za-z0-9.-]+$' -or "$port" -notmatch '^[0-9]{4,5}$' -or "$path" -notmatch '^/[A-Za-z0-9/?=._~-]*$') {
    Write-Host "No running $Kind session. Ask the assistant to start one (it may still be waiting in the queue)."
    exit 1
}
$localPort = if ($env:SCHUB_LAB_PORT) { $env:SCHUB_LAB_PORT } else { $port }
$tunnel = Start-Process ssh -NoNewWindow -PassThru -ArgumentList @(
    '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-N', '-L', "127.0.0.1:${localPort}:${node}:${port}", $Alias)
Start-Sleep -Seconds 2
if ($tunnel.HasExited) { Write-Host "The tunnel failed (is local port $localPort busy? set SCHUB_LAB_PORT)."; exit 1 }
$url = "http://localhost:$localPort$path"
Start-Process $url
Write-Host "sc-hub $Kind on ${node}: $url (Ctrl-C closes the tunnel; the session keeps running)"
try { Wait-Process -Id $tunnel.Id } finally { if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id } }
