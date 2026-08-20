# RouteForge installer for Windows.
#
#   irm https://raw.githubusercontent.com/OWNER/routeforge/main/install.ps1 | iex
#
# Starts RouteForge with Docker and opens your browser.

$ErrorActionPreference = "Stop"

$Port      = if ($env:ROUTEFORGE_PORT) { $env:ROUTEFORGE_PORT } else { "8000" }
$Image     = "ghcr.io/OWNER/routeforge:latest"
$Container = "routeforge"

function Info($m) { Write-Host "  $m" }
function Ok($m)   { Write-Host "  " -NoNewline; Write-Host "OK " -ForegroundColor Green -NoNewline; Write-Host $m }
function Warn($m) { Write-Host "  " -NoNewline; Write-Host "!  " -ForegroundColor Yellow -NoNewline; Write-Host $m }
function Die($m)  { Write-Host ""; Write-Host "  X $m" -ForegroundColor Red; Write-Host ""; exit 1 }

Write-Host ""
Write-Host "RouteForge - delivery route planning" -ForegroundColor White
Write-Host ""

# ---- Docker -----------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Die @"
Docker isn't installed.
     RouteForge runs inside Docker so you don't have to install Python
     or anything else. Install Docker Desktop, then run this again:
       https://www.docker.com/products/docker-desktop/
"@
}

try { docker info 2>&1 | Out-Null } catch {
  Die "Docker is installed but not running. Start Docker Desktop, then run this again."
}
if ($LASTEXITCODE -ne 0) {
  Die "Docker is installed but not running. Start Docker Desktop, then run this again."
}
Ok "Docker is ready"

# ---- Existing install -------------------------------------------------------
$existing = docker ps -a --format "{{.Names}}" | Where-Object { $_ -eq $Container }
if ($existing) {
  Warn "RouteForge is already installed."
  $reply = Read-Host "  Update it to the latest version? Your data is kept. [y/N]"
  if ($reply -match '^[yY]') {
    Info "Stopping the old version..."
    docker stop $Container 2>&1 | Out-Null
    docker rm   $Container 2>&1 | Out-Null
  } else {
    Die "Left the existing installation alone."
  }
}

# ---- Image ------------------------------------------------------------------
Info "Downloading RouteForge (a few hundred MB the first time)..."
docker pull $Image 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "Couldn't download RouteForge. Check your internet connection." }
Ok "RouteForge downloaded"

# ---- Run --------------------------------------------------------------------
docker volume create routeforge-data 2>&1 | Out-Null

# Bound to 127.0.0.1: reachable from this computer only.
docker run -d --name $Container --restart unless-stopped `
  -p "127.0.0.1:${Port}:8000" `
  -v routeforge-data:/data `
  $Image 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "Couldn't start RouteForge. Check: docker logs $Container" }

Info "Starting up..."
$ready = $false
foreach ($i in 1..30) {
  try {
    Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2 | Out-Null
    $ready = $true; break
  } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) { Die "It didn't start. Check the logs with: docker logs $Container" }
Ok "RouteForge is running"

$url = "http://localhost:$Port"
Write-Host ""
Write-Host "Ready - open $url" -ForegroundColor White
Write-Host ""
Info "Your browser will walk you through the one-time setup."
Info "You'll need a free API key from https://locationiq.com/register"
Write-Host ""
Info "To stop:    docker stop $Container"
Info "To start:   docker start $Container"
Info "To update:  run this installer again"
Write-Host ""

Start-Process $url
