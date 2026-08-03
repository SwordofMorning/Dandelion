# Regent 上下文压缩与记忆存储架构优化设计

> 用途：本文档用于提交给其他 LLM 作为优化依据，描述 Regent 当前的上下文管理架构（压缩、记忆、会话持久化）、业界参考设计（Claude Code / OpenAI Codex / 社区实践），以及提出的目标架构与优化建议。
> 关联文档：`explain.md`（缓存命中率分析，结论：请求排序策略有效、截断/冷启动/模型切换是主要可控失配因素）。
> 代码基线：2026-08-03，`src/core/agent.py`、`src/core/memory.py`、`src/core/sysprompt.py`、`src/core/skill.py`、`src/subagent/subagent.py`、`src/utils/logging/session.py`、`src/utils/safe_llm/safe_llm.py`、`main.py`。

---

## 一、当前架构

### 1.1 系统数据流总览

```
main.py
  └─ SessionManager(log_dir=".log")
       └─ 自动加载最近一个 session（switch_session）
  └─ MyAgent(config, session, workspace)
       ├─ SafeLLMClient（主模型 Provider + 子代理路由）
       ├─ MemoryManager(llm/memory)     ← 记忆存储（当前为空目录）
       ├─ SkillManager(llm/skill)       ← 技能目录（2 个技能）
       ├─ PromptBuilder(memory, skill)  ← system prompt 构建
       └─ SubAgentPool（子代理集群，深度上限 3）
```

主循环 `MyAgent.step()`（`agent.py:157`）每轮执行：

1. `memory.load_memories_string(history)` 检索记忆（当前返回空）；
2. `prompt_builder.build()` 构建 system prompt（含时间戳、环境、SubAgent 说明、技能目录、MEMORY.md 索引、安全规则）；
3. 将检索到的记忆注入**最后一条 user 消息**之前（`agent.py:165-187`）；
4. 组装 payload：`{"tools": schemas, "messages": history, "max_tokens": ..., "system": prompt}`（字段顺序：tools → messages → system）；
5. 流式调用 LLM，追加 `assistant` 消息并持久化；
6. 若返回 `tool_use`，执行工具，追加 `user(tool_result)` 消息并持久化；循环直到非工具调用。

子代理 `SubAgent.run()`（`subagent.py:85`）：独立消息列表，上限 30 轮，**无任何压缩机制**；payload 结构相同（tools → messages → system）。

### 1.2 上下文压缩现状（`agent.py:128-155`）

```python
def _compact_context(self):
    if len(self.history) > 40:                      # 触发条件：消息条数 > 40
        head = self.history[:5]                     # 保留前 5 条
        safe_idx = len(self.history) - 10           # 尾部保留 10 条（寻找安全断点）
        ...
        snip_msg = {"role": "user",
                    "content": "[snipped previous messages to save context]"}
        self.history = head + [snip_msg] + self.history[safe_idx:]
        self.session.save_history(self.history)
```

调用点：仅 `inject_user_message()`（`agent.py:241`）——即**只在用户发送新消息时检查**；工具执行循环内不压缩。

**问题清单（压缩）**：

| # | 问题 | 后果 | 严重性 |
| --- | --- | --- | --- |
| C1 | 按**消息条数**（40）触发，而非 token 数 | 一条 5 万 token 的 bash 输出即可撑爆窗口；纯聊天 40 条可能仅 8k token 就过早压缩 | 高 |
| C2 | 暴力删除中段，无 LLM 摘要 | 丢失目标、已做决策、关键文件路径、错误码；模型"失忆" | 高 |
| C3 | 无 token 记账 | 无法在接近窗口上限前预警，只能事后截断 | 高 |
| C4 | 压缩后无"恢复说明" | 模型不知道丢了什么，无从补问 | 中 |
| C5 | head 固定 5 条，无任务语义 | 用户最新指令若在早期会被剪掉 | 中 |
| C6 | 压缩立即覆盖持久化 | 无法回滚；压缩质量差时上下文不可恢复 | 中 |
| C7 | 子代理无压缩 | 30 轮上限 + 大工具输出可能超窗；子代理只做单任务，被截断后无法完成任务 | 中 |
| C8 | 工具结果原样入历史（无摘要） | bash/read_file 大输出占满上下文（社区实测单次测试输出可达 3 万 token） | 高 |

