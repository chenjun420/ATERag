#!/bin/bash
# 板卡能力与可写路径探测 (容器化受限环境, CapEff=0)
echo "===== 身份与能力 ====="
id
grep -E 'CapEff|CapBnd|NoNewPrivs' /proc/self/status
echo "--- 是否有 useradd/groupadd ---"
command -v useradd || echo "no useradd"
command -v systemctl && systemctl --version | head -1

echo "===== 可写路径探测 ====="
for d in /opt/aterag /opt /home/<board_user> /home/<board_user>/aterag /var/log /tmp; do
  if touch "$d/.wtest" 2>/dev/null; then
    echo "WRITABLE   $d"
    rm -f "$d/.wtest"
  else
    echo "READ-ONLY  $d"
  fi
done

echo "===== SSH 用户能否在 /opt 建目录 ====="
su - "$BOARD_USER" -c "mkdir -p /opt/ateragtest 2>&1 && echo 'user CAN mkdir /opt' && rmdir /opt/ateragtest" || echo "user CANNOT mkdir /opt"

echo "===== systemd 能否启停自定义服务 ====="
cat >/tmp/probe.service <<'EOF'
[Unit]
Description=probe
[Service]
Type=oneshot
ExecStart=/bin/echo probe-ok
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
cp /tmp/probe.service /etc/systemd/system/probe.service 2>&1 && \
  systemctl daemon-reload && systemctl start probe.service 2>&1 && \
  systemctl is-active probe.service && echo "SYSTEMD_CUSTOM_OK" || echo "SYSTEMD_CUSTOM_FAIL"
systemctl disable --now probe.service 2>/dev/null
rm -f /etc/systemd/system/probe.service /tmp/probe.service

echo "===== SSH 用户家目录下的 uv/python ====="
ls -d "$HOME/.local/bin" 2>/dev/null || echo "SSH 用户无 ~/.local/bin"
ls -d /root/.local/share/uv/python/* 2>/dev/null | head -3 || echo "root uv python 不可见"
echo "PROBE_DONE"
