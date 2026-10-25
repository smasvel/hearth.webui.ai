$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    throw 'Python 3.12 or newer is required. Install Python and make sure it is on PATH.'
}
$Version = & $Python.Source -c 'import sys; print(sys.version_info >= (3, 12))'
if ($Version -ne 'True') { throw 'Python 3.12 or newer is required.' }
& $Python.Source tools/release_installer.py @args
if ($LASTEXITCODE -ne 0) { throw "Hearth setup failed with exit code $LASTEXITCODE." }