### 1.3 记忆存储现状（`memory.py` / `sysprompt.py`）

存储布局：

```
llm/memory/
  ├── MEMORY.md            # 索引文件（get_index_text 全文注入 system）
  └── <topic>.md           # 记忆文件（frontmatter: name/description + body）
```

**当前状态：`llm/memory/` 目录为空**。机制存在但：
- 没有任何代码路径写入记忆（全项目无 `save`/`write` 记忆的调用）；
- 检索逻辑 `select_relevant_memories()`（`memory.py:55`）为纯关键词匹配：取最近 3 条 user 消息中长度 >3 的词，与 `name + description` 做子串包含匹配，命中即选，最多 5 个，**无排序、无相关性分数、无语义**；
- 注入点两处：system prompt 的 "Relevant Memories"（MEMORY.md 全文）+ 最后一条 user 消息之前（`load_memories_string`）。

**问题清单（记忆）**：

| # | 问题 | 后果 | 严重性 |
| --- | --- | --- | --- |
| M1 | **无写回机制**：模型从不把经验/偏好/决策写入记忆 | 记忆系统形同虚设，跨会话无法积累 | 高 |
| M2 | 检索质量低（关键词子串匹配） | 相关记忆召回差；无预算控制（token 上限） | 中 |
| M3 | 注入位置：动态记忆注入"最后一条 user 消息" | 该消息每轮内容变化，破坏其缓存复用；若未来注入 messages 前缀则更糟 | 中 |
| M4 | 无记忆管理：无更新/删除/冲突解决/过期合并 | 记忆会陈旧、矛盾，且无人处理（Claude Code 有 Auto Dream 机制） | 中 |
| M5 | MEMORY.md 无行数/大小上限 | 索引膨胀后 system 体积失控 | 低 |
| M6 | 记忆无验证机制 | 模型可能把过期记忆当事实使用（业界原则：记忆是 hint 而非 fact） | 中 |

### 1.4 会话持久化现状（`session.py`）

- `SessionManager` 启动时自动加载**最近** session（`_ensure_default_session`），跨天/跨任务续用同一 history；
- `history.log` 为全量 JSON，**每次追加都全量重写磁盘**（`save_history`，O(n) 序列化 + 写盘，每轮 2 次）；
- 无 token 级记账（每次请求的 input/read/miss 仅记录在 api.log，无汇总）；
- 无 session 摘要机制（会话结束时不留"存档"，下一个会话从零开始）。

### 1.5 与缓存命中率的关系（引用 `explain.md` 结论）

- 消息**严格追加**是缓存命中的核心前提（当前非截断时 100% 达标）；
- **压缩是缓存的主动破坏者**：5 次截断请求的缓存命中率仅 51.5%（全会话平均 61.6%，平台口径 75.1%）；
- 记忆注入位置必须考虑缓存：注入 messages 前缀会击穿缓存；注入 system 末尾 / 最后一条 user 消息之前影响最小；
- 优化压缩与记忆 = 优化缓存保持率 + 优化模型效果，两者一致。

---

## 二、业界参考架构

### 2.1 Claude Code（Anthropic）

**记忆：双层结构**
1. `CLAUDE.md`（用户编写，入库版本控制）：构建命令、编码规范、架构笔记；按作用域分层加载：Managed Policy → User rules → User memory → Project rules → Project memory → Local Project Memory（拼接而非覆盖）；
2. Auto Memory（模型自写，`~/.claude/projects/<project>/memory/`）：会话中自动记录四类内容——用户偏好、反馈、项目上下文、参考指针；采用**两级存储**：`MEMORY.md` 索引（上限 200 行）+ 按主题拆分的细节文件；检索为**精确关键词匹配**（无向量语义检索）。

