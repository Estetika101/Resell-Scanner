#!/bin/bash
# Install or update sgw-watch on roger. Run from this folder as the user that
# should own it (pi):   ./install.sh        or   PORT=8090 ./install.sh
# Safe to re-run: the database (sgw-watch.db) is never touched.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
PORT="${PORT:-8088}"
ME="$(id -un)"

python3 -c 'import sys, zoneinfo; assert sys.version_info >= (3, 9)' \
  || { echo "Needs Python 3.9+ (Pi OS Bookworm has 3.11)."; exit 1; }

# Two scanners on one IP doubles the request rate ShopGoodwill sees.
for t in flip-scan.timer flip-close.timer flip-asks.timer; do
  if systemctl is-active --quiet "$t" 2>/dev/null; then
    echo "WARNING: the retired scanner's $t is still active on this Pi."
    echo "         Stop it with: sudo systemctl disable --now $t"
  fi
done

if ! systemctl is-active --quiet sgw-watch 2>/dev/null && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "Port $PORT is in use. Re-run with another, e.g. PORT=8090 ./install.sh"; exit 1
fi

echo "Running tests..."
python3 -m unittest discover -s tests -q

sudo tee /etc/systemd/system/sgw-watch.service >/dev/null <<EOF
[Unit]
Description=sgw-watch web app (ShopGoodwill camera watch list)
After=network-online.target
Wants=network-online.target

[Service]
User=$ME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/app.py serve --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/sgw-watch-scan.service >/dev/null <<EOF
[Unit]
Description=sgw-watch scan pass
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$ME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/app.py scan
# 40 terms at 1.5 s plus up to 60 shipping quotes is ~3 minutes. This only
# catches a hang. Don't lower the 1.5 s delay to speed it up.
TimeoutStartSec=1800
Nice=10
EOF

sudo tee /etc/systemd/system/sgw-watch-scan.timer >/dev/null <<EOF
[Unit]
Description=sgw-watch scan every 2 hours

[Timer]
# OnActiveSec starts the cycle when the timer is enabled on a Pi that has
# been up for days; OnUnitActiveSec alone never fires if the scan never ran.
OnBootSec=5min
OnActiveSec=2min
OnUnitActiveSec=2h
RandomizedDelaySec=240

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now sgw-watch.service sgw-watch-scan.timer
sudo systemctl restart sgw-watch.service
sleep 1
systemctl is-active --quiet sgw-watch && echo "Running: http://$(hostname).local:$PORT" \
  || { echo "Service failed to start:"; journalctl -u sgw-watch -n 20 --no-pager; exit 1; }
echo "First scan runs within 5 minutes of boot, then every 2 hours. Or press Scan now."
