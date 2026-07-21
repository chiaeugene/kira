# One-time: publish Kira to GitHub as a private repo.
# Prereq: gh CLI installed (winget install GitHub.cli) — this script handles login.
param(
    [string]$RepoName = "kira"
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

# 1. Login if needed (opens browser once)
gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
    gh auth login --hostname github.com --git-protocol https --web
}

# 2. Create private repo from the current directory and push
gh repo create $RepoName --private --source . --remote origin --push

Write-Host ""
Write-Host "Published. Next: Render dashboard -> New -> Blueprint -> select '$RepoName' -> Apply."