关键设计原则（来自 Claude Code 泄露系统提示与官方文档）：
- 记忆写入的隐式标准：**重复性（recurrence）、非显然性（non-obviousness）、稳定性（stability）、项目特异性（project specificity）**；
- 记忆被视为 **hint 而非 fact**：使用时先对照真实代码验证，减少幻觉；
- Auto Dream：后台定期把过时/重复记忆整合合并；
- 子代理拥有自己的 Auto Memory 空间。

**压缩**：auto-compact（会话变长时自动触发，LLM 对历史生成摘要，保留决策、目标、关键发现、文件路径，丢弃冗长工具输出与冗余对话）；提供 `/compact` 手动命令。

**缓存**：显式 `cache_control` 断点（最多 4 个），5 分钟 TTL 滚动续期；system/tools/CLAUDE.md 等稳定内容置于断点之前。

### 2.2 OpenAI Codex / Agents SDK

- `AGENTS.md`：与 CLAUDE.md 对等的静态项目指令；
- Agents SDK Sessions：两种上下文管理原语——**trimming**（丢弃早期轮次，保留最近 N 轮）与 **compression**（LLM 摘要）；
- **memory-as-a-tool**：长期记忆不注入上下文，而是作为可检索工具按需调用（避免上下文污染与噪声）；
- 状态对象（state objects）+ 结构化笔记：显式声明 agent 必须记住的状态，而非依赖隐式上下文；
- 官方建议压缩用较强模型执行，且采用结构化 schema 提取关键声明，避免摘要失真。

### 2.3 社区与研究最佳实践

来自 Agentic Context Engineering、终端编码 Agent 论文（arXiv 2603.05344）与 OpenAI 官方 cookbook 的共识：

| 实践 | 说明 |
| --- | --- |
| Per-tool summarizer | 每个工具结果落盘前做摘要（实测：单次长测试输出 3 万 token → 摘要 <100 token；会话轮数 15-20 → 30-40） |
| 大输出 offload | 超过阈值（如 8000 字符）的工具结果不入上下文，写入文件，只保留文件路径 + 摘要 |
| 防摘要漂移（summary drift） | 摘要应**周期性从完整历史重新生成**（如每 5 条新消息），而非反复压缩旧摘要（迭代压缩导致信息逐轮失真）；同时摊销摘要成本 |
| 摘要保留清单 | 结构化保留：目标、已做决策、关键发现、文件路径、函数名、变量名、错误码；丢弃：冗长工具输出、冗余交换 |
| 工具结果清理 | 把大 tool_result（如 250 token）替换为 30 token 摘要（压缩比 ~8:1） |
| 状态持久化分析 | 显式分类"必须记住 vs 可丢弃"，而非整体删减 |
| Attention budget | 上下文里每个 token 都要有理由；检索注入要有预算上限 |
| 记忆写回时机 | 会话结束 / 里程碑节点写回；避免每轮写 |
| 质量度量 IRR | Instruction Retention Ratio：预设 20 条关键事实，压缩后统计保留率（实测 LLM 摘要可达 ~90%） |

---


## 三、提出的架构方案

### 3.1 总体目标：四层上下文模型

```
┌─ L0 静态指令层 ──────────────────────────────────────────────┐
│  system: 身份 / 环境 / 工具能力说明 / 技能目录 / 安全规则       │  几乎不变，缓存友好
├─ L1 长期记忆层 ──────────────────────────────────────────────┤
│  MEMORY.md 索引（静态注入 system 末尾，低频变化）               │  跨会话
│  + 相关记忆文件（检索注入最后一条 user 消息之前，有 token 预算）  │
├─ L2 工作上下文层 ────────────────────────────────────────────┤
│  head（任务定义与当前目标，保留 K 条）                          │  会话内
│  + 结构化压缩摘要（LLM 生成，周期性从全量重生成）                │
│  + recent（最近 N 轮完整保留，含 tool_use/tool_result 配对）    │
├─ L3 实时事件层 ──────────────────────────────────────────────┤
│  当前轮次 tool_use / tool_result（大输出先行摘要化）             │  瞬时
└──────────────────────────────────────────────────────────────┘
```

