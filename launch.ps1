param(
    [string]$Repository,
    [string]$MapPath,
    [string]$Python,
    [int]$Port = 8765,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
$toolRoot = $PSScriptRoot
if (-not $Python) {
    $bundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $bundledPython) {
        $Python = $bundledPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw 'Python 3.10+ is required. Pass its path using -Python.' }
        $Python = $pythonCommand.Source
    }
}

Push-Location -LiteralPath $toolRoot
try {
    if (-not $MapPath) {
        $MapPath = if ($Repository) { Join-Path $Repository '.codemap' } else { Join-Path $toolRoot 'work/sample-canvas' }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $MapPath 'map.sqlite'))) {
        if ($Repository) {
            & $Python -m codemap init $Repository --output $MapPath
        } else {
            & $Python examples/review_sample.py --output $MapPath
        }
        if ($LASTEXITCODE -ne 0) { throw 'Project preparation failed; no canvas was started.' }
    }
    if ($PrepareOnly) {
        Write-Output "Project ready: $MapPath"
    } else {
        & $Python -m codemap serve $MapPath --port $Port
        if ($LASTEXITCODE -ne 0) { throw 'Canvas could not start. Check the project path or choose a different port.' }
    }
} finally {
    Pop-Location
}
