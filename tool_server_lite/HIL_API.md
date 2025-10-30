# Human-in-Loop API 说明文档

Tool Server Lite 的人机交互 API，用于实现 VS Code 插件的协作功能。

**服务器**: http://localhost:8001  
**版本**: 1.0.0

---

## 核心概念

### HIL 任务
- **hil_id**: 唯一标识符，用于追踪和完成任务
- **异步挂起**: Agent 调用后阻塞等待，但不阻塞服务器其他请求
- **无限等待**: 默认 `timeout=null`，可选设置超时

### 工作流程
```
1. Agent 调用 human_in_loop 工具 → 阻塞等待
2. HIL 任务注册到服务器（状态: waiting）
3. VS Code 插件查询 HIL 任务列表
4. 插件显示 UI，用户操作
5. 插件调用完成 API
6. Agent 收到信号，继续执行
```

---

## API 端点

### 1. 执行 human_in_loop 工具

**端点**: `POST /api/tool/execute`

**请求**:
```json
{
  "task_id": "/absolute/path/to/workspace",
  "tool_name": "human_in_loop",
  "params": {
    "hil_id": "HIL-unique-id",
    "instruction": "给用户的任务说明",
    "timeout": null
  }
}
```

**参数说明**:
- `hil_id` (str, 必需): 唯一 ID，建议格式 `HIL-{uuid}`
- `instruction` (str, 必需): 任务描述，告诉用户要做什么
- `timeout` (int, 可选): 超时秒数，`null` 表示无限等待

**响应**（阻塞直到完成）:
```json
{
  "success": true,
  "data": {
    "status": "success",
    "output": "人类任务已完成: {用户提交的结果}",
    "error": ""
  }
}
```

**示例**:
```bash
curl -X POST http://localhost:8001/api/tool/execute \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "/path",
    "tool_name": "human_in_loop",
    "params": {
      "hil_id": "HIL-001",
      "instruction": "请上传数据文件到 upload 目录"
    }
  }'
# 此请求会挂起，直到调用完成 API
```

---

### 2. 查看所有 HIL 任务

**端点**: `GET /api/hil/tasks`

**响应**:
```json
{
  "total": 2,
  "tasks": [
    {
      "hil_id": "HIL-001",
      "status": "waiting",
      "instruction": "请上传文件",
      "task_id": "/path"
    },
    {
      "hil_id": "HIL-002",
      "status": "waiting",
      "instruction": "请确认结果",
      "task_id": "/path2"
    }
  ]
}
```

**示例**:
```bash
curl http://localhost:8001/api/hil/tasks | jq
```

---

### 3. 查看特定 HIL 任务状态

**端点**: `GET /api/hil/{hil_id}`

**响应**:
```json
{
  "found": true,
  "hil_id": "HIL-001",
  "status": "waiting",
  "instruction": "请上传文件到 upload 目录",
  "task_id": "/absolute/path"
}
```

**状态值**:
- `waiting`: 等待中
- `completed`: 已完成
- `cancelled`: 已取消
- `timeout`: 超时

**示例**:
```bash
curl http://localhost:8001/api/hil/HIL-001 | jq
```

---

### 4. 完成 HIL 任务 ⭐

**端点**: `POST /api/hil/complete/{hil_id}`

**请求 Body**:
```json
{
  "result": "用户操作的结果描述"
}
```

**响应**:
```json
{
  "success": true,
  "message": "HIL task {hil_id} marked as completed"
}
```

**示例**:
```bash
curl -X POST http://localhost:8001/api/hil/complete/HIL-001 \
  -H "Content-Type: application/json" \
  -d '{"result": "文件已上传至 upload/data.csv"}'
```

**效果**: 
- HIL 任务状态标记为 `completed`
- 阻塞的 `human_in_loop` 工具调用立即返回成功
- Agent 收到消息：`✅ 用户已确认: {result}`
- Agent 继续执行后续步骤

---

### 5. 取消 HIL 任务 ⏭️

**端点**: `POST /api/hil/cancel/{hil_id}`

**请求 Body**:
```json
{
  "reason": "取消原因（可选）"
}
```

**响应**:
```json
{
  "success": true,
  "message": "HIL task {hil_id} marked as cancelled"
}
```

**示例**:
```bash
curl -X POST http://localhost:8001/api/hil/cancel/HIL-001 \
  -H "Content-Type: application/json" \
  -d '{"reason": "用户不需要此功能"}'
```

**效果**: 
- HIL 任务状态标记为 `cancelled`
- 阻塞的 `human_in_loop` 工具调用立即返回成功
- Agent 收到消息：`⏭️ 用户已取消: {reason}`
- Agent 可以根据取消信息采取替代方案或跳过该步骤

**命令行快捷方式**:
```bash
# 使用 mla-agent 命令
mla-agent cancel HIL-001 --reason "不需要此功能"
```

---

## VS Code 插件集成示例

### TypeScript 代码

