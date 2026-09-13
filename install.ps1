param([string]$Python, [string]$Codex)

$ErrorActionPreference = 'Stop'
if (-not $Python) {
    $blueprintBundledPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $blueprintBundledPython) {
        $Python = $blueprintBundledPython
    } else {
        $blueprintPythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $blueprintPythonCommand) { throw 'Python 3.10+ is required. Pass -Python with its full path.' }
        $Python = $blueprintPythonCommand.Source
    }
}
if (-not $Codex) {
    $blueprintCodexCommand = Get-Command codex -ErrorAction SilentlyContinue
    if (-not $blueprintCodexCommand) { throw 'Codex CLI was not found. Pass -Codex with its full path.' }
    $Codex = $blueprintCodexCommand.Source
}
& $Python -X utf8 (Join-Path $PSScriptRoot 'scripts/install_plugin.py') --codex $Codex
if ($LASTEXITCODE -ne 0) { throw 'Code-blueprint installation failed. See the preceding error.' }
