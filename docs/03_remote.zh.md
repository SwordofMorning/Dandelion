# Dandelion Remote

## 一、模块结构与总体定位

`src/` 划分为 Core、CLI 和 Remote 三个顶层包：

1. `src/core/`：收纳与前端无关的通用执行机制与抽象基类，包括 `agent/`（`MyAgent`、`BasePromptBuilder`、`MemoryManager`、`SkillManager`、`ContextCompressor`、`ContextPruner`、`run_agent_loop`）、`tool/`（内置工具集，含 `remember`、`forget`、`prune_context`）、`subagent/` 与 `utils/`。
2. `src/cli/`：提供本地终端交互、带沙箱边界保护的 `cli_runtime()`、`CLIPromptBuilder` 以及手动会话同步客户端 `sync.py`。CLI 的 Nuitka 独立发行版仅打包 `src.cli` 与 `src.core`，不包含 `src.remote`。
3. `src/remote/`：提供部署在专用服务器上的全天候个人编码与陪伴助手，解除本地路径与命令沙箱限制（`sandbox=False`），采用单阻塞中断循环与工具化外部通讯。

Remote 的目录布局如下，入口为 `python -m src.remote`：

```text
src/remote/
├── execution/   # 主 Agent 装配、单阻塞中断循环、后台 Worker 与委派工具
├── storage/     # SQLite 生命周期存储、纯文件系统会话同步 (assets/syncs/) 与回复队列
├── channels/    # HTTP 接口与 Telegram 长轮询收发
├── tool/        # Remote 专属扩展工具（如 tool/communication/message_tool.py）
├── exports/     # 基于 assets/syncs/ 的显式 Google Drive 日报导出
├── sysprompt.py # RemotePromptBuilder 全天候助手系统提示词
├── service.py   # 命令解析、服务装配与平滑停机
└── __main__.py  # python -m src.remote 薄入口
```

---

## 二、手动同步本地会话（`sync`）

在本机 `.env/api.cfg` 中显式配置 `[Remote]`：

```ini
[Remote]
NAME=origin
URL=https://dr.example.com
TOKEN=${DANDELION_DR_TOKEN}
DEVICE_ID=work-laptop
```

启动 CLI 后可执行：

```text
remote
remote -v
sync
sync --session sess_20261028_091213_736513
sync --session project-notes
```

1. `remote` 显示远端名称，`remote -v` 显示 URL、Device ID 与 Token 配置状态（不回显明文凭据）。
2. `sync` 手动同步全部有效会话，`sync --session <ID/名称>` 仅同步指定会话。每个会话上传 `history.log`、`meta.log` 以及可选的 `task_state.json`（不上传 `api.log` 与 `artifacts/`）。
3. 服务端将同步文件按会话 ID 直接原子写入 `<workspace>/assets/syncs/<session_id>/`。当检测到会话内容更新或缺少摘要时，自动向 `RemoteLoop` 投递 `sync_received` 中断事件，唤醒远端 Dandelion 读取同步目录、生成或更新 `<workspace>/assets/syncs/<session_id>/summary.md`，并通过 `remember` / `forget` 自整理全局记忆。

---

## 三、启动远端服务与交互渠道

### 3.1 启动服务

```sh
export DANDELION_DR_TOKEN='<管理凭据>'
export DANDELION_DR_DEVICES='{"work-laptop":"<设备凭据>"}'
python -m src.remote serve \
  --host 127.0.0.1 --port 8765 \
  --data-dir .remote --workspace /srv/dandelion-workspace \
  --config .env/api.cfg --workers 4
```

### 3.2 Telegram 与主动通讯（`send_message`）

配置以下环境变量即可启用 Telegram 私聊白名单通道：

```sh
export DANDELION_TELEGRAM_TOKEN='<Bot token>'
export DANDELION_TELEGRAM_CHATS='123456789'
```

1. Dandelion Remote 运行在单阻塞中断循环（`RemoteLoop`）中，统一响应四类中断：用户消息（`message`）、CLI 会话同步（`sync_received`）、后台任务完成（`job_done`）、定时唤醒（`analysis`）。
2. 外部通讯通过 `send_message` 工具（`SendMessageTool`）由模型自主决策：处理后台 `sync_received` 或定时唤醒时，模型可静默更新摘要与记忆而不打扰用户，也可在发现重要事项时调用 `send_message` 主动推送消息。

### 3.3 记忆自整理与主动上下文剪裁

1. 双向记忆管理：除 `remember` 外，提供 `forget` 工具用于移除过时或已被合并的碎片记忆，单层索引上限为 500 行。
2. 主动上下文剪裁（`prune_context`）与换日剪裁继承：模型可主动调用 `prune_context` 启动剪裁 SubAgent 折叠历史中的大体积工具输出（自动在会话 `archive/` 下备份完整原历史并校验 `tool_use`/`tool_result` 配对）；跨日时自动创建新会话分支并继承剪裁后的昨日上下文。

### 3.4 Google Drive 日报导出

配置 Google Drive OAuth 环境变量后，可基于 `<workspace>/assets/syncs/` 中的同步会话与摘要显式导出按 UTC 日期聚合的 Google Docs 日报：

```sh
python -m src.remote drive-export --data-dir .remote --workspace /srv/dandelion-workspace
```
