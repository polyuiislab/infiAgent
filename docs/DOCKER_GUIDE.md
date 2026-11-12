# Docker 与开发运行界面

## 启动

```bash
mkdir -p user_root
docker run -d --name infiagent \
  -p 127.0.0.1:4260:4260 \
  -v "$PWD/user_root:/data/user_root" \
  chenglinhku/mlav3:3.13.0
```

打开 http://localhost:4260。镜像支持 amd64 和 arm64，Docker 会选择与宿主匹配的架构。

也可运行 `docker compose up -d`。程序、浏览器运行依赖、文档工具和开发界面随镜像提供；持久数据写入挂载目录。

## 配置模型

点击黄色运行页面左侧的 **模型 API 配置**。

| 类型 | 用途 |
|---|---|
| 主执行模型 | Agent 的主要决策与工具调用 |
| 思考模型 | Thinking 模式的规划与复盘 |
| 上下文压缩 | 长任务的上下文压缩 |
| 读图模型 | 图片理解与文字分析 |
| 文生图模型 | 生成图片 |

默认 API 地址与密钥可供各类模型共用。取消某一行的“使用默认 API 地址和密钥”，即可填写独立连接。已保存的密钥不回填到表单；留空保持原值。修改 API 地址时，需要重新填写对应密钥。

模型名可以带供应商前缀，例如 `openai/your-model`；普通名称按 OpenAI 兼容接口处理。文生图接口可选择自动识别、图片生成接口或聊天多模态接口。请使用支持相应用途的模型，文字模型不一定能读图或生成图片。

“主模型直接接收图片”控制 `image_read` 是否把图片交给主执行模型；关闭时，由读图模型先返回文字分析。高级区域可以编辑完整的 `llm_config.yaml`。保存前结束或停止正在运行的任务，保存后下一轮运行生效；智能体 YAML 中明确指定的模型优先于角色默认值。

## 运行会话

1. 选择 **Agent System** 与 **Agent Name**。
2. 点击 **New Task** 或填写 Task ID 后点击 **Ensure**。
3. 输入目标，点击 **Stream Send**，查看模型、工具和子智能体的过程。
4. 运行中可使用 **Queue Message** 追加指令。
5. **Stop / Pause** 请求停止；**Resume** 继续中断的任务。
6. **Reload History** 重新读取记录，**Snapshot** 查看当前状态。

运行中的工具可能需要完成当前调用后才能停止。执行结果不明的副作用操作会报告 `OUTCOME_UNKNOWN`，应先检查产物或外部状态，再决定是否重新执行。

**Advanced** 提供运行方式、动作窗口、轮数和请求参数等设置。配置错误和模型返回错误会显示在 API Output 或运行记录中。

## 工作区与文件

新会话可以创建或加入本地工作区。工作区中的会话共享工程文件，各自保存运行状态。默认按顺序运行同一工作区的会话；也可选择并发模式，使用文件工具的互斥保护。并发工作区中的其他工具需要声明 `workspace_compatible: true`，开发者应确认其并发行为。

右侧文件面板显示工作文件；共享工作区的会话还会显示本会话的记录目录。可上传、下载和删除工作文件。Docker 能访问的是挂载到容器中的目录，当前挂载入口为 `/data/user_root`。

**Upload User Root** 可导入配置、智能体、工具和资源，支持覆盖同名文件或跳过同名文件；已有会话和运行记录保留。导入前停止正在运行的任务。

## 用量面板

点击 **Usage Panel** 或打开 http://localhost:4260/usage.html。

- 按小时、天、周、月、自定义时间段查看用量。
- 区分输入、缓存命中、输出与 reasoning token。
- 按模型、Agent 系统、Agent 名称或任务分组。
- 查看高用量任务；可在 `config/usage_pricing.yaml` 配置每百万 token 单价。

统计使用供应商返回的 usage；没有回传 usage 的调用会标记为缺失，不估算成已知 token。单价计算是本地估算，不等同于供应商账单。

详见 [用量统计](USAGE.md)。

## HTTP API

开发界面使用 `/api/v1/` 接口，完整请求形状可在页面右侧 **API Preview** 查看。

```bash
curl http://localhost:4260/health
curl http://localhost:4260/api/v1/runtime
curl http://localhost:4260/api/v1/tasks
curl 'http://localhost:4260/api/v1/usage?granularity=day&group_by=model'
```

需要远程访问时可配置 `INFIAGENT_API_TOKEN`；调用 API 时使用 `Authorization: Bearer <token>`。页面左侧也有运行时访问令牌入口，仅保存在当前标签页。Docker 示例默认只向本机开放端口。通过自定义域名访问时，在 `INFIAGENT_ALLOWED_HOSTS` 中列出域名（逗号分隔）。

## 资源与数据

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `INFIAGENT_USER_ROOT` | `/data/user_root` | 容器内持久数据目录 |
| `INFIAGENT_MAX_RUNS` | `2` | 同时运行的会话上限 |
| `MLA_PARALLEL_AGENTS_MAX` | `4` | 每个父实例的子 Agent 并发数 |
| `MLA_ACTIVE_AGENTS_MAX` | `64` | 一个 run 中的活动子实例上限 |
| `MLA_SPAWN_MAX_DEPTH` | `1` | 后台派生链的深度限制 |
| `MLA_LLM_MAX_CONCURRENCY` | `6` | 同一配置模型的并发调用上限 |
| `TZ` | `Asia/Shanghai` | 日志与用量面板的本地时区 |

保留并备份整个挂载目录，包含 `config`、`tasks`、`conversations`、`workspaces` 与 `runtime`。模型配置含密钥，备份应按同等权限保管。

## 更新与 CLI

更新镜像前停止任务并备份 `user_root`。拉取指定版本，停止旧容器，再以同一宿主目录创建新容器。3.13.0 的默认入口为开发运行界面，容器端口为 4260。

```bash
docker pull chenglinhku/mlav3:3.13.0
```

交互式命令行：

```bash
docker run -it --rm \
  -v "$PWD/user_root:/data/user_root" \
  chenglinhku/mlav3:3.13.0 cli
```
