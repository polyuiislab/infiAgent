#!/bin/bash
# 启动 Web UI 服务器和工具服务器

# 设置 UTF-8 编码（兼容 macOS）
export LANG=${LANG:-en_US.UTF-8}
export LC_ALL=${LC_ALL:-en_US.UTF-8}

# 获取脚本所在目录（server 目录）
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
# 切换到 server 目录
cd "$SCRIPT_DIR"

# 检查 conda 环境（抑制所有警告和错误信息）
if ! command -v conda &> /dev/null 2>&1; then
    printf "❌ 未找到 conda 命令\n"
    exit 1
fi

# 抑制 conda 相关的警告和错误
export CONDA_AUTO_UPDATE_CONDA=false
export PYTHONWARNINGS="ignore::FutureWarning"
export CONDARC=/dev/null 2>/dev/null

# 激活 conda 环境（如果存在，抑制所有输出）
if conda env list 2>/dev/null | grep -q "paper_agent"; then
    # 初始化 conda（抑制所有输出）
    eval "$(conda shell.bash hook 2>/dev/null)" 2>/dev/null
    if [ -f "$(conda info --base 2>/dev/null)/etc/profile.d/conda.sh" ]; then
        source "$(conda info --base 2>/dev/null)/etc/profile.d/conda.sh" 2>/dev/null
    fi
    # 激活环境（抑制所有输出）
    conda activate paper_agent 2>/dev/null || true
fi

# 检查端口是否被占用
WEB_PORT=${PORT:-22228}

if lsof -ti:$WEB_PORT &> /dev/null; then
    printf "⚠️  Web UI 端口 %s 已被占用\n" "$WEB_PORT"
    printf "💡 使用以下命令停止现有服务器：\n"
    printf "   ./stop.sh\n"
    exit 1
fi

# 读取工具服务器端口配置（从 tool_config.yaml）
PROJECT_ROOT="$( cd "$SCRIPT_DIR/../.." && pwd )"
TOOL_CONFIG_FILE="$PROJECT_ROOT/config/run_env_config/tool_config.yaml"
TOOL_SERVER_PORT=""

if [ -f "$TOOL_CONFIG_FILE" ]; then
    # 使用 Python 解析 YAML 中的端口（更可靠）
    TOOL_SERVER_URL=$(python3 -c "import yaml, sys; config = yaml.safe_load(open('$TOOL_CONFIG_FILE')); print(config.get('tools_server', 'http://127.0.0.1:8001/'))" 2>/dev/null)
    
    if [ -n "$TOOL_SERVER_URL" ]; then
        # 提取端口号（从 URL 中提取，例如 http://127.0.0.1:8002/）
        TOOL_SERVER_PORT=$(echo "$TOOL_SERVER_URL" | sed -n 's/.*:\([0-9]*\)\/.*/\1/p')
    fi
fi

# 如果读取失败，使用默认端口 8001
TOOL_SERVER_PORT=${TOOL_SERVER_PORT:-8001}

# 启动工具服务器（如果未运行）
printf "🔧 检查工具服务器（端口: %s）...\n" "$TOOL_SERVER_PORT"

# 先清理可能存在的僵尸进程（端口被占用但无法连接）
if lsof -ti:$TOOL_SERVER_PORT > /dev/null 2>&1; then
    # 检查端口是否有响应
    if ! curl -s "http://127.0.0.1:$TOOL_SERVER_PORT/health" > /dev/null 2>&1; then
        printf "   🧹 清理占用端口 %s 的僵尸进程...\n" "$TOOL_SERVER_PORT"
        kill -9 $(lsof -ti:$TOOL_SERVER_PORT) 2>/dev/null || true
        sleep 1
    fi
fi

