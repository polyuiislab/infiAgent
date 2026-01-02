# MLA-V3 Web UI

这是一个简单的 Web 前端界面，用于与 MLA-V3 框架进行交互。

## 功能特性

- 🎨 现代化的对话界面
- 🤖 显示当前执行的 Agent（带头像）
- 📂 显示 Task ID 和 Workspace 路径
- 📊 实时流式输出（JSONL 事件流）
- 🔔 **Human-in-Loop (HIL) 交互支持**：自动检测并响应 Agent 的人类交互任务
- 💬 支持多行输入和 Enter 发送
- 💾 对话历史自动保存到 `task_id/conversations/` 目录
- 📁 集成文件浏览器，可查看和管理任务文件

## 安装依赖

```bash
pip install flask flask-cors
```

或者添加到 `requirements.txt`：

```
flask
flask-cors
```

## 启动方式

### 方法 1：使用便捷脚本（推荐）

**注意**：启动脚本会自动启动工具服务器（tool_server_lite），无需手动启动。首次运行时会询问您设置工作空间路径（Workspace Root）。

1. 启动服务器（会自动启动 Web UI 和工具服务器）：
   - 首次运行时会提示输入工作空间路径
   - 直接回车将使用当前目录作为工作空间（与 CLI 模式相同）
   - 或输入绝对路径指定自定义工作空间
   ```bash
   cd web_ui/server
   ./start.sh
   ```
   或者使用统一管理脚本：
   ```bash
   cd web_ui/server
   ./server start
   ```

2. 停止服务器（会同时停止 Web UI 和工具服务器）：
   ```bash
   cd web_ui/server
   ./stop.sh
   ```
   或者：
   ```bash
   cd web_ui/server
   ./server stop
   ```

3. 查看服务器状态：
   ```bash
   cd web_ui/server
   ./server status
   ```
   会显示 Web UI 和工具服务器的运行状态。

4. 重启服务器：
   ```bash
   cd web_ui/server
   ./server restart
   ```

5. 打开浏览器访问：
   ```
   http://localhost:22228
   ```
   
   **服务器地址**：
   - Web UI: http://localhost:22228
   - 工具服务器 API: http://localhost:24243
   - 工具服务器文档: http://localhost:24243/docs

### 方法 2：直接运行 Python（传统方式）

**注意**：如果使用此方法，需要手动启动工具服务器。

1. 启动工具服务器（在一个终端）：
   ```bash
   cd tool_server_lite
   python server.py
   ```

2. 启动 Web UI 服务器（在另一个终端）：
   ```bash
   cd web_ui/server
   python server.py
   ```

3. 打开浏览器访问：
   ```
   http://localhost:22228
   ```

### 端口配置

- **Web UI 默认端口**: 22228（因为 macOS 的 AirPlay 可能占用 5000 端口）
- **工具服务器默认端口**: 24243
- 可以通过环境变量指定其他端口：
  ```bash
  cd web_ui/server
  PORT=8080 TOOL_PORT=8002 ./start.sh
  # 或
  PORT=8080 TOOL_PORT=8002 ./server start
  ```

## 使用说明

### 1. 设置 Task ID

在顶部的 "Task ID" 输入框中输入任务目录的绝对路径，例如：
```
/mla_task
```
或
```
/Users/username/Desktop/my_project
```

### 2. Agent 配置

- 当前版本固定使用 `alpha_agent` 和 `Default` 系统
- 无需手动选择，系统会自动使用正确的配置

### 3. 输入任务

在底部的输入框中输入任务描述，例如：
```
帮我找一篇关于 ECM 的论文
```

然后点击 "发送" 按钮或按 Enter 键（Shift+Enter 换行）。

### 4. 查看输出

Agent 的执行输出会实时显示在对话窗口中：
- 每条消息显示 Agent 头像和名称
- 不同类型的消息有不同的颜色：
  - 🔧 工具调用（tool_call）：青色
  - 🤖 子 Agent 调用（agent_call）：蓝色
  - ✅ 成功消息：青色
  - ❌ 错误消息：红色
  - ⚠️ 警告消息：黄色
  - 📊 结果消息：橙色
  - 💭 思考消息：紫色

### 5. Human-in-Loop (HIL) 交互

当 Agent 需要人工输入时：
- 输入框会自动显示红色闪烁效果
- 输入框 placeholder 会显示 Agent 的指令
- Send 按钮自动启用（即使输入框为空）
- 在输入框中输入响应并点击 Send
- Agent 会继续执行任务

**工作流程**：
1. Agent 调用 `human_in_loop` 工具
2. 前端自动检测到 HIL 任务
3. 输入框显示红色闪烁提示
4. 用户输入响应并发送
5. Agent 继续执行

