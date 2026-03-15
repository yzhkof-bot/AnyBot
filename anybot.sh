#!/bin/bash
# AnyBot 服务管理脚本
# 用法: ./anybot.sh [start|stop|restart|status]

set -e

# 项目根目录（脚本所在目录）
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"
PID_FILE="$PROJECT_DIR/.anybot.pid"
CAFFEINATE_PID_FILE="$PROJECT_DIR/.anybot_caffeinate.pid"
LOG_FILE="$PROJECT_DIR/anybot.log"
PORT=9765

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 获取本机局域网 IP（macOS）
get_local_ip() {
    ipconfig getifaddr en0 2>/dev/null || echo "localhost"
}

# 检查服务是否在运行
is_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        else
            # PID 文件存在但进程已死，清理
            rm -f "$PID_FILE"
        fi
    fi
    return 1
}

# 获取 PID
get_pid() {
    if [ -f "$PID_FILE" ]; then
        cat "$PID_FILE"
    fi
}

# 启动服务
start() {
    if is_running; then
        echo -e "${YELLOW}⚠️  AnyBot 已在运行中 (PID: $(get_pid))${NC}"
        echo -e "   如需重启请使用: $0 restart"
        return 1
    fi

    # 检查虚拟环境
    if [ ! -f "$VENV_PYTHON" ]; then
        echo -e "${RED}❌ 虚拟环境不存在: $VENV_PYTHON${NC}"
        echo "   请先创建: python3 -m venv .venv && .venv/bin/pip install -r server/requirements.txt"
        return 1
    fi

    # 检查端口是否被占用
    if lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  端口 $PORT 已被占用，尝试释放...${NC}"
        lsof -ti :$PORT | xargs kill -9 2>/dev/null || true
        sleep 1
    fi

    local ip
    ip=$(get_local_ip)

    echo -e "${GREEN}🤖 AnyBot 启动中...${NC}"
    cd "$PROJECT_DIR"

    # AI Agent 配置：从项目根目录的 config.json 读取
    # 也可以通过环境变量覆盖：
    #   export ANYBOT_API_KEY=your-key
    #   export ANYBOT_BASE_URL=https://your-proxy.com  （可选，代理地址）
    #   export ANYBOT_MODEL=claude-sonnet-4-20250514    （可选，模型名）
    if [ -f "$PROJECT_DIR/config.json" ]; then
        echo -e "   ${GREEN}✓${NC} 检测到 config.json，AI Agent 将使用该配置"
    else
        echo -e "   ${YELLOW}⚠️${NC} 未找到 config.json，请复制 config.example.json 并填入 API Key"
    fi

    # 后台启动，日志写入文件
    nohup "$VENV_PYTHON" run.py > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo $pid > "$PID_FILE"

    # 启动 caffeinate 阻止系统睡眠（含合盖），随 AnyBot 进程自动退出
    caffeinate -dims -w $pid &
    echo $! > "$CAFFEINATE_PID_FILE"

    # 等待服务就绪
    echo -n "   等待服务就绪"
    for i in $(seq 1 15); do
        if curl -s "http://localhost:$PORT/api/screen-info" >/dev/null 2>&1; then
            echo ""
            echo -e "${GREEN}✅ AnyBot 已启动 (PID: $pid)${NC}"
            echo -e "   📱 局域网访问: ${GREEN}http://$ip:$PORT${NC}"
            echo -e "   📖 API 文档: http://localhost:$PORT/docs"
            echo -e "   📋 日志文件: $LOG_FILE"
            return 0
        fi
        echo -n "."
        sleep 1
    done

    echo ""
    echo -e "${RED}❌ 启动超时，请检查日志: $LOG_FILE${NC}"
    tail -20 "$LOG_FILE"
    return 1
}

# 停止服务
stop() {
    if ! is_running; then
        echo -e "${YELLOW}⚠️  AnyBot 未在运行${NC}"
        # 清理可能残留的端口占用
        if lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
            echo "   发现端口 $PORT 残留进程，清理中..."
            lsof -ti :$PORT | xargs kill -9 2>/dev/null || true
        fi
        return 0
    fi

    local pid
    pid=$(get_pid)
    echo -e "${YELLOW}🛑 正在停止 AnyBot (PID: $pid)...${NC}"

    # 停止 caffeinate 防睡眠进程
    if [ -f "$CAFFEINATE_PID_FILE" ]; then
        local caf_pid
        caf_pid=$(cat "$CAFFEINATE_PID_FILE")
        kill "$caf_pid" 2>/dev/null || true
        rm -f "$CAFFEINATE_PID_FILE"
    fi

    # 优雅关闭（SIGTERM）
    kill "$pid" 2>/dev/null

    # 等待进程退出
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$PID_FILE"
            echo -e "${GREEN}✅ AnyBot 已停止${NC}"
            return 0
        fi
        sleep 0.5
    done

    # 强制关闭
    echo "   优雅关闭超时，强制终止..."
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"

    # 确保端口释放
    lsof -ti :$PORT | xargs kill -9 2>/dev/null || true
    echo -e "${GREEN}✅ AnyBot 已停止（强制）${NC}"
}

# 重启服务
restart() {
    echo -e "${GREEN}🔄 重启 AnyBot...${NC}"
    stop
    sleep 1
    start
}

# 查看状态
status() {
    if is_running; then
        local pid
        pid=$(get_pid)
        echo -e "${GREEN}✅ AnyBot 正在运行${NC}"
        echo -e "   PID: $pid"
        echo -e "   端口: $PORT"
        echo -e "   📱 局域网访问: http://$(get_local_ip):$PORT"

        # 检查 WebRTC 状态
        local webrtc_status
        webrtc_status=$(curl -s "http://localhost:$PORT/api/webrtc/status" 2>/dev/null || echo "")
        if [ -n "$webrtc_status" ]; then
            echo -e "   WebRTC: $webrtc_status"
        fi
    else
        echo -e "${RED}❌ AnyBot 未在运行${NC}"

        # 检查端口是否被其他进程占用
        if lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
            echo -e "   ${YELLOW}⚠️  但端口 $PORT 被其他进程占用${NC}"
            lsof -i :$PORT -sTCP:LISTEN
        fi
    fi
}

# 查看日志
logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo -e "${YELLOW}⚠️  日志文件不存在: $LOG_FILE${NC}"
    fi
}

# 主入口
case "${1:-}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    *)
        echo "🤖 AnyBot 服务管理"
        echo ""
        echo "用法: $0 {start|stop|restart|status|logs}"
        echo ""
        echo "  start    启动服务（后台运行）"
        echo "  stop     停止服务"
        echo "  restart  重启服务"
        echo "  status   查看运行状态"
        echo "  logs     实时查看日志"
        ;;
esac
