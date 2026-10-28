# Dandelion Remote

`src/` 仅包含 Core、CLI 和 Remote 三个主包：`src/core/agent/` 收纳 Main Agent、执行循环和上下文模块，`src/core/runtime.py` 保留通用宿主回调；工具、SubAgent 与通用接口分别归入 Core 的 `tool/`、`subagent/`、`utils/` 子包。`src/cli/` 提供终端交互和手动备份客户端 `sync.py`。CLI 保留同步 `spawn_subagent`、Ctrl+C 回滚和现有依赖规模；Remote 不进入 CLI 的 Nuitka 发行版。

Remote 按功能收纳，入口仍是 `python -m src.remote`：

```text
src/remote/
├── execution/   # 主 Agent 绑定、事件循环、后台任务与委派工具
├── storage/     # SQLite 生命周期、会话快照与待发送回复
├── channels/    # HTTP 接口与 Telegram 传输
├── exports/     # 显式 Google Drive 日报导出
├── service.py   # 命令解析、服务装配与关闭
└── __main__.py  # python -m src.remote 的薄入口
```

## 手动备份会话

在本机 `.env/api.cfg` 中显式配置一个 Remote：

```ini
[Remote]
NAME=origin
URL=https://dr.example.com
TOKEN=${DANDELION_DR_TOKEN}
DEVICE_ID=work-laptop
```

`TOKEN` 可填写凭据，也可使用完整的 `$VAR` 或 `${VAR}` 环境变量占位符。URL 与设备 ID 来自该配置段，不隐式读取环境变量作为目的地。使用独立设备凭据时，`DEVICE_ID` 必须与服务器的设备映射一致。

启动 CLI 后使用：

```text
remote
remote -v
sync
sync --session sess_20261027_123456_123456
sync --session project-notes
```

`remote` 显示配置的远端名称，`remote -v` 额外显示 URL，两者都不展示 TOKEN。`sync` 手动备份全部有效会话，`sync --session <ID/名称>` 仅备份指定会话，不切换当前会话，也不修改本地文件。每个会话原样传输 `history.log` 和 `meta.log`，不生成摘要，不调用模型；`api.log`、产物、记忆与草稿不在本次备份范围。

备份以固定快照分块上传，校验文件大小与 SHA-256，两个文件齐全后才原子发布。失败时本地文件保持原样，可再次手动运行 `sync`。启动 CLI 不会自动联网同步；没有自动重试队列。

服务端 `GET /sessions` 可按 `device`、`project` 筛选已发布备份，返回最新快照元数据。使用管理凭据查看：

```sh
curl 'https://dr.example.com/sessions' \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
```

使用列表中返回的备份 ID 查询分页消息，或下载原始文件：

```text
GET /sessions/<id>?offset=0&limit=100
GET /sessions/<id>/history.log
GET /sessions/<id>/meta.log
```

这些读取接口仅允许管理凭据访问。备份历史包含原始对话，请按本地会话日志的敏感程度保护服务器数据。

## 启动服务器