## 界面说明

### 顶部控制栏
- **Task ID**: 任务工作目录路径（支持任务选择下拉框）
- **Agent**: 固定使用 `alpha_agent`
- **System**: 固定使用 `Default` 系统
- **文件浏览器**: 右侧可浏览和管理任务文件

### 对话窗口
- 显示所有消息（用户输入和 Agent 输出）
- 自动滚动到最新消息
- 支持长文本和多行显示

### 输入区域
- 文本输入框（支持多行）
  - 正常状态：蓝色边框
  - HIL 等待状态：红色闪烁边框
- 发送按钮
  - 有内容时自动启用
  - HIL 模式下始终启用
- 状态栏（显示运行状态和 Workspace 路径）

## 技术架构

- **后端**: Flask + Server-Sent Events (SSE)
- **前端**: 原生 HTML + CSS + JavaScript
- **事件流**: 直接解析 JSONL 事件流（`--jsonl` 模式）
- **流式传输**: 使用 SSE 实现实时输出
- **HIL 支持**: 事件触发 + 智能轮询检测机制
- **数据存储**: 对话历史存储在 `task_id/conversations/` 目录

## 文件结构

```
web_ui/
├── server/                    # 服务器相关文件
│   ├── server.py              # Flask 后端服务器
│   ├── start.sh               # 启动脚本（支持 workspace 配置）
│   ├── stop.sh                # 停止脚本
│   └── users.yaml             # 用户认证配置
├── index.html                 # 前端页面
├── login.html                 # 登录页面
├── static/                    # 静态资源
│   ├── style.css             # 样式文件
│   └── app.js                # JavaScript 逻辑（包含 HIL 检测）
├── requirements.txt           # Python 依赖
└── README.md                 # 使用说明
```

## 数据存储

### 存储位置

系统使用两个存储位置来管理不同类别的数据：

#### 1. Workspace 目录 (`{task_id}/`)

任务工作目录，存储任务相关的工作文件：

```
{task_id}/
├── temp/                      # 临时文件目录
├── code_run/                  # 代码执行目录
├── code_env/                  # 代码环境目录
├── reference.bib              # 参考文件
├── chat_history.json          # Web UI 聊天记录（前端显示）
└── latest_output.json         # 最新输出（用于快速预览）
```

**注意**：删除 `task_id` 目录会删除所有任务相关的工作文件和 Web UI 聊天记录。

#### 2. 主目录 (`~/mla_v3/conversations/`)

Agent 对话历史和状态存储位置：

```
~/mla_v3/conversations/
├── {task_hash}_{task_folder}_stack.json           # Agent 调用栈
├── {task_hash}_{task_folder}_share_context.json   # 共享上下文
└── {task_hash}_{task_folder}_{agent_id}_actions.json  # Agent 动作历史
```

其中：
- `task_hash`: task_id 的 MD5 前 8 位
- `task_folder`: 如果 task_id 是路径，取最后一级文件夹名；否则使用 task_id 本身
- `agent_id`: Agent 的唯一标识符

**注意**：删除此目录会删除所有任务的 Agent 对话历史。如果要清理特定任务的历史，可以根据文件名前缀（`{task_hash}_{task_folder}`）来识别和删除。

## 故障排除

### 1. 无法连接到服务器
- 检查服务器是否正在运行
- 检查端口 5000 是否被占用
- 查看服务器终端的错误信息

### 2. Agent 执行失败
- 检查 Task ID 路径是否正确
- 检查工具服务器是否正常运行
- 查看浏览器控制台的错误信息

### 3. 输出不显示
- 检查浏览器控制台是否有 JavaScript 错误
- 检查 SSE 连接是否正常（Network 标签页）
- 查看服务器日志

### 4. HIL 任务无响应
- 确认工具服务器正在运行（检查端口 8001/8002）
- 检查浏览器控制台是否有错误信息
- 刷新页面重试

### 5. 工作空间路径问题
- 确保路径是绝对路径
- 检查路径是否有写权限
- 重新运行 `start.sh` 配置正确的 workspace

## 开发说明

### 修改端口

在 `server.py` 中修改：
```python
port = int(os.environ.get('PORT', 5000))
```

或通过环境变量：
```bash
PORT=8080 python server.py
```

### 添加新的 Agent 头像

在 `app.js` 中的 `agentAvatars` 对象中添加：
```javascript
const agentAvatars = {
    'your_agent': '🎯',
    // ...
};
```

### 自定义样式

修改 `static/style.css` 文件来自定义界面样式。

## 许可证

与主项目相同。