设计原则：
1. **稳定内容前置、易变内容后置**（与现有排序策略一致，保持缓存命中）；
2. **能摘要的不全文、能引用文件的不内联**（token 预算意识）；
3. **记忆是 hint 不是 fact**（注入时明确要求模型验证后使用）；
4. **压缩可度量、可回滚**。

### 3.2 压缩方案（替换 `_compact_context`）

#### 3.2.1 Token 感知触发（解决 C1/C3）

- 引入 `ContextTracker`：每轮请求后从响应 usage 记录 `input_tokens / cache_read / cache_miss`，维护 `history_tokens` 的滑动估计（字符数/3.5 或按 provider 校准）；
- 双阈值：`soft_limit`（如窗口 60%，触发预压缩，提示模型收尾）与 `hard_limit`（如窗口 85%，强制压缩）；
- 触发点从 `inject_user_message` 扩展到 `step()` 每轮开头（工具循环内也会触发，解决 C7 的主代理部分）。

#### 3.2.2 结构化三层压缩（解决 C2/C5）

```
压缩后消息序列 =
  head 保留区（K=5，含用户原始任务指令，若含工具配对则整对保留）
+ [压缩摘要消息]  role=user, content=summary_block（LLM 生成）
+ recent 保留区（最近 N 轮，N≈10，按工具配对完整性对齐断点）
```

摘要生成（`summarize_history`，独立 LLM 调用，**用主模型**）要求结构化输出：

```
<conversation_summary>
  <goals>当前任务目标与验收标准</goals>
  <completed>已完成事项</completed>
  <decisions>关键决策与理由</decisions>
  <artifacts>关键文件路径 / 函数名 / 变量名 / 错误码</artifacts>
  <pending>待办与下一步</pending>
  <risks>风险与约束</risks>
</conversation_summary>
```

关键要求：
- **防漂移**：每 5 轮或每次压缩时，从**全量历史**（文件存档版）重新生成摘要，禁止"压缩摘要再压缩"；
- 摘要消息之后紧跟 `[context compacted at <time>, previous details archived to <file>]` 说明（解决 C4）；
- 摘要消息独立成一条 user 消息置于 head 与 recent 之间，**保证 recent 与后续消息仍严格追加**（缓存友好：压缩后只牺牲 head 之后、摘要之前的缓存段）。

#### 3.2.3 工具结果摘要化与 offload（解决 C8，最高性价比）

- 在 `agent.py` 执行工具后、入历史前增加 `ToolResultProcessor`：
  - 输出 ≤ 2000 字符：原样入历史；
  - 2000~8000 字符：入历史保留截断 + 末尾提示"完整输出见 <session>/artifacts/<id>.txt"（写入文件）；
  - > 8000 字符：**LLM 生成 ≤100 token 摘要**入历史（per-tool summarizer），全文写入 artifacts 文件，并提示模型可用 read_file 按需读取；
- 大文件读取类工具（read_file/read_excel/bash 输出）建议工具侧支持 `max_chars` 参数先行截断（文件系统工具层做，避免每轮重复搬运大输出）；
- 收益：单轮大输出从数万 token 降至数百 token，会话轮数容量成倍提升，缓存前缀稳定（同一文件的读取结果不再因内容差异破坏后续前缀——注意：若文件内容未变，读取结果应保持字节一致以复用缓存）。

#### 3.2.4 可回滚与持久化（解决 C6）

- 压缩前将完整历史写入 `<session>/history_archives/history_<seq>.json`（append-only）；
- 压缩后的 history 才覆盖 `history.log`；
- 提供 `reload_history()` 回滚入口（已有，`agent.py:246`）。

#### 3.2.5 子代理压缩（解决 C7）

- `SubAgent.run()` 循环内复用同一套 `ContextTracker` 与压缩逻辑：超过窗口 80% 时，将早期轮次（head 之前的工具轮次）摘要化，保留任务描述 head + 最近 5 轮 + 摘要；
- 子代理单任务语义简单，摘要格式可简化（goals/decisions/artifacts 三要素即可）。

