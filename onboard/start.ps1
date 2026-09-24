# Start the sc-hub setup page (Windows 10/11). Needs Python 3.9+; without one, uv fetches it.
#   powershell -ExecutionPolicy Bypass -File onboard\start.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
foreach ($candidate in @("py -3", "python", "python3")) {
    $exe, $rest = $candidate.Split(" ", 2)
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $check = if ($rest) { & $exe $rest -c "import sys; sys.exit(sys.version_info < (3, 9))" } else { & $exe -c "import sys; sys.exit(sys.version_info < (3, 9))" }
        if ($LASTEXITCODE -eq 0) {
            if ($rest) { & $exe $rest -m sc_hub_onboard @args } else { & $exe -m sc_hub_onboard @args }
            exit $LASTEXITCODE
        }
    }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[sc-hub] Python 3.9+ not found: installing uv (https://astral.sh/uv) to run the setup"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv run --no-project --python 3.12 python -m sc_hub_onboard @args
exit $LASTEXITCODE
