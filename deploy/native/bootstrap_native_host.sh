#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/b2b/apps/sky-sriScrapping}"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv-native}"

ensure_chrome_repo() {
  if dpkg -s google-chrome-stable >/dev/null 2>&1; then
    return
  fi

  sudo install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | sudo gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    | sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
}

main() {
  cd "$REPO_DIR"

  ensure_chrome_repo

  sudo apt-get update
  sudo apt-get install -y \
    ca-certificates curl gnupg \
    google-chrome-stable \
    xvfb x11-utils \
    fonts-liberation fonts-noto-cjk \
    libasound2t64 libatk-bridge2.0-0 libatk1.0-0 libcups2t64 \
    libgbm1 libgtk-3-0 libnspr4 libnss3 libxcomposite1 libxdamage1 \
    libxfixes3 libxkbcommon0 libxrandr2 \
    python3.12-venv python3-pip \
    postgresql postgresql-client

  python3.12 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip wheel
  "$VENV_DIR/bin/pip" install -r requirements.txt -r api/requirements.txt

  sudo install -m 0644 deploy/native/sky-sri-api.service /etc/systemd/system/sky-sri-api.service
  sudo install -m 0644 deploy/native/sky-sri-worker.service /etc/systemd/system/sky-sri-worker.service
  sudo systemctl daemon-reload
  sudo systemctl disable --now sky-sri-worker.service || true

  echo "Bootstrap nativo listo."
  echo "Chrome: $(command -v google-chrome || command -v google-chrome-stable)"
  echo "Venv:   $VENV_DIR"
}

main "$@"