# 检查工具服务器是否已在运行（通过 health endpoint）
if curl -s "http://127.0.0.1:$TOOL_SERVER_PORT/health" > /dev/null 2>&1; then
    printf "   ✅ 工具服务器已在运行（端口: %s）\n" "$TOOL_SERVER_PORT"
    else
    # 尝试启动工具服务器
    printf "   🚀 启动工具服务器（端口: %s）...\n" "$TOOL_SERVER_PORT"
    
    TOOL_SERVER_DIR="$PROJECT_ROOT/tool_server_lite"
    SERVER_PY="$TOOL_SERVER_DIR/server.py"
    
    # 优先使用 mla-tool-server 命令
    if command -v mla-tool-server > /dev/null 2>&1; then
        if mla-tool-server start --port "$TOOL_SERVER_PORT" 2>/dev/null; then
            printf "   ✅ 工具服务器已启动（端口: %s）\n" "$TOOL_SERVER_PORT"
            sleep 3
        else
            printf "   ⚠️  mla-tool-server 启动失败，尝试直接启动...\n"
            _start_tool_server_direct "$TOOL_SERVER_PORT"
        fi
    elif [ -f "$SERVER_PY" ]; then
        # 直接启动 server.py
        _start_tool_server_direct "$TOOL_SERVER_PORT"
    else
        printf "   ⚠️  未找到工具服务器文件: %s\n" "$SERVER_PY"
        printf "   💡 请确保项目目录正确\n"
        printf "   💡 但继续启动 Web UI...\n"
    fi
fi

# 辅助函数：直接启动工具服务器
_start_tool_server_direct() {
    local port=$1
    local log_file="$TOOL_SERVER_DIR/tool_server.log"
    
    # 后台启动工具服务器
    cd "$TOOL_SERVER_DIR"
    nohup python3 server.py --port "$port" > "$log_file" 2>&1 &
    local pid=$!
    cd "$SCRIPT_DIR"
    
    # 等待服务器启动（最多15秒）
    local retries=15
    while [ $retries -gt 0 ]; do
        sleep 1
        if curl -s "http://127.0.0.1:$port/health" > /dev/null 2>&1; then
            printf "   ✅ 工具服务器已启动（PID: %s, 端口: %s）\n" "$pid" "$port"
            return 0
        fi
        retries=$((retries - 1))
    done
    
    printf "   ⚠️  工具服务器启动超时\n"
    printf "   💡 请查看日志: %s\n" "$log_file"
    printf "   💡 但继续启动 Web UI...\n"
    return 1
}

# 询问用户 workspace 路径
printf "\n"
printf "📂 设置工作空间路径（Workspace Root）\n"
printf "   💡 直接回车将使用当前目录作为工作空间（与 CLI 模式相同）\n"
printf "   💡 输入绝对路径可指定自定义工作空间\n"
read -p "   请输入工作空间路径 (回车使用当前目录): " workspace_input

# 处理用户输入
if [ -z "$workspace_input" ]; then
    # 用户直接回车，使用当前工作目录（和 CLI 模式一样）
    WORKSPACE_ROOT=$(pwd)
    printf "   ✅ 使用当前目录作为工作空间: %s\n" "$WORKSPACE_ROOT"
else
    # 用户输入了路径
    workspace_input=$(printf "%s" "$workspace_input" | xargs)  # 去除首尾空格
    if [ -z "$workspace_input" ]; then
        # 输入全是空格，当作回车处理
        WORKSPACE_ROOT=$(pwd)
        printf "   ✅ 使用当前目录作为工作空间: %s\n" "$WORKSPACE_ROOT"
    else
        # 检查路径是否存在或是有效路径
        if [ -d "$workspace_input" ] || mkdir -p "$workspace_input" 2>/dev/null; then
            # 转换为绝对路径
            WORKSPACE_ROOT=$(cd "$workspace_input" 2>/dev/null && pwd || printf "%s" "$workspace_input")
            printf "   ✅ 使用指定工作空间: %s\n" "$WORKSPACE_ROOT"
        else
            # 路径无效，使用当前目录
            WORKSPACE_ROOT=$(pwd)
            printf "   ⚠️  输入路径无效，使用当前目录作为工作空间: %s\n" "$WORKSPACE_ROOT"
        fi
    fi
fi

# 启动 Web UI 服务器
printf "\n"
printf "🚀 启动 Web UI 服务器...\n"
printf "📂 服务器工作目录: %s\n" "$SCRIPT_DIR"
printf "📂 用户工作空间: %s\n" "$WORKSPACE_ROOT"
printf "🌐 Web UI 地址: http://localhost:%s\n" "$WEB_PORT"
printf "\n"
printf "💡 提示: 使用 Ctrl+C 停止服务器，或运行 ./stop.sh\n"
printf "\n"

WORKSPACE_ROOT="$WORKSPACE_ROOT" PORT=$WEB_PORT python server.py

