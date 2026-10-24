<#
.SYNOPSIS
    Start the NovelCraft backend for local development.

.DESCRIPTION
    This script exists to wrap two recurring footguns. It keeps its own text
    ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files using the
    system ANSI code page unless the file has a UTF-8 BOM, so non-ASCII
    comments get mangled and break parsing on non-English systems. Keeping it
    ASCII means any editor or tool can save this file safely.

    1. Interpreter resolution.
       On this machine the Microsoft Store app-execution alias
       (C:\Users\...\Microsoft\WindowsApps) appears on PATH before the real
       Python, so a bare `python` fails with "Python was not found".
       This script resolves the interpreter explicitly by priority and never
       relies on PATH order. It does NOT modify PATH, app-execution aliases,
       or any machine-level setting.

    2. Environment hygiene.
       Leftover variables from temporary test runs (NOVELCRAFT_PORT,
       NOVELCRAFT_DB_PATH, NOVELCRAFT_DATA_DIR, NOVELCRAFT_RELOAD) would
       silently point the app at a test database or a different port.
       They are cleared before start so the app uses backend/.env.
       This script does NOT modify .env and does not touch the registry.

.PARAMETER Port
    Listening port for this process only (sets a process environment
    variable; no config file is changed). Defaults to the value in .env.

.PARAMETER Reload
    Enable uvicorn auto-reload (same as NOVELCRAFT_RELOAD=1). Off by default
    so no stray backend processes are left behind.

.EXAMPLE
    .\start-backend.ps1
    Start with .env configuration (default port 8000).

.EXAMPLE
    .\start-backend.ps1 -Port 8001 -Reload
    Alternative port with auto-reload, for temporary end-to-end checks.

.NOTES
    Reversible: delete this file. It has no persistent side effects.
#>
param(
    [int]$Port = 0,
    [switch]$Reload
)

$ErrorActionPreference = 'Stop'

# Clear leftover temporary overrides. Session scope only, nothing is persisted.
foreach ($name in 'NOVELCRAFT_PORT', 'NOVELCRAFT_DB_PATH', 'NOVELCRAFT_DATA_DIR', 'NOVELCRAFT_RELOAD') {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}

# Interpreter priority: project venv -> shared sibling venv -> py launcher -> common install path.
$candidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe')
)
$python = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $python) {
    # The official `py` launcher is not affected by the Store alias, so asking it
    # for sys.executable is the most reliable way to find the real interpreter.
    try {
        $reported = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($reported -and (Test-Path -LiteralPath $reported)) { $python = $reported.Trim() }
    } catch {
        # No py launcher available; fall through to the next candidate.
    }
}

if (-not $python) {
    $fallback = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    if (Test-Path -LiteralPath $fallback) { $python = $fallback }
}

if (-not $python) {
    throw 'No usable Python interpreter found. Install Python 3.11 (or the py launcher), or run "python -m venv .venv" in backend/ and retry.'
}

if ($Port -gt 0) { $env:NOVELCRAFT_PORT = "$Port" }
if ($Reload) { $env:NOVELCRAFT_RELOAD = '1' }

Write-Host "Interpreter : $python" -ForegroundColor DarkGray
Write-Host "Database    : see backend/.env (this script does not change it)" -ForegroundColor DarkGray
if ($Port -gt 0) { Write-Host "Port        : $Port (this process only)" -ForegroundColor DarkGray }

# The backend resolves relative data paths against the process working directory,
# so it must be started from backend/.
Push-Location -LiteralPath $PSScriptRoot
try {
    & $python main.py
} finally {
    Pop-Location
}
