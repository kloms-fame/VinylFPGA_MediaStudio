param([string]$Python='')
$ErrorActionPreference='Stop'
$toolRoot=Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python=Join-Path $toolRoot '.venv/Scripts/python.exe' }
Push-Location $toolRoot
try {
    & $Python scripts/build_portable.py
    if ($LASTEXITCODE -ne 0) { throw 'Portable build failed. See the output above.' }
} finally { Pop-Location }