```typescript
import { spawn } from 'child_process';
import axios from 'axios';

interface HILEvent {
  type: 'human_in_loop';
  call_id: string;
  hil_id: string;
  title: string;
  message: string;
  ui: any;
  timeout_sec: number;
  resume_hint?: string;
}

// 启动 Agent
function startAgent(taskId: string, userInput: string) {
  const child = spawn('python3', [
    'start.py',
    '--task_id', taskId,
    '--user_input', userInput,
    '--agent_name', 'writing_agent',
    '--jsonl'
  ]);
  
  child.stdout.on('data', (data) => {
    const lines = data.toString().split('\n');
    lines.forEach(line => {
      if (!line.trim()) return;
      
      try {
        const event = JSON.parse(line);
        handleEvent(event);
      } catch (e) {
        console.log(line); // 非 JSON 行
      }
    });
  });
}

// 处理事件
async function handleEvent(event: any) {
  switch (event.type) {
    case 'human_in_loop':
      await handleHIL(event as HILEvent);
      break;
    case 'token':
      appendToChat(event.text);
      break;
    case 'result':
      showResult(event);
      break;
  }
}

// 处理 HIL
async function handleHIL(event: HILEvent) {
  const { hil_id, title, message, ui } = event;
  
  // 显示 UI
  const userInput = await vscode.window.showInputBox({
    prompt: message,
    placeHolder: title
  });
  
  // 完成 HIL 任务
  await axios.post(`http://localhost:8001/api/hil/complete/${hil_id}`, {
    result: userInput || '取消'
  });
}
```

---

## Python 客户端示例

```python
import requests
import threading
import time

def call_human_in_loop(task_id, hil_id, instruction):
    """
    在后台线程调用 human_in_loop
    主线程可以继续处理其他事情
    """
    def worker():
        response = requests.post(
            'http://localhost:8001/api/tool/execute',
            json={
                'task_id': task_id,
                'tool_name': 'human_in_loop',
                'params': {
                    'hil_id': hil_id,
                    'instruction': instruction
                }
            }
        )
        print(f"HIL 完成: {response.json()}")
    
    thread = threading.Thread(target=worker)
    thread.start()
    return thread

# 启动 HIL 任务（后台）
hil_thread = call_human_in_loop(
    '/path/to/workspace',
    'HIL-test-001',
    '请上传文件到 upload 目录'
)

# 主线程继续做其他事情
print("主线程继续运行...")
time.sleep(5)

# 查看 HIL 状态
status = requests.get('http://localhost:8001/api/hil/HIL-test-001').json()
print(f"HIL 状态: {status}")

# 完成 HIL 任务
requests.post(
    'http://localhost:8001/api/hil/complete/HIL-test-001',
    json={'result': '文件已上传'}
)

# 等待 HIL 线程结束
hil_thread.join()
```

---

## 最佳实践

### 1. hil_id 命名
```python
import uuid
hil_id = f"HIL-{uuid.uuid4().hex[:8]}"
```

### 2. 超时设置
```python
# 文件上传 - 长超时
timeout = 3600  # 1小时

# 简单确认 - 短超时
timeout = 300  # 5分钟

# 无限等待
timeout = None
```

### 3. UI 类型选择
```python
# 确认类
ui = {"type": "confirm"}

# 文本输入
ui = {"type": "text", "placeholder": "请输入分支名"}

# 文件选择
ui = {"type": "file_pick", "pattern": "**/*.csv"}
```

### 4. 错误处理
```python
try:
    response = requests.post(...)
    if response.status_code == 200:
        result = response.json()
        if result.get('success'):
            print("HIL 完成")
except requests.Timeout:
    print("HIL 超时")
except Exception as e:
    print(f"HIL 错误: {e}")
```

---

## 安全考虑

1. **hil_id 唯一性**: 避免冲突，使用 UUID
2. **超时设置**: 防止永久阻塞
3. **结果验证**: 检查用户输入的合法性
4. **幂等性**: 重复完成同一 HIL 只生效一次

---

## 附录：完整事件示例

```jsonl
{"type":"start","call_id":"c-1697801234-abc123","project":"/path","agent":"writing_agent","task":"写文章"}
{"type":"token","call_id":"c-1697801234-abc123","text":"开始收集资料..."}
{"type":"progress","call_id":"c-1697801234-abc123","phase":"collect","pct":20}
{"type":"human_in_loop","call_id":"c-1697801234-abc123","hil_id":"HIL-001","title":"请确认参考文献","message":"已找到10篇论文，是否继续？","ui":{"type":"confirm"},"timeout_sec":1800}
{"type":"notice","call_id":"c-1697801234-abc123","text":"等待用户确认..."}
{"type":"token","call_id":"c-1697801234-abc123","text":"用户已确认，继续处理..."}
{"type":"artifact","call_id":"c-1697801234-abc123","kind":"file","path":"upload/references.md","summary":"参考文献列表"}
{"type":"result","call_id":"c-1697801234-abc123","ok":true,"summary":"文章已生成","artifacts":["upload/article.md"]}
{"type":"end","call_id":"c-1697801234-abc123","status":"ok","duration_ms":125430}
```

---

**HIL API 文档完成！用于 VS Code 插件的人机协作集成。**

