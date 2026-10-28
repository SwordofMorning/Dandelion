# Dandelion Remote

## 一、模块结构与总体定位

`src/` 划分为 Core、CLI 和 Remote 三个顶层包：

1. `src/core/`：收纳与前端无关的通用执行机制与抽象基类，包括 `agent/`（`MyAgent`、`BasePromptBuilder`、`MemoryManager`、`SkillManager`、`ContextCompressor`、`ContextPruner`、`run_agent_loop`）、`tool/`（内置工具集，含 `remember`、`forget`、`prune_context`）、`subagent/` 与 `utils/`。
2. `src/cli/`：提供本地终端交互、带沙箱边界保护的 `cli_runtime()`、`CLIPromptBuilder` 以及手动会话同步客户端 `sync.py`。本地客户端读取 `cli.cfg`（兼容回退 `api.cfg`）。
3. `src/remote/`：提供部署在专用服务器上的全天候个人编码与陪伴助手，解除本地路径与命令沙箱限制（`sandbox=False`），采用单阻塞中断循环与工具化外部通讯。远端服务器统一读取 `remote.cfg`，无需配置繁琐的环境变量，并通过根目录的 `./remote.sh` 脚本一键管理。

Remote 的目录布局如下：

```text
src/remote/
├── execution/   # 主 Agent 装配、单阻塞中断循环与后台 Worker
├── storage/     # SQLite 生命周期存储、纯文件系统会话同步 (assets/syncs/) 与回复队列
├── channels/    # 基于 MD5 ACCESS_KEY 校验的 HTTP 接口与 Telegram 长轮询收发
├── tool/        # Remote 专属工具（agent/task_tool.py 与 communication/message_tool.py）
├── exports/     # 基于 assets/syncs/ 的显式 Google Drive 日报导出
├── sysprompt.py # RemotePromptBuilder 全天候助手系统提示词
├── service.py   # remote.cfg 加载、服务装配与平滑停机
└── __main__.py  # python -m src.remote 薄入口
```

---

## 二、配置文件说明（`cli.cfg` 与 `remote.cfg`）

### 2.1 本地客户端配置（`cli.cfg`）

在本机配置目录下的 `cli.cfg` 中显式配置 `[Remote]` 段：

```ini
[Remote]
NAME = origin
URL = https://sub2api.swordofmorning.com/dandelion-sync
ACCESS_KEY = dr-your-shared-secret-key
DEVICE_ID = work-pc
```

1. `ACCESS_KEY` 为本地 CLI 与远端 DR 服务器共用的密钥。客户端在发起 HTTP 请求前先对 `ACCESS_KEY` 计算 MD5 摘要，服务端通过恒定时间比对 MD5 摘要完成鉴权，避免明文比对。
2. 启动 CLI 后可使用：
   - `remote` / `remote -v`：查看远端名称、URL、Device ID 与 Access Key 配置状态。
   - `sync` / `sync --session <ID/名称>`：手动将本地会话的 `history.log`、`meta.log` 及可选的 `task_state.json` 上传至远端 `<workspace>/assets/syncs/<session_id>/`，并自动触发 `sync_received` 中断唤醒远端 Dandelion 生成 `summary.md` 与更新全局记忆。

### 2.2 远端服务器配置（`remote.cfg`）

远端服务器使用独立的 `remote.cfg`，除包含与 `cli.cfg` 相同的 `[Main]` 和模型 Provider 段外，直接通过 `[Server]`、`[Telegram]`、`[Drive]` 段管理全部服务参数：

```ini
[Server]
HOST = 0.0.0.0
PORT = 8765
WORKSPACE = ./workspace
DATA_DIR = ./.remote
WORKERS = 4
ACCESS_KEY = dr-your-shared-secret-key

[Telegram]
BOT_TOKEN = 123456789:AAHxxxxxxxxxxxxxxxx
ALLOWED_CHATS = 987654321

[Drive]
ACCESS_TOKEN =
CLIENT_ID =
CLIENT_SECRET =
REFRESH_TOKEN =
FOLDER_ID =
```

---

## 三、服务端一键部署与管理（`remote.sh`）

在远端服务器上使用仓库根目录的 `remote.sh` 脚本管理服务生命周期：

```sh
./remote.sh init      # 初始化目录、内置技能与 remote.cfg 模板（自动生成随机 ACCESS_KEY）
./remote.sh start     # 后台守护模式启动 Dandelion Remote
./remote.sh status    # 查看运行状态与 PID
./remote.sh logs      # 实时查看运行日志
./remote.sh restart   # 平滑重启服务
./remote.sh stop      # 发送 SIGTERM 平滑停止服务
./remote.sh fg        # 前台直接运行（用于调试）
```
