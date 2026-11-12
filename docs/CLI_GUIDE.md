# CLI

安装并配置模型后运行：

```bash
pip install infiagent==3.13.0
infiagent
```

CLI 使用本地 `user_root` 中的模型与 Agent System 配置。可先启动 `infiagent-runtime --user-root ./user_root`，在开发界面完成配置；启动 CLI 时设置相同的根目录：

```bash
MLA_USER_DATA_ROOT="$PWD/user_root" infiagent
```

根据启动菜单选择智能体系统与入口智能体，输入任务目录和目标。会话记录会持久保存，可按 CLI 提示继续或恢复已有任务。需要观察完整过程、管理多个会话或编辑模型连接时，可使用黄色开发运行界面。

Docker CLI：

```bash
docker run -it --rm -v "$PWD/user_root:/data/user_root" chenglinhku/mlav3:3.13.0 cli
```

SDK 的程序化调用见 [SDK 指南](SDK_GUIDE.md)。
