#!/usr/bin/env bash
# Install CrunchyByt on a Raspberry Pi running Tronbyt server.
# Can be invoked from anywhere — the script locates its own repo root.
set -euo pipefail

APP_DIR=/opt/crunchybyt
DATA_DIR=/srv/crunchybyt
CFG_DIR=/etc/crunchybyt

# Resolve the repo root from this script's location so `./` doesn't mean
# "wherever sudo happened to be invoked from" (that's how we once rsync'd
# all of $HOME into /opt/crunchybyt — not great).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
[[ -f "$REPO_ROOT/pyproject.toml" ]] || {
    echo "expected $REPO_ROOT/pyproject.toml — is this the repo root?" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }

apt-get update
apt-get install -y python3-venv python3-pip ffmpeg libwebp-dev

# Stop any existing daemon before we shuffle files under it (otherwise an
# in-progress crash loop will fight us).
systemctl stop crunchybyt 2>/dev/null || true

install -d -m 0755 -o pi -g pi "$APP_DIR" "$DATA_DIR" "$DATA_DIR/chunks" "$DATA_DIR/sources"
install -d -m 0755 "$CFG_DIR"

# Copy source from the resolved repo root (not the caller's CWD).
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude 'venv' \
    --exclude '*.egg-info' --exclude 'config.toml' --exclude '*.sqlite*' \
    "$REPO_ROOT/" "$APP_DIR/"

sudo -u pi python3 -m venv "$APP_DIR/venv"
sudo -u pi "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -u pi "$APP_DIR/venv/bin/pip" install -e "$APP_DIR"

if [[ ! -f "$CFG_DIR/config.toml" ]]; then
    install -m 0644 "$APP_DIR/config.example.toml" "$CFG_DIR/config.toml"
    echo "Wrote default config to $CFG_DIR/config.toml — edit it before starting."
fi

install -m 0644 "$APP_DIR/scripts/crunchybyt.service" /etc/systemd/system/crunchybyt.service
systemctl daemon-reload
echo
echo "Installed. Next steps:"
echo "  1) Edit $CFG_DIR/config.toml (device_id, api_key, paths)."
echo "  2) Drop episodes under $DATA_DIR/sources/<Show>/*.mkv"
echo "  3) sudo -u pi $APP_DIR/venv/bin/crunchybyt-ingest scan"
echo "  4) systemctl enable --now crunchybyt"
