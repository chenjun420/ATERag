#!/bin/bash
# ATERag 板卡原生部署 Step 3: Qdrant v1.19.1 (aarch64-musl 静态二进制 + systemd)
set -euo pipefail
QDRANT_VERSION=1.19.1

curl -fsSL -o /tmp/qdrant.tar.gz \
    "https://github.com/qdrant/qdrant/releases/download/v${QDRANT_VERSION}/qdrant-aarch64-unknown-linux-musl.tar.gz"
sudo mkdir -p /opt/qdrant/storage
sudo tar xzf /tmp/qdrant.tar.gz -C /opt/qdrant
sudo chmod +x /opt/qdrant/qdrant
rm -f /tmp/qdrant.tar.gz

sudo tee /etc/systemd/system/qdrant.service >/dev/null <<'EOF'
[Unit]
Description=Qdrant vector database (ATERag)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/qdrant
ExecStart=/opt/qdrant/qdrant
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now qdrant
sleep 3
systemctl is-active qdrant
echo "== STEP3 DONE =="
