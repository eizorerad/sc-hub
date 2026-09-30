# Your sc-hub dashboard on this computer (http://sc-hub.localhost:27182): starts it in the background if it is
# not running, then opens it. It keeps running until you stop it or restart the computer.
#   ~\.sc-hub\bin\schub-view.cmd            start and open
#   ~\.sc-hub\bin\schub-view.cmd status     (and stop, serve, --once)
# The dashboard itself is schub_view.py next to this file (the setup installs both in ~\.sc-hub\bin). It needs
# Python 3.9+: the one the setup used first (written in below), then py -3, python or python3, then uv.
# (the .cmd wrapper runs this script even where PowerShell scripts are blocked)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "schub_view.py"
$pinned = "__PYTHON__"
$rest = @($args | ForEach-Object { if ($_ -eq "-Once") { "--once" } else { $_ } })  # the older mirror's switch

function Test-Python($Exe, $Prefix) {
    # The Microsoft Store's stand-in "python" prints to stderr and fails: a candidate, not an error.
    $ErrorActionPreference = "Continue"
    if (-not (Get-Command $Exe -ErrorAction SilentlyContinue)) { return $false }
    & $Exe @Prefix -c "import sys; sys.exit(sys.version_info < (3, 9))" 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

$candidates = @(@{Exe = $pinned; Prefix = @()}, @{Exe = "py"; Prefix = @("-3")}, @{Exe = "python"; Prefix = @()},
                @{Exe = "python3"; Prefix = @()})
foreach ($candidate in $candidates) {
    if (Test-Python $candidate.Exe $candidate.Prefix) {
        $prefix = $candidate.Prefix
        & $candidate.Exe @prefix $script @rest
        exit $LASTEXITCODE
    }
}
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv run --no-project --python 3.12 python $script @rest
    exit $LASTEXITCODE
}
Write-Host "sc-hub: the dashboard needs Python 3.9 or newer, or uv (https://docs.astral.sh/uv/)"
exit 1
