#!/usr/bin/env bash
# 离线安装后的自检。**每条都对着「装上但没生效」这类失败设计**。
#
# 不做的事: 不「验证一切正常」然后打印绿色。查不动的地方直接说查不动,
# 因为一个永远绿的检查等于没有检查。
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/aterag}"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
fail=0

ok()   { printf '  [ok]   %s\n' "$*"; }
bad()  { printf '  [FAIL] %s\n' "$*"; fail=$((fail + 1)); }
note() { printf '  [note] %s\n' "$*"; }

echo "=== 1 文件与依赖 ==="
[ -x "$PY" ] && ok "venv 可用 ($($PY -V 2>&1))" || { bad "venv 不可用: $PY"; exit 1; }
"$PY" - <<'EOF' || exit 1
import sys
mods = ["semantica", "psycopg", "fastapi", "pydantic"]
missing = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:  # noqa: BLE001
        missing.append(f"{m}: {e}")
print("  [ok]   核心依赖可导入" if not missing else "  [FAIL] 缺依赖: " + "; ".join(missing))
raise SystemExit(1 if missing else 0)
EOF
[ $? -eq 0 ] || fail=$((fail + 1))

echo "=== 2 知识门(种子自带门禁, 不依赖服务) ==="
cd "$APP_DIR"
if "$PY" scripts/knowledge_gate.py --seed data/seed/power_domain_seed.json; then
    ok "知识门通过"
else
    bad "知识门未通过"
fi

echo "=== 3 数据库 ==="
if "$PY" - <<'EOF'
import os, sys
sys.path.insert(0, "src")
try:
    from aterag.config import get_settings
    import psycopg
    s = get_settings()
    with psycopg.connect(s.postgres_dsn) as conn:
        q = conn.execute("SELECT count(*) FROM l0_term.provenance").fetchone()[0]
        c = conn.execute(
            "SELECT count(*) FROM public.aterag_chunks WHERE workspace_id = '_domain_power'"
        ).fetchone()[0]
        d = conn.execute("SELECT count(*) FROM l0_term.provenance WHERE entity_type = 'decision'").fetchone()[0]
    print(f"  [ok]   provenance {q} 行 / _domain_power chunks {c} 条 / 决策谱系 {d} 行")
except Exception as e:  # noqa: BLE001
    print(f"  [FAIL] 数据库核对失败: {e}")
    raise SystemExit(1)
EOF
then :; else fail=$((fail + 1)); fi

echo "=== 4 服务与 MCP ==="
if command -v systemctl >/dev/null; then
    for svc in aterag-mcp aterag-explorer; do
        if systemctl is-active --quiet "$svc"; then ok "$svc active"; else bad "$svc 未 active"; fi
    done
fi

MCP_URL="${MCP_URL:-http://127.0.0.1:8080/mcp}"
SID=$(curl -s -D - -o /dev/null -X POST "$MCP_URL" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}' \
    | tr -d '\r' | sed -n 's/^[Mm]cp-[Ss]ession-[Ii]d: //p')
if [ -z "${SID:-}" ]; then
    bad "MCP initialize 失败 ($MCP_URL)"
else
    curl -s -o /dev/null -X POST "$MCP_URL" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $SID" \
        -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    LIST=$(curl -s -X POST "$MCP_URL" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' -H "Mcp-Session-Id: $SID" \
        -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | sed -n 's/^data: //p')
    N=$(printf '%s' "$LIST" | grep -o '"name":"[a-z_]*"' | wc -l | tr -d ' ')
    if [ "$N" -ge 19 ]; then ok "MCP 工具 $N 个 (>=19)"; else bad "MCP 工具只有 $N 个"; fi
    for need in calculate explain_decision get_decision_provenance search_requirements; do
        printf '%s' "$LIST" | grep -q "\"name\":\"$need\"" \
            && ok "工具在册: $need" || bad "工具缺失: $need"
    done
fi

echo
if [ "$fail" -eq 0 ]; then
    echo "自检全部通过 (0 FAIL)"
    exit 0
fi
echo "自检失败项: $fail"
exit 1