### 3.3 记忆方案（重建 MemoryManager）

#### 3.3.1 存储格式规范（解决 M5）

```
llm/memory/
  ├── MEMORY.md                 # 索引：每行一个条目，上限 200 行
  │     - [name] description (tags: a,b) [updated: date]
  └── <topic>.md                # 主题文件，frontmatter 扩展：
      ---
      name: <唯一名>
      description: <一句话说明>
      tags: [tag1, tag2]
      created_at: <ISO>
      updated_at: <ISO>
      source_session: <session_id>
      confidence: high|medium|low
      ---
      <正文>
```

#### 3.3.2 写回机制（解决 M1，最高优先级）

- **触发器**：主循环结束（`step()` 返回 False 时，任务完成/暂停）、用户纠正行为（error_count 上升或用户否定）、里程碑（子代理结果回流后）；
- **写回流程**：以"记忆建议"形式调用 LLM（结构化输出），输入为最近 8 轮历史 + 现有 MEMORY.md 索引；输出候选记忆（满足四标准：重复性/非显然性/稳定性/项目特异性），每条标注类型（preference / project_fact / decision / pattern / lesson）；
- **执行**：新主题 → 新建 `<topic>.md`；已有主题 → 更新 `updated_at` 与正文；与旧记忆冲突 → 在正文中标记旧条目为 superseded；
- 频率控制：每会话最多写回 1 次（会话结束），避免每轮写造成噪声与成本；
- 记忆写入需要工具支持（如 `remember` 工具或复用 `write_file` 受限路径），供模型主动调用。

#### 3.3.3 检索升级（解决 M2）

- 保留关键词匹配作为 fallback，新增两级：
  1. **预算控制**：检索结果按 token 预算（如 800 token）截断，避免记忆喧宾夺主；
  2. **排序**：关键词命中数 + 标题匹配权重排序（可选：本地 embedding 模型如 BGE-small 做语义相似度，纯本地离线，无 API 成本）；
- MEMORY.md 索引全文仍注入 system（200 行内，稳定内容，缓存友好）；
- 检索出的记忆正文注入**最后一条 user 消息之前**（保持现有位置，不触碰 messages 前缀，缓存安全）。

#### 3.3.4 记忆生命周期管理（解决 M4/M6）

- 注入 system 的记忆段落固定前缀提示："以下记忆来自历史会话，属于线索（hint）而非事实，使用前请对照当前代码与文件验证"（M6）；
- 定期（每周或每 20 会话）LLM 合并任务（Auto Dream 类比）：读取全部记忆文件，合并重复、删除过时（引用已删除文件的条目）、更新冲突；
- MEMORY.md 超过 200 行时，将低频条目降级（移入 `archive/` 或合并进主题文件）。

### 3.4 会话生命周期（解决跨天冷启动，呼应 explain.md）

- **会话摘要存档**：每次会话结束时（Ctrl+C / 任务完成），生成 `session_summary.md`（目标、产出、未竟事项、关键路径），写入 `llm/memory/` 或 session 目录；
- **会话策略**：跨天自动新建 session（检测 last activity > 4h），并将上一 session 摘要注入新会话的首次请求（作为记忆）；
- `_ensure_default_session` 增加"最近活跃时间"判断，避免自动加载过期 session；
- **history 存储改为 append-only 日志 + 检查点**（可选）：`history.log` 改为行追加（每条消息一行 JSON），每 20 轮做一次全量检查点，降低写盘开销。

### 3.5 度量与观测（支撑后续优化决策）

- `ContextTracker` 输出每轮：input tokens、cache hit/miss、压缩事件、记忆写回事件；
- 会话结束输出报表：命中率、全 miss 事件数、压缩次数与触发阈值、记忆文件数、检索召回数；
- 压缩质量抽样评估：每 5 次压缩后，用 LLM 对"压缩前 20 条关键事实"与"摘要"做 IRR 对比（保留率 ≥ 85% 视为合格）。

