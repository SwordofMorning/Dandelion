# Dandelion Remote

DR 在服务器上常驻，复用 Dandelion 的同步 Core。CLI 仍然使用原来的终端交互、同步 `spawn_subagent` 和 Ctrl+C 回滚；Remote 的数据库、网络入口和后台调度位于独立的 `dandelion_remote/` 包，不进入 CLI 的 Nuitka 发行版，也没有增加第三方依赖。

## 本地摘要同步

在运行 CLI 的环境中配置：

```sh
export DANDELION_DR_URL=https://dr.example.com
export DANDELION_DR_TOKEN='<该设备的同步凭据>'
export DANDELION_DEVICE_ID=work-laptop
python main.py
```

CLI 中可使用：

```text
sync
sync 今天完成登录接口，待办是补充部署说明。
sync --retry
sync --export activity.jsonl
```

`sync` 从最近的最终答复和当前任务状态生成有长度限制的活动摘要，也可直接提供摘要文字；不会上传原始 API 日志、图片和工具大输出。活动记录包含稳定的 `event_id`、设备、项目、会话、时间、摘要和来源引用。它与历史消息下标无关，因此上下文压缩不会使同步游标失效。

记录先写入 `.log/sync/`，仅在服务器确认对应 ID 后移出待发送队列。失败时保留，`sync --retry` 和下次启动会尝试补传。服务端按 ID 去重。`sync --export` 导出待发送 JSONL，并保留本地队列；将文件复制到服务器后导入即可：

```sh
python -m dandelion_remote import activity.jsonl --data-dir .remote
```

旧环境变量 `DR_URL`、`DR_TOKEN` 也可使用。设备 ID 应在同一电脑上保持稳定；服务端为设备配置独立凭据时，ID 必须与配置中的键相同。

## 启动服务器

