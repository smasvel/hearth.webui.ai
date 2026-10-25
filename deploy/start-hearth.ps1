$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $ProjectDir
$Python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Hearth is not installed yet. Run deploy\install-release.ps1 first.' }
& $Python app.py