---

## 四、其他优化建议

1. **缓存保持**：维持"tools → messages → system"字段顺序；压缩摘要消息作为一条独立 user 消息插入（不修改 head/recent 内部消息内容）；记忆注入不触碰 messages 前缀；工具结果字节稳定（同一文件未变则读取结果不变）。
2. **子代理结果回流控制**：`spawn_subagent` 将 SubAgentResult.summary 写入主历史时，限制长度（如 ≤1500 字符），超出部分由子代理先行压缩。
3. **思考块处理**：若启用 extended thinking，思考块（thinking）不持久化入 history.log 主消息（或单独存储），避免压缩时把思考历史误当对话内容。
4. **错误重试与记忆**：`error_count` 达到阈值时，将失败模式（工具、命令、参数）写入记忆的 lesson 类型，防止同类错误反复发生。
5. **多语言与 token 估算**：中文场景 token 估算按字符/0.6 校准（中文 token 密度高），建议用实际 usage 反馈校准估算系数，而非固定除法。
6. **并发安全**：`save_history` 全量写盘增加文件锁或原子写（tmp + rename），防止中断损坏 history。
7. **技能与记忆联动**：技能加载（load_skill）成功后，将技能目录清单的命中情况计入缓存稳定性设计（技能目录已注入 system，保持字节稳定即可）。
8. **配置化**：所有阈值（窗口比例、压缩保留 K/N、摘要 token 预算、记忆检索预算、写回频率）进入 `api.cfg`，支持按任务类型调整（长任务 vs 短对话）。

---

## 五、实施优先级建议

| 优先级 | 事项 | 解决 | 预期收益 |
| --- | --- | --- | --- |
| P0 | Token 感知触发 + 结构化三层压缩（LLM 摘要） | C1-C5 | 会话容量 2-3 倍；跨截断记忆保留 |
| P0 | 工具结果摘要化 + 大输出 offload | C8 | 单轮 token 消耗下降 70-90%；轮数容量倍增 |
| P1 | 记忆写回（会话结束触发）+ 记忆格式规范 | M1/M5 | 跨会话能力积累从 0 到 1 |
| P1 | 检索预算 + 排序（含可选本地 embedding） | M2 | 召回质量与注入噪声控制 |
| P1 | 会话摘要存档 + 跨天新会话策略 | 冷启动 | 缓存命中率提升（消除跨天续用） |
| P2 | 记忆生命周期管理（冲突/过期合并） | M4/M6 | 记忆质量长期稳定 |
| P2 | 度量报表 + IRR 质量评估 | 观测 | 后续优化有据可依 |
| P3 | 子代理压缩、append-only 存储、向量检索 | C7/其余 | 增量提升 |

---

## 附录 A：关键代码位置索引

| 模块 | 位置 | 说明 |
| --- | --- | --- |
| 主循环 | `src/core/agent.py:157` (`step`) | 记忆注入、payload 组装、工具执行 |
| 压缩 | `src/core/agent.py:128` (`_compact_context`) | 现状：40 条阈值暴力截断 |
| 压缩触发 | `src/core/agent.py:241` (`inject_user_message`) | 仅在用户消息时触发 |
| 记忆检索 | `src/core/memory.py:55` (`select_relevant_memories`) | 关键词子串匹配 |
| 记忆注入 | `src/core/memory.py:83` (`load_memories_string`) + `agent.py:165-187` | 注入最后一条 user 消息之前 |
| 索引注入 | `src/core/sysprompt.py:67` | MEMORY.md 全文入 system |
| 子代理循环 | `src/subagent/subagent.py:85` (`run`) | 30 轮上限，无压缩 |
| 会话持久化 | `src/utils/logging/session.py:83-110` | 全量 JSON 每轮重写 |
| 会话选择 | `src/utils/logging/session.py:19` (`_ensure_default_session`) | 自动加载最近 session |
| LLM 调用 | `src/utils/safe_llm/safe_llm.py` | 主模型/路由/重试 |
| 入口 | `main.py` | 组装 session 与 agent |
