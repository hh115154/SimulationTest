#!/usr/bin/env bash
# SimulationTest — 一键启动监控面板
# 用法：
#   ./scripts/start_monitor.sh           # 启动服务，打开浏览器
#   ./scripts/start_monitor.sh --build   # 先重新编译 AP/CP，再启动
#   ./scripts/start_monitor.sh --stop    # 停止所有进程
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKBUDDY="$(cd "$ROOT/.." && pwd)"

AP_DIR="$WORKBUDDY/MyAutoSarAp"
CP_DIR="$WORKBUDDY/MyAutoSarCP"
SERVER_PY="$ROOT/server/monitor_server.py"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── 停止模式 ──
if [[ "${1:-}" == "--stop" ]]; then
  info "Stopping monitor server and AP/CP processes..."
  pkill -f "monitor_server.py" 2>/dev/null && ok "Monitor server stopped" || warn "Not running"
  pkill -f "MyAutoSarAp"       2>/dev/null && ok "AP stopped"              || warn "AP not running"
  pkill -f "MyAutoSarCP"       2>/dev/null && ok "CP stopped"              || warn "CP not running"
  exit 0
fi

echo ""
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  SimulationTest — AUTOSAR IPC 可视化监控面板${NC}"
echo -e "${CYAN}════════════════════════════════════════════════════${NC}"
echo ""

# ── 编译模式 ──
if [[ "${1:-}" == "--build" ]]; then
  info "Building MyAutoSarAp..."
  (cd "$AP_DIR" && arch -arm64 cmake -S . -B build_someip \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_C_COMPILER=clang \
    -DBUILD_TESTS=OFF 2>&1 | tail -3 \
   && arch -arm64 cmake --build build_someip --parallel 4 2>&1 | grep -E "(Built target|error:)" | tail -5)
  ok "MyAutoSarAp built"

  info "Building MyAutoSarCP..."
  (cd "$CP_DIR" && arch -arm64 cmake -S . -B build_someip \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DCMAKE_C_COMPILER=clang \
    -DCMAKE_CXX_COMPILER=clang++ 2>&1 | tail -3 \
   && arch -arm64 cmake --build build_someip --parallel 4 2>&1 | grep -E "(Built target|error:)" | tail -5)
  ok "MyAutoSarCP built"
fi

# ── 检查二进制存在 ──
AP_BIN="$AP_DIR/build_someip/src/application/MyAutoSarAp"
CP_BIN="$CP_DIR/build_someip/MyAutoSarCP"

if [[ ! -f "$AP_BIN" ]]; then
  warn "AP binary not found at $AP_BIN"
  warn "Run: ./scripts/start_monitor.sh --build"
fi
if [[ ! -f "$CP_BIN" ]]; then
  warn "CP binary not found at $CP_BIN"
  warn "Run: ./scripts/start_monitor.sh --build"
fi

# ── 检查 Python 依赖 ──
if ! python3 -c "import websockets, psutil" 2>/dev/null; then
  info "Installing Python dependencies..."
  pip3 install websockets psutil --quiet
fi

# ── 停止已有实例 ──
pkill -f "monitor_server.py" 2>/dev/null || true
sleep 0.2

# ── 启动监控服务 ──
info "Starting monitor server..."
python3 "$SERVER_PY" > "$LOG_DIR/monitor.log" 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > "$ROOT/.server.pid"
sleep 1.2

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  error "Monitor server failed to start. Check $LOG_DIR/monitor.log"
  cat "$LOG_DIR/monitor.log"
  exit 1
fi

ok "Monitor server started (PID=$SERVER_PID)"
echo ""
echo -e "${GREEN}  ✓ 监控面板地址：${CYAN}http://127.0.0.1:8080${NC}"
echo -e "${GREEN}  ✓ WebSocket 地址：${CYAN}ws://127.0.0.1:8765${NC}"
echo ""
echo -e "${YELLOW}  在面板上点击 [▶ 启动完整 Demo] 即可自动启动 AP+CP 进程${NC}"
echo ""

# ── 打开浏览器 ──
if command -v open &>/dev/null; then
  sleep 0.5 && open "http://127.0.0.1:8080" &
elif command -v xdg-open &>/dev/null; then
  sleep 0.5 && xdg-open "http://127.0.0.1:8080" &
fi

info "Press Ctrl+C to stop all..."
# 追踪日志
tail -f "$LOG_DIR/monitor.log" &
TAIL_PID=$!

trap "kill $SERVER_PID $TAIL_PID 2>/dev/null; pkill -f MyAutoSarAp 2>/dev/null; pkill -f MyAutoSarCP 2>/dev/null; exit 0" INT TERM
wait $SERVER_PID
