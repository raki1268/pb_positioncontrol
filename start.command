#!/bin/zsh
# Double-click launcher: starts the RSD tunnel + server.py, auto-fills the RSD
# address into the web UI, and opens the browser. Close the window or press
# Ctrl+C to stop both.

PROJ="$(cd "$(dirname "$0")" && pwd)"
PY="$PROJ/venv/bin/python3"
LOGDIR="${TMPDIR:-/tmp}/pikmin-launcher"
TUNNEL_LOG="$LOGDIR/tunnel.log"
SERVER_LOG="$LOGDIR/server.log"

if [[ ! -x "$PY" ]]; then
  echo "❌ 找不到 $PY"
  echo "   请先按 README 的 Setup 步骤创建 venv"
  read -k1 "?按任意键关闭…"
  exit 1
fi

mkdir -p "$LOGDIR"
: > "$TUNNEL_LOG"
: > "$SERVER_LOG"

echo "🔑 需要管理员密码（只输一次）"
sudo -v || exit 1
# 保持 sudo 授权不过期
( while true; do sudo -n true; sleep 50; done ) 2>/dev/null &
KEEPALIVE=$!

TUNNEL_PID=""
SERVER_PID=""
TAIL_PID=""
cleanup() {
  echo "\n🛑 正在关闭…"
  [[ -n "$TAIL_PID" ]] && kill "$TAIL_PID" 2>/dev/null
  [[ -n "$SERVER_PID" ]] && sudo kill "$SERVER_PID" 2>/dev/null
  [[ -n "$TUNNEL_PID" ]] && sudo kill "$TUNNEL_PID" 2>/dev/null
  kill "$KEEPALIVE" 2>/dev/null
  exit 0
}
trap cleanup INT TERM HUP

cd "$PROJ" || exit 1

# ① Tunnel
echo "🔌 启动 tunnel（手机请用线连好并解锁）…"
sudo "$PY" -u -m pymobiledevice3 remote start-tunnel --connection-type usb >"$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

RSD_HOST=""; RSD_PORT=""
for i in {1..60}; do
  RSD_HOST=$(grep -m1 -i "RSD Address" "$TUNNEL_LOG" | sed -E 's/.*[Aa]ddress[^0-9a-fA-F]*([0-9a-fA-F:.]+).*/\1/')
  RSD_PORT=$(grep -m1 -i "RSD Port" "$TUNNEL_LOG" | sed -E 's/.*[Pp]ort[^0-9]*([0-9]+).*/\1/')
  [[ -n "$RSD_HOST" && -n "$RSD_PORT" ]] && break
  if ! sudo kill -0 "$TUNNEL_PID" 2>/dev/null; then break; fi
  sleep 1
done

if [[ -z "$RSD_HOST" || -z "$RSD_PORT" ]]; then
  echo "❌ Tunnel 没起来，输出如下："
  cat "$TUNNEL_LOG"
  read -k1 "?按任意键关闭…"
  cleanup
fi
echo "✅ Tunnel: $RSD_HOST  $RSD_PORT"

# ② Server
echo "🌱 启动 server…"
sudo "$PY" -u server.py >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

URL=""
for i in {1..30}; do
  URL=$(grep -m1 -oE "http://127\.0\.0\.1:[0-9]+" "$SERVER_LOG")
  [[ -n "$URL" ]] && curl -s -o /dev/null "$URL/api/status" && break
  if ! sudo kill -0 "$SERVER_PID" 2>/dev/null; then URL=""; break; fi
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "❌ Server 没起来，输出如下："
  cat "$SERVER_LOG"
  read -k1 "?按任意键关闭…"
  cleanup
fi

# ③ 自动填 RSD，省得手动粘贴
curl -s -X POST "$URL/api/setup/rsd" -H 'Content-Type: application/json' \
  -d "{\"host\":\"$RSD_HOST\",\"port\":$RSD_PORT}" >/dev/null \
  && echo "✅ RSD 已自动填入"

open "$URL"
echo "\n🎉 已就绪 → $URL"
echo "   关闭此窗口或按 Ctrl+C 即可全部结束。\n"

# 实时显示两边日志
tail -n +1 -f "$SERVER_LOG" "$TUNNEL_LOG" &
TAIL_PID=$!
wait "$SERVER_PID"
cleanup