使用 Python 3.11 及以上，沿用 [CLI 的安装方式](../README.md#22-安装依赖)，并准备独立的模型配置 `api.cfg`。从仓库目录启动：

```sh
export DANDELION_DR_TOKEN='<管理凭据>'
export DANDELION_DR_DEVICES='{"work-laptop":"<设备凭据>"}'
python -m dandelion_remote serve \
  --host 127.0.0.1 --port 8765 \
  --data-dir .remote --workspace /srv/dandelion-workspace \
  --config .env/api.cfg --workers 4
```

管理凭据用于消息、任务、摘要查询等接口；`DANDELION_DR_DEVICES` 是可选的设备 ID 到凭据的 JSON 映射，设备凭据仅允许上传本设备的摘要。不要把管理凭据分发给每台电脑。`/health` 用于存活检查；其余 HTTP 入口使用 `Authorization: Bearer <token>`。摘要上传为 `POST /sync`，请求形如 `{"records":[活动记录...]}`，应答的 `accepted` 列出已持久化的活动 ID。

HTTP 还提供 `/activities`、`/messages`、`/replies`、`/jobs`、`/schedules`。网络线程只提交事件；Agent 线程按顺序处理会话，不从 HTTP 或 Telegram 回调直接修改主 Agent 的 history。默认仅绑定回环地址；跨电脑访问时在前面配置 HTTPS 反向代理，并保留鉴权。

### HTTP 示例

使用管理凭据提交消息，再按回复 ID 游标读取结果：

```sh
curl "$DANDELION_DR_URL/messages" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","text":"Summarize my project progress."}'
curl "$DANDELION_DR_URL/replies?session_id=web%3Ame&after=0" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
```

将下一次查询的 `after` 设为已收到的最大回复 `id`。下面创建从指定日期开始、每 86400 秒重复的普通提醒；请按需要修改日期。`due_at` 必须带时区，省略 `interval` 表示单次任务，`kind` 也可用 `analysis` 让主 Agent 分析 `text`：

```sh
curl "$DANDELION_DR_URL/schedules" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN" \
  --json '{"session_id":"web:me","due_at":"2030-01-01T20:00:00+08:00","text":"Review project notes.","kind":"reminder","interval":86400}'
```

取消后台任务使用其实际 `job_id`；取消当前主 Agent 轮次使用 `/cancel`：

```sh
curl -X DELETE "$DANDELION_DR_URL/jobs/<job_id>?session_id=web%3Ame" \
  -H "Authorization: Bearer $DANDELION_DR_TOKEN"
curl "$DANDELION_DR_URL/cancel" \
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

### 可选 Google Drive 日报

先在自己的 Google Cloud 项目启用 Drive API，完成 OAuth 授权，建议使用 `drive.file` 范围管理本应用创建的文档。现有 Gmail 授权不能直接替代 Drive 授权；程序不会启动交互式登录。提供有效的访问令牌后，显式运行：

```sh
export DANDELION_DRIVE_ACCESS_TOKEN='<OAuth access token>'
# 可选：将新建文档放入已经授权可访问的文件夹
export DANDELION_DRIVE_FOLDER_ID='<folder ID>'
python -m dandelion_remote drive-export --data-dir .remote
```

也可改用 `DANDELION_DRIVE_CLIENT_ID`、`DANDELION_DRIVE_CLIENT_SECRET`、`DANDELION_DRIVE_REFRESH_TOKEN` 三个变量，由命令刷新访问令牌。凭据仅保存在服务端环境中。日报按 UTC 日期汇总已同步活动；SQLite 保存日期到文档 ID 的映射，内容变化时更新同一份 Google Docs，内容未变则跳过。更新会完整替换该文档正文，因此请在 DR 原始活动中维护信息，不要依赖对导出文档的手工修改。

导出是显式操作，不随每次同步自动上传。Gemini 可通过其已配置的 Google Drive 连接按需读取文档；上传成功不代表 Gemini 会在每次对话中自动记住全部内容。实现采用官方 [Drive 上传与转换接口](https://developers.google.com/workspace/drive/api/guides/manage-uploads#import_to_google_docs_types) 和 [OAuth 刷新令牌接口](https://developers.google.com/identity/protocols/oauth2/web-server#offline)。

## 执行与恢复

Core 中的 `run_agent_loop()`、工具工厂和 `Runtime` 回调供 CLI 与 Remote 共用。工具工厂分别选择 Main 与 Worker 工具，提示词跟随实际注册的工具。Remote 默认不读取终端审批，越界访问被拒绝。

Remote 主 Agent 通过 `submit_task` 立即取得任务 ID，随后可继续对话；后台 Worker 在独立日志、会话和取消标志下调用同一个 `SubAgentPool.create_and_run()`。同一工作区的后台写任务串行执行。Worker 内部仍使用原来的同步递归委派。完成结果保存后，作为后续事件返回所属会话，不会对同一个工具调用追加第二份 `tool_result`。

SQLite 保存任务、活动、定时任务及待投递事件。空闲时阻塞等待事件，不持续请求模型。普通提醒由调度线程直接通知；需要分析的定时任务才唤醒 Agent，因此提醒不必等待主 Agent 的长任务。

- 取消在完整步骤之间协作生效，不强杀正在运行的网络请求或工具，不回滚工作区文件；已取消任务的迟到结果不会作为正常完成继续唤醒会话。
- 重启后恢复排队任务，未处理事件继续保留。崩溃时正在处理的主 Agent 轮次和后台任务标记为 `interrupted` 并发出通知，不自动重跑；检查任务状态后再明确提交，避免重复执行有副作用的工具。不会恢复 SubAgent 的执行现场。
- 通知及 Telegram 出站消息采用至少一次投递；在发送成功但尚未记录确认的崩溃窗口中，可能收到重复通知。
- 停机后备份整个 `--data-dir`，包括 SQLite 文件、会话日志和任务产物；工作区文件另行备份。不要只复制一个正在写入的 SQLite 文件。

可用以下命令导出服务端活动，便于人工查看和迁移：

```sh
python -m dandelion_remote export activities.jsonl --data-dir .remote
```

[部署目录](../deploy/) 提供容器示例。网页入口可在后续扩展；权威记录保存在 DR 本地，Google Docs 是可重建的日报副本。
