#!/usr/bin/env bash
# One-time setup on the Raspberry Pi. Combinable flags:
#   ./pi/setup.sh                 venv + requirements only
#   ./pi/setup.sh --service       + pi-voice systemd unit (127.0.0.1:3000)
#   ./pi/setup.sh --nginx         + nginx site on :80 (WebSocket-ready)
#   ./pi/setup.sh --tls           + self-signed cert and :443 site too
#                                 (implies --nginx; needed for iPhone/Safari)
set -euo pipefail
cd "$(dirname "$0")/.."

WANT_SERVICE=0; WANT_NGINX=0; WANT_TLS=0
for arg in "$@"; do
  case "$arg" in
    --service) WANT_SERVICE=1 ;;
    --nginx)   WANT_NGINX=1 ;;
    --tls)     WANT_TLS=1; WANT_NGINX=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# vcgencmd (thermal badge) needs /dev/vchiq, which is group "video".
if ! id -nG "$USER" | grep -qw video; then
  echo "Adding $USER to the 'video' group so vcgencmd works (re-login to apply)."
  sudo usermod -aG video "$USER"
fi

# ---------------------------------------------------------------- nginx
if [ "$WANT_NGINX" = "1" ]; then
  command -v nginx >/dev/null || sudo apt-get install -y nginx

  TLS_BLOCK=""
  if [ "$WANT_TLS" = "1" ]; then
    # Self-signed cert, SANs cover hostname + current IPs. iOS rejects
    # certs valid >825 days, so don't bump that.
    PI_IP=$(hostname -I | awk '{print $1}')
    sudo mkdir -p /etc/nginx/pi-voice
    sudo openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
      -keyout /etc/nginx/pi-voice/pi-voice.key \
      -out /etc/nginx/pi-voice/pi-voice.crt \
      -subj "/CN=pi-voice" \
      -addext "subjectAltName=DNS:pi-03.local,IP:10.104.0.55,IP:${PI_IP}"
    TLS_BLOCK=$(cat <<'NGINX'
    listen 443 ssl;
    ssl_certificate     /etc/nginx/pi-voice/pi-voice.crt;
    ssl_certificate_key /etc/nginx/pi-voice/pi-voice.key;
NGINX
)
  fi

  sudo tee /etc/nginx/sites-available/pi-voice >/dev/null <<NGINX
server {
    listen 80 default_server;
${TLS_BLOCK}
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 3600s;
    }
}
NGINX

  sudo ln -sf /etc/nginx/sites-available/pi-voice \
    /etc/nginx/sites-enabled/pi-voice
  sudo rm -f /etc/nginx/sites-enabled/default   # frees default_server
  sudo nginx -t && sudo systemctl reload nginx
  echo "nginx site live: http://$(hostname -I | awk '{print $1}')"
  if [ "$WANT_TLS" = "1" ]; then
    echo "TLS too: https://<pi-ip> (self-signed — accept the warning once)"
  fi
fi

# ---------------------------------------------------------------- service
if [ "$WANT_SERVICE" = "1" ]; then
  # Bound to 127.0.0.1: only nginx can reach it, not the whole LAN.
  sed -e "s|@USER@|$USER|g" -e "s|@DIR@|$PWD|g" pi/pi-voice.service \
    | sudo tee /etc/systemd/system/pi-voice.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now pi-voice
  cat <<'EOF'

Service installed: pi-voice (127.0.0.1:3000, restarts on crash/reboot).

  sudo systemctl status pi-voice
  journalctl -u pi-voice -f
EOF
fi

cat <<'EOF'

Setup complete.

  ./start.sh                # run now (0.0.0.0:3000)
  ./pi/setup.sh --service   # run as a service instead
  ./pi/setup.sh --nginx     # nginx proxy on :80 (WS headers included)
  ./pi/setup.sh --tls       # + self-signed HTTPS on :443 for iPhone
EOF
