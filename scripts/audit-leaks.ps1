<#
.SYNOPSIS
    Pre-push leak audit for the `comes` repo.

.DESCRIPTION
    Scans tracked-eligible files for personal identifiers that must never
    be committed (emails, IDs, names, addresses, real subscription/tenant
    GUIDs). Exits 0 if clean, 1 if leaks found.

    Wire into a git pre-push hook, or run manually before `git push`:
        ./scripts/audit-leaks.ps1
        ./scripts/audit-leaks.ps1 -Staged    # only files staged for commit

    Custom patterns can be added via -ExtraPatterns @('pattern1','pattern2').

.NOTES
    This is the operational counterpart to AGENTS.md rule 7.
    Same discipline as samoletovs/me's pre-push audit.
#>
[CmdletBinding()]
param(
    [switch]$Staged,
    [string[]]$ExtraPatterns = @()
)

$ErrorActionPreference = 'Stop'

# Default patterns: things that should NEVER appear in tracked files.
# Plain-text (SimpleMatch) — keep it boring and fast.
$Patterns = @(
    '@users.noreply.github.com',     # work email domain
    '@users.noreply.github.com',          # personal email domain
    'REDACTED_CHAT_ID',         # Telegram chat ID
    '[redacted]',           # Azure subscription ID prefix
    '146099412+samoletovs@users.noreply.github.com',         # personal email handle
    '[redacted]',           # real first name
    '[redacted]',           # real surname (covers [redacted]s/[redacted]a)
    '[redacted]',
    '[redacted]',
    '[redacted]',
    '[redacted]',
    '[redacted]',             # street name
    'Marupe'               # neighborhood
) + $ExtraPatterns

# Locate repo root from the script location.
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Push-Location $repoRoot
try {
    if ($Staged) {
        # Only files staged for commit.
        $staged = git diff --cached --name-only --diff-filter=ACM 2>$null
        if (-not $staged) {
            Write-Host "No staged files to scan." -ForegroundColor Yellow
            exit 0
        }
        $files = $staged | ForEach-Object { Get-Item -LiteralPath $_ -ErrorAction SilentlyContinue } | Where-Object { $_ -and -not $_.PSIsContainer }
    }
    else {
        # All tracked-eligible files: everything except build/runtime noise
        # and the real .env (gitignored). NOTE: .env.example IS scanned -
        # it's committed and should only ever contain placeholders.
        $files = Get-ChildItem -Recurse -File | Where-Object {
            $_.FullName -notmatch '\\(\.venv|venv|node_modules|\.git|__pycache__|bin|obj|dist|\.pytest_cache|\.mypy_cache|\.ruff_cache)\\' `
                -and $_.Name -ne '.env' `
                -and $_.Name -notmatch '^\.env\.[^e]' `
                -and $_.Name -ne 'audit-leaks.ps1'   # don't scan ourselves - patterns live here legitimately
        }
    }

    if (-not $files) {
        Write-Host "No files to scan." -ForegroundColor Yellow
        exit 0
    }

    Write-Host "Scanning $($files.Count) file(s) for $($Patterns.Count) pattern(s)..." -ForegroundColor Cyan

    $leaks = 0
    foreach ($p in $Patterns) {
        $hits = $files | Select-String -Pattern $p -SimpleMatch -ErrorAction SilentlyContinue
        if ($hits) {
            $leaks += $hits.Count
            Write-Host ""
            Write-Host "[LEAK '$p']" -ForegroundColor Red
            $hits | ForEach-Object {
                $rel = Resolve-Path -Relative $_.Path
                Write-Host "  $($rel):$($_.LineNumber)  $($_.Line.Trim())" -ForegroundColor Yellow
            }
        }
    }

    Write-Host ""
    if ($leaks -gt 0) {
        Write-Host "FAIL: $leaks leak(s) found. Move values to .env (gitignored) or to the Personal OS, then re-stage." -ForegroundColor Red
        exit 1
    }
    else {
        Write-Host "OK: repo is clean across $($Patterns.Count) pattern(s)." -ForegroundColor Green
        exit 0
    }
}
finally {
    Pop-Location
}