使用 Python 3.11 及以上，沿用 [CLI 的安装方式](../README.md#22-安装依赖)，并准备独立的模型配置 `api.cfg`。从仓库目录启动：

```sh
export DANDELION_DR_TOKEN='<管理凭据>'
export DANDELION_DR_DEVICES='{"work-laptop":"<设备凭据>"}'
python -m src.remote serve \
  --host 127.0.0.1 --port 8765 \
  --data-dir .remote --workspace /srv/dandelion-workspace \
  --config .env/api.cfg --workers 4
```

管理凭据用于消息、任务、会话备份查询等接口；`DANDELION_DR_DEVICES` 是可选的设备 ID 到凭据的 JSON 映射，设备凭据仅允许上传本设备的会话备份。不要把管理凭据分发给每台电脑。`/health` 用于存活检查；其余 HTTP 入口使用 `Authorization: Bearer <token>`。`POST /sync` 接收文件分块和最终提交请求，提交确认后快照才对查询接口可见。

HTTP 还提供 `/activities`、`/messages`、`/replies`、`/jobs`、`/schedules`。网络线程只提交事件；Agent 线程按顺序处理会话，不从 HTTP 或 Telegram 回调直接修改主 Agent 的 history。默认仅绑定回环地址；跨电脑访问时在前面配置 HTTPS 反向代理，并保留鉴权。

### HTTP 示例

使用管理凭据提交消息，再按回复 ID 游标读取结果：

```sh
curl "https://dr.example.com/messages" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","text":"Summarize my project progress."}'
curl "https://dr.example.com/replies?session_id=web%3Ame&after=0" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
```

将下一次查询的 `after` 设为已收到的最大回复 `id`。下面创建从指定日期开始、每 86400 秒重复的普通提醒；请按需要修改日期。`due_at` 必须带时区，省略 `interval` 表示单次任务，`kind` 也可用 `analysis` 让主 Agent 分析 `text`：

```sh
curl "https://dr.example.com/schedules" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","due_at":"2030-01-01T20:00:00+08:00","text":"Review project notes.","kind":"reminder","interval":86400}'
```

取消后台任务使用其实际 `job_id`；取消当前主 Agent 轮次使用 `/cancel`：

```sh
curl -X DELETE "https://dr.example.com/jobs/<job_id>?session_id=web%3Ame" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
curl "https://dr.example.com/cancel" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me"}'
```

### Telegram

可选配置以下环境变量后再启动服务：

```sh
export DANDELION_TELEGRAM_TOKEN='<Bot token>'
export DANDELION_TELEGRAM_CHATS='123456789'
```

多个 chat ID 用逗号分隔。只接收允许列表中的私聊；不要将 Bot token 写进仓库。`/cancel` 请求在下一个安全边界取消本轮。服务使用长轮询，不需要公开 Telegram webhook。各电脑只连接 DR，由服务器访问 Telegram 和模型服务。服务器可以先使用现有美国 VPS，再根据实际直连情况迁至香港。

### 独立活动数据与可选 Google Drive 日报

既有活动 JSONL 的 `import` / `export` 和 Drive 日报仍可用于独立活动数据；CLI 的 `sync` 只备份会话，不写入活动记录，也不自动生成日报：

```sh
python -m src.remote import activity.jsonl --data-dir .remote
python -m src.remote export activities.jsonl --data-dir .remote
```

先在自己的 Google Cloud 项目启用 Drive API，完成 OAuth 授权，建议使用 `drive.file` 范围管理本应用创建的文档。现有 Gmail 授权不能直接替代 Drive 授权；程序不会启动交互式登录。提供有效的访问令牌后，显式运行：

```sh
export DANDELION_DRIVE_ACCESS_TOKEN='<OAuth access token>'
# 可选：将新建文档放入已经授权可访问的文件夹
export DANDELION_DRIVE_FOLDER_ID='<folder ID>'
python -m src.remote drive-export --data-dir .remote
```

也可改用 `DANDELION_DRIVE_CLIENT_ID`、`DANDELION_DRIVE_CLIENT_SECRET`、`DANDELION_DRIVE_REFRESH_TOKEN` 三个变量，由命令刷新访问令牌。凭据仅保存在服务端环境中。日报按 UTC 日期汇总已导入的独立活动；SQLite 保存日期到文档 ID 的映射，内容变化时更新同一份 Google Docs，内容未变则跳过。更新会完整替换该文档正文，因此请在 DR 原始活动中维护信息，不要依赖对导出文档的手工修改。

导出是显式操作，与会话备份无自动关联。Gemini 可通过其已配置的 Google Drive 连接按需读取文档；上传成功不代表 Gemini 会在每次对话中自动记住全部内容。实现采用官方 [Drive 上传与转换接口](https://developers.google.com/workspace/drive/api/guides/manage-uploads#import_to_google_docs_types) 和 [OAuth 刷新令牌接口](https://developers.google.com/identity/protocols/oauth2/web-server#offline)。

## 执行与恢复

Core 中的 `run_agent_loop()`、工具工厂和 `Runtime` 回调供 CLI 与 Remote 共用。工具工厂分别选择 Main 与 Worker 工具，提示词跟随实际注册的工具。Remote 默认不读取终端审批，越界访问被拒绝。

Remote 主 Agent 通过 `submit_task` 立即取得任务 ID，随后可继续对话；后台 Worker 在独立日志、会话和取消标志下调用同一个 `SubAgentPool.create_and_run()`。同一工作区的后台写任务串行执行。Worker 内部仍使用原来的同步递归委派。完成结果保存后，作为后续事件返回所属会话，不会对同一个工具调用追加第二份 `tool_result`。

SQLite 保存任务、活动、定时任务及待投递事件。空闲时阻塞等待事件，不持续请求模型。普通提醒由调度线程直接通知；需要分析的定时任务才唤醒 Agent，因此提醒不必等待主 Agent 的长任务。

- 取消在完整步骤之间协作生效，不强杀正在运行的网络请求或工具，不回滚工作区文件；已取消任务的迟到结果不会作为正常完成继续唤醒会话。
- 重启后恢复排队任务，未处理事件继续保留。崩溃时正在处理的主 Agent 轮次和后台任务标记为 `interrupted` 并发出通知，不自动重跑；检查任务状态后再明确提交，避免重复执行有副作用的工具。不会恢复 SubAgent 的执行现场。
- 通知及 Telegram 出站消息采用至少一次投递；在发送成功但尚未记录确认的崩溃窗口中，可能收到重复通知。
- 停机后备份整个 `--data-dir`，包括 SQLite 文件、`backups/` 下的会话快照、运行会话日志和任务产物；工作区文件另行备份。不要只复制一个正在写入的 SQLite 文件。

可用以下命令导出服务端活动，便于人工查看和迁移：

```sh
python -m src.remote export activities.jsonl --data-dir .remote
```

[部署目录](../deploy/) 提供容器示例。目录迁移不改变 `--data-dir` 及原有 SQLite 任务表；沿用之前的数据目录即可。网页入口可在后续扩展；独立活动的 Google Docs 日报是可重建副本。
