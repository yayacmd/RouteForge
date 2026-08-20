#!/usr/bin/env bash
# RouteForge installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/routeforge/main/install.sh | bash
#
# Installs into ~/routeforge, starts it with Docker, and opens your browser.

set -euo pipefail

INSTALL_DIR="${ROUTEFORGE_DIR:-$HOME/routeforge}"
PORT="${ROUTEFORGE_PORT:-8000}"
IMAGE="ghcr.io/OWNER/routeforge:latest"
CONTAINER="routeforge"

bold()  { printf '\033[1m%s\033[0m\n' "$1"; }
info()  { printf '  %s\n' "$1"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()   { printf '\n  \033[31m✗ %s\033[0m\n\n' "$1" >&2; exit 1; }

echo
bold "RouteForge — delivery route planning"
echo

# ---- Docker -----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  die "Docker isn't installed.
     RouteForge runs inside Docker so you don't have to install Python
     or anything else. Install Docker Desktop, then run this again:
       https://www.docker.com/products/docker-desktop/"
fi

if ! docker info >/dev/null 2>&1; then
  die "Docker is installed but not running.
     Start Docker Desktop (or the docker service), then run this again."
fi
ok "Docker is ready"

# ---- Port -------------------------------------------------------------------
if command -v lsof >/dev/null 2>&1 && lsof -i ":$PORT" >/dev/null 2>&1; then
  warn "Something is already using port $PORT."
  read -rp "  Use a different port? [8080]: " NEWPORT </dev/tty || true
  PORT="${NEWPORT:-8080}"
fi

# ---- Existing install -------------------------------------------------------
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  warn "RouteForge is already installed."
  read -rp "  Update it to the latest version? Your data is kept. [y/N]: " REPLY </dev/tty || true
  case "${REPLY:-n}" in
    [yY]*)
      info "Stopping the old version…"
      docker stop "$CONTAINER" >/dev/null 2>&1 || true
      docker rm "$CONTAINER"   >/dev/null 2>&1 || true
      ;;
    *) die "Left the existing installation alone." ;;
  esac
fi

mkdir -p "$INSTALL_DIR"
ok "Using $INSTALL_DIR"

# ---- Image ------------------------------------------------------------------
info "Downloading RouteForge (a few hundred MB the first time)…"
if ! docker pull "$IMAGE" >/dev/null 2>&1; then
  warn "Couldn't download the prebuilt image; building from source instead."
  if [ ! -f "$INSTALL_DIR/Dockerfile" ]; then
    command -v git >/dev/null 2>&1 || die "Need either a working image download or git installed."
    git clone --depth 1 https://github.com/OWNER/routeforge.git "$INSTALL_DIR/src" >/dev/null 2>&1
    INSTALL_SRC="$INSTALL_DIR/src"
  else
    INSTALL_SRC="$INSTALL_DIR"
  fi
  docker build -t "$IMAGE" "$INSTALL_SRC" || die "Build failed."
fi
ok "RouteForge downloaded"

# ---- Run --------------------------------------------------------------------
docker volume create routeforge-data >/dev/null

# Bound to 127.0.0.1: reachable from this computer only. See the README for
# how to open it up to your office network safely.
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "127.0.0.1:${PORT}:8000" \
  -v routeforge-data:/data \
  "$IMAGE" >/dev/null

info "Starting up…"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    ok "RouteForge is running"
    break
  fi
  [ "$i" -eq 30 ] && die "It didn't start. Check the logs with: docker logs $CONTAINER"
  sleep 1
done

URL="http://localhost:${PORT}"
echo
bold "Ready — open $URL"
echo
info "Your browser will walk you through the one-time setup."
info "You'll need a free API key from https://locationiq.com/register"
echo
info "To stop:    docker stop $CONTAINER"
info "To start:   docker start $CONTAINER"
info "To update:  run this installer again"
echo

if command -v open >/dev/null 2>&1; then open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
fi
