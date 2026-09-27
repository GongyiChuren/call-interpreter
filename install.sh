#!/usr/bin/env bash
# Call Interpreter — install as a systemd service.
#
#   sudo ./install.sh              install to /opt/call-interpreter
#   sudo ./install.sh --port 8790  override the listen port
#
# Idempotent: safe to re-run after editing config.yaml or pulling new code.
set -euo pipefail

APP_DIR=/opt/call-interpreter
SVC_USER=${SUDO_USER:-$(id -un)}
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --dir)  APP_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

echo "==> checking dependencies"
missing=()
command -v python3 >/dev/null || missing+=(python3)
command -v ffmpeg  >/dev/null || missing+=(ffmpeg)
if [ ${#missing[@]} -gt 0 ]; then
  apt-get update -qq
  apt-get install -y "${missing[@]}"
fi

echo "==> installing to $APP_DIR"
mkdir -p "$APP_DIR"
for item in server web tests config.yaml config.test.yaml README.md LICENSE check.py; do
  if [ -e "$SRC_DIR/$item" ]; then
    cp -a "$SRC_DIR/$item" "$APP_DIR/"
  fi
done
# A trailing `cmd && cp` under `set -e` aborts the whole script when the last
# item is missing, so every copy above is guarded with an explicit if.

echo "==> python environment"
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
# edge-tts gives the free TTS voices; the rest is the HTTP/WebSocket/audio stack.
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet \
  'websockets>=13,<16' 'aiohttp>=3.9,<4' 'PyYAML>=6,<7' 'edge-tts>=7,<8'

# Preserve an existing config: it holds the gateway password and API keys.
if [ ! -f "$APP_DIR/config.yaml" ] || [ "$SRC_DIR/config.yaml" != "$APP_DIR/config.yaml" ]; then
  if [ -f "$APP_DIR/config.yaml" ] && [ "${FORCE_CONFIG:-0}" != "1" ]; then
    echo "    keeping existing $APP_DIR/config.yaml (set FORCE_CONFIG=1 to overwrite)"
  fi
fi

if [ -n "$PORT" ]; then
  "$APP_DIR/.venv/bin/python" - "$APP_DIR/config.yaml" "$PORT" <<'PY'
import sys, pathlib, re
p, port = pathlib.Path(sys.argv[1]), sys.argv[2]
s = p.read_text(encoding="utf-8")
s = re.sub(r'(?m)^(\s*port:\s*)\d+', r'\g<1>' + port, s, count=1)
p.write_text(s, encoding="utf-8")
print(f"    port set to {port}")
PY
fi

chown -R "$SVC_USER":"$SVC_USER" "$APP_DIR"

echo "==> systemd unit"
# The unit needs absolute paths, so substitute the real install dir here rather
# than shipping a unit that only works with the default layout.
sed "s#__APP_DIR__#$APP_DIR#g" "$SRC_DIR/systemd/call-interpreter@.service" \
  > /etc/systemd/system/call-interpreter@.service
chmod 0644 /etc/systemd/system/call-interpreter@.service
systemctl daemon-reload
systemctl enable "call-interpreter@$SVC_USER"
systemctl restart "call-interpreter@$SVC_USER"

sleep 2
if systemctl is-active --quiet "call-interpreter@$SVC_USER"; then
  ip=$(hostname -I | awk '{print $1}')
  port=$(grep -E '^\s*port:' "$APP_DIR/config.yaml" | head -1 | awk '{print $2}')
  echo
  echo "==> running:  http://${ip}:${port}/"
  echo "    status:   systemctl status call-interpreter@$SVC_USER"
  echo "    logs:     journalctl -u call-interpreter@$SVC_USER -f"
  echo
  echo "    NEXT: edit $APP_DIR/config.yaml"
  echo "          - interpreter.fused.api_key  (or export the env var it names)"
  echo "          - sip.*  with your gateway's WebRTC endpoint"
else
  echo "!! service failed to start:" >&2
  journalctl -u "call-interpreter@$SVC_USER" -n 30 --no-pager >&2
  exit 1
fi
