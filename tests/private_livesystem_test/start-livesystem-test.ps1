# ============================================================================
# Start Darts Penalty Manager - Live System Test (Docker Desktop)
# ============================================================================
#
# This script builds the current local source code and starts the Darts
# Penalty Manager against a copy of the live system database - or, when no
# copy is present, creates a fresh database on first startup.
#
# Usage:
#   .\start-livesystem-test.ps1            # Build (cached) & start (default)
#   .\start-livesystem-test.ps1 build      # Full rebuild (no cache) & start
#   .\start-livesystem-test.ps1 stop       # Stop the container (data kept)
#   .\start-livesystem-test.ps1 stop-clean # Stop and remove containers/networks
#   .\start-livesystem-test.ps1 logs       # Show container logs
#   .\start-livesystem-test.ps1 status     # Show container status + health
#
# ============================================================================

param(
    [ValidateSet("up", "build", "stop", "stop-clean", "logs", "status")]
    [string]$Action = "up"
)

$ErrorActionPreference = "Stop"

# Keep in sync with "ports" in docker-compose.yml (left side) and the health
# path from the Dockerfile/urls.py.
$AppPort = 8200
$AppUrl = "http://localhost:$AppPort"

# Navigate to script directory (in case run from elsewhere)
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host " Darts Penalty Manager - Live System Test" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

# Check if Docker is running
try {
    $dockerVersion = docker version --format '{{.Server.Version}}' 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Docker not running"
    }
    Write-Host "[OK] Docker is running (version: $dockerVersion)" -ForegroundColor Green
}
catch {
    Write-Host "[ERROR] Docker Desktop is not running!" -ForegroundColor Red
    Write-Host "        Please start Docker Desktop first." -ForegroundColor Yellow
    exit 1
}

# Check for the live system database copy: if present it is used, otherwise
# a FRESH database is created on first startup (entrypoint runs migrate +
# bootstrap: role groups, superuser from env, default catalog).
if (-not (Test-Path ".\data")) {
    New-Item -ItemType Directory -Path ".\data" | Out-Null
}
if (Test-Path ".\data\db.sqlite3") {
    $dbSize = (Get-Item ".\data\db.sqlite3").Length / 1KB
    Write-Host "[OK] Database found: .\data\db.sqlite3 ($([math]::Round($dbSize, 1)) KB)" -ForegroundColor Green
}
else {
    Write-Host "[..] No database found - a fresh one will be created on first startup." -ForegroundColor Yellow
    Write-Host "     Optional: use a live system backup instead (run in the repo root):" -ForegroundColor Gray
    Write-Host "       docker compose cp app:/app/data/db.sqlite3 ./tests/private_livesystem_test/data/db.sqlite3" -ForegroundColor Gray
    Write-Host "     Optional: seed demo data after first start:" -ForegroundColor Gray
    Write-Host "       docker exec darts-penalty-livesystem-test python manage.py bootstrap --with-demo" -ForegroundColor Gray
}

# Bootstrap admin from docker-compose.yml (created only if missing in backup).
# The compose file is PRIVATE (real secrets) and is therefore NOT part of the
# repository — fresh checkouts create it first from the committed template.
if (-not (Test-Path ".\docker-compose.yml")) {
    Write-Host "[ERROR] .\docker-compose.yml not found!" -ForegroundColor Red
    Write-Host "        This file contains secrets (SMTP password, secret key) and is" -ForegroundColor Yellow
    Write-Host "        intentionally NOT committed to git." -ForegroundColor Yellow
    Write-Host "        Copy docker-compose.example.yml to docker-compose.yml, fill in your" -ForegroundColor Yellow
    Write-Host "        local credentials and run this script again." -ForegroundColor Yellow
    exit 1
}
$superEmail = ""
$match = Select-String -Path ".\docker-compose.yml" -Pattern "DJANGO_SUPERUSER_EMAIL=(.+)$" | Select-Object -First 1
if ($match) { $superEmail = $match.Matches[0].Groups[1].Value.Trim() }

Write-Host ""

switch ($Action) {
    "up" {
        Write-Host "Building image from local source code (cached layers)..." -ForegroundColor Yellow
        Write-Host "(Repository root: $((Get-Item "$PSScriptRoot\..\..\").FullName))" -ForegroundColor Gray
        Write-Host ""

        docker compose build

        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "[ERROR] Docker build failed!" -ForegroundColor Red
            exit 1
        }

        Write-Host ""
        Write-Host "Starting container..." -ForegroundColor Yellow
        docker compose up -d
    }

    "build" {
        Write-Host "Building image from local source code (NO cache, full rebuild)..." -ForegroundColor Yellow
        Write-Host "(Repository root: $((Get-Item "$PSScriptRoot\..\..\").FullName))" -ForegroundColor Gray
        Write-Host ""

        docker compose build --no-cache

        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host "[ERROR] Docker build failed!" -ForegroundColor Red
            exit 1
        }

        Write-Host ""
        Write-Host "Starting container..." -ForegroundColor Yellow
        docker compose up -d
    }

    "stop" {
        Write-Host "Stopping container (backup copy is preserved)..." -ForegroundColor Yellow
        docker compose down
        Write-Host ""
        Write-Host "[OK] Container stopped. .\data\db.sqlite3 is untouched." -ForegroundColor Green
        Write-Host ""
        return
    }

    "stop-clean" {
        Write-Host "Stopping container and removing containers/networks..." -ForegroundColor Yellow
        docker compose down -v
        Write-Host ""
        Write-Host "[OK] Containers removed." -ForegroundColor Green
        Write-Host "     Note: your database copy in .\data is still intact." -ForegroundColor Gray
        Write-Host ""
        return
    }

    "logs" {
        docker compose logs -f --tail=50
        return
    }

    "status" {
        Write-Host "Container status:" -ForegroundColor Yellow
        Write-Host ""
        docker compose ps
        Write-Host ""

        # Check if app is responding (-UseBasicParsing: no IE first-run prompt)
        try {
            $response = Invoke-WebRequest -Uri "$AppUrl/health/" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
            Write-Host "[OK] Application is responding (HTTP $($response.StatusCode))" -ForegroundColor Green
        }
        catch {
            Write-Host "[WAIT] Application not yet responding (still starting?)" -ForegroundColor Yellow
            Write-Host "       Run: .\start-livesystem-test.ps1 logs" -ForegroundColor Gray
        }
        Write-Host ""
        return
    }
}

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host " Container is starting!" -ForegroundColor Green
    Write-Host "=============================================" -ForegroundColor Green
    Write-Host ""
    Write-Host " Application URL:  " -NoNewline
    Write-Host $AppUrl -ForegroundColor Yellow
    Write-Host ""
    Write-Host " Login:            " -NoNewline
    Write-Host "(your live credentials from the backup)" -ForegroundColor Yellow
    if ($superEmail) {
        Write-Host " Bootstrap admin:  " -NoNewline
        Write-Host $superEmail -ForegroundColor Yellow
        Write-Host "                    (password: docker-compose.yml env, only if newly created)" -ForegroundColor Gray
    }
    Write-Host ""
    Write-Host " Use '.\start-livesystem-test.ps1 logs' to follow startup." -ForegroundColor Gray
    Write-Host " The app runs migrations + bootstrap first (~5-10 seconds)." -ForegroundColor Gray
    Write-Host ""
}
else {
    Write-Host ""
    Write-Host "[ERROR] Failed to start container!" -ForegroundColor Red
    Write-Host "        Run: .\start-livesystem-test.ps1 logs" -ForegroundColor Yellow
    exit 1
}
