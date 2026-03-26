#!/usr/bin/env bash
# ════════════════════════════════════════════════════════════════════════
#  SimulationTest — HMI 监控面板一键启动脚本
#
#  用法：
#    bash launch_hmi.sh           # 启动服务并打开浏览器
#    bash launch_hmi.sh --stop    # 停止后台服务
#    bash launch_hmi.sh --status  # 查看服务状态
#
#  依赖：python3 + websockets + psutil（已预装）
# ════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SCRIPT="$SCRIPT_DIR/server/monitor_server.py"
PID_FILE="$SCRIPT_DIR/logs/monitor_server.pid"
LOG_FILE="$SCRIPT_DIR/logs/monitor_server.log"
HTTP_PORT=8080
WS_PORT=8765
HMI_URL="http://127.0.0.1:${HTTP_PORT}"

GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'
RED='\033[0;31m';   CYAN='\033[0;36m'; NC='\033[0m'

mkdir -p "$SCRIPT_DIR/logs"

# ── 辅助函数 ────────────────────────────────────────────────────────────

is_server_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
        [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
    else
        return 1
    fi
}

stop_server() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo -e "${YELLOW}[STOP]${NC} Stopping monitor server (PID=$pid)..."
            kill "$pid" 2>/dev/null || true
            sleep 0.5
            kill -9 "$pid" 2>/dev/null || true
            echo -e "${GREEN}[STOP]${NC} Server stopped."
        else
            echo -e "${YELLOW}[STOP]${NC} Server not running."
        fi
        rm -f "$PID_FILE"
    else
        # 兜底：用 pkill
        pkill -f "monitor_server.py" 2>/dev/null && \
            echo -e "${GREEN}[STOP]${NC} Server stopped." || \
            echo -e "${YELLOW}[STOP]${NC} Server not running."
    fi
}

open_browser() {
    local url="$1"
    if command -v open &>/dev/null; then
        open "$url"                  # macOS
    elif command -v xdg-open &>/dev/null; then
        xdg-open "$url" &            # Linux
    elif command -v start &>/dev/null; then
        start "$url"                 # Windows (Git Bash)
    else
        echo -e "${CYAN}[INFO]${NC} 请手动打开浏览器访问: $url"
    fi
}

# ── 命令处理 ────────────────────────────────────────────────────────────

case "${1:-}" in

    --stop)
        stop_server
        exit 0
        ;;

    --status)
        if is_server_running; then
            pid=$(cat "$PID_FILE")
            echo -e "${GREEN}[OK]${NC}  Monitor server running (PID=$pid)"
            echo -e "${CYAN}       HMI: $HMI_URL${NC}"
        else
            echo -e "${RED}[--]${NC}  Monitor server not running"
        fi
        exit 0
        ;;

    --restart)
        stop_server
        sleep 0.5
        ;;

    "")
        # 正常启动
        ;;

    *)
        echo "Usage: $0 [--stop | --status | --restart]"
        exit 1
        ;;
esac

# ── 启动前检查 ──────────────────────────────────────────────────────────

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   SimulationTest — AUTOSAR IPC HMI 监控面板           ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# 检查 python3
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}[ERROR]${NC} python3 not found. Please install Python 3.9+"
    exit 1
fi

# 检查依赖
python3 -c "import websockets, psutil" 2>/dev/null || {
    echo -e "${YELLOW}[INFO]${NC} Installing dependencies..."
    pip3 install websockets psutil --quiet
}

# 如果已在运行，提示
if is_server_running; then
    pid=$(cat "$PID_FILE")
    echo -e "${YELLOW}[INFO]${NC} Server already running (PID=$pid)"
    echo -e "${CYAN}[INFO]${NC} Opening browser: $HMI_URL"
    open_browser "$HMI_URL"
    exit 0
fi

# 检查端口占用
for port in $HTTP_PORT $WS_PORT; do
    if lsof -iTCP:"$port" -sTCP:LISTEN &>/dev/null 2>&1; then
        echo -e "${YELLOW}[WARN]${NC} Port $port in use, trying to free it..."
        lsof -ti tcp:"$port" | xargs kill -9 2>/dev/null || true
        sleep 0.3
    fi
done

# ── 启动服务 ────────────────────────────────────────────────────────────

echo -e "${GREEN}[START]${NC} Starting monitor server..."
nohup python3 "$SERVER_SCRIPT" > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
echo -e "${GREEN}[START]${NC} Server PID=$SERVER_PID"

# 等待服务就绪（最多 5 秒）
echo -n "        Waiting for server"
for i in $(seq 1 20); do
    sleep 0.25
    echo -n "."
    # 检查进程存活
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ""
        echo -e "${RED}[ERROR]${NC} Server crashed! Log output:"
        tail -20 "$LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
    # 检查端口是否就绪
    if lsof -iTCP:"$HTTP_PORT" -sTCP:LISTEN &>/dev/null 2>&1; then
        echo " ready!"
        break
    fi
    if [[ $i -eq 20 ]]; then
        echo " timeout (server may still be starting)"
    fi
done

echo ""
echo -e "${GREEN}[OK]${NC}  ┌─────────────────────────────────────────────┐"
echo -e "${GREEN}[OK]${NC}  │  HMI 面板:  ${CYAN}$HMI_URL${GREEN}          │"
echo -e "${GREEN}[OK]${NC}  │  WebSocket: ws://127.0.0.1:${WS_PORT}                │"
echo -e "${GREEN}[OK]${NC}  │  日志文件:  logs/monitor_server.log          │"
echo -e "${GREEN}[OK]${NC}  └─────────────────────────────────────────────┘"
echo ""
echo -e "${CYAN}[TIPS]${NC} 打开面板后点击 [▶ 启动完整 Demo] 开始通信演示"
echo -e "${CYAN}[TIPS]${NC} 停止服务: bash launch_hmi.sh --stop"
echo ""

# 打开浏览器
sleep 0.5
open_browser "$HMI_URL"

echo -e "${GREEN}[OK]${NC}  浏览器已打开，Ctrl+C 不会停止后台服务"
echo -e "${YELLOW}       后台日志: tail -f $LOG_FILE${NC}"
