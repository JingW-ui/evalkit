# Codemaker 会话日志兼容 evalkit —— 调研与接入报告

> 调研日期：2026-08-15（会话内调研，本机 `~/.codemaker` 与 `~/.local/share/codemaker` 实测）
> 调研对象：
> - **Codemaker 桌面客户端**（网易内部 AI 编程 agent，OpenCode 系）：`~/.codemaker/` 安装目录、
>   `~/.local/share/codemaker/opencode.db` 会话库（SQLite，38MB，20 个会话）
> - Codemaker 网关源码：`~/.codemaker/agent-client/gateway_pkg/{history_readers,sessions,paths}.py`
> - pi-agent 会话格式文档：`~/.codemaker/doctor-agent/pi/runtime/docs/session-format.md`
> - 对照：DSH 事件流（`docs/dsh-integration-research.md`）、Claude Code（`docs/claude-backend-research.md`）
> 结论性质：只读调研 + 兼容方案。本报告是 `codemaker_backend.py` 开发的设计依据。

---

## 一、结论摘要（TL;DR）

1. **Codemaker 不产 DSH/Claude 式 JSONL 会话日志，而是把会话持久化在一个 OpenCode 系的
   SQLite 库 `~/.local/share/codemaker/opencode.db`**（另有 `~/.codemaker/` 安装目录只有
   运行时日志/配置，无会话历史）。要分析 Codemaker 会话，唯一可靠入口就是这个 SQLite 库。
2. **库内 event 表是追加式事件日志**（`aggregate_id=session_id`、`seq` 连续、`type` 为
   `session.created.1 / session.updated.1 / message.updated.1 / message.part.updated.1`）——
   **与 DSH 的 append-only SessionEvent 日志同构**，是实时挂接的关键：轮询 `seq` 增量即可
   得到实时事件流，无需文件尾随。
3. **message / part 表是最终快照**：message（role/time/finish/tokens/cost/modelID/error）
   + part（text / reasoning / tool{callID,state} / step-start / step-finish{reason,tokens,cost}
   / compaction）。离线重放读这两张表即可还原完整会话。
4. **todo 表直接给出子任务**（content/status/priority/position），映射到评测面板的
   tasks[].subitems，无需从工具调用推断。
5. **成本/耗时官方直给**：session.cost（USD）、tokens_*（含 cache_read/cache_write）、
   time_created/time_updated，优于 DSH/Claude 的自算。
6. **接入方案**：`codemaker_backend.py` 提供
   `CodemakerDB`（只读访问）+ `CodemakerEventAdapter`（DB/event 行 → 统一事件，喂
   `EventMetrics`）+ `CodemakerTails`（event 表实时尾随）+ `scan_codemaker_log`（离线解析）；
   `session_discovery.py` 识别 `.db` → agent=`codemaker`；`eval_server.py` 支持列表/挂接
   （live=先重放再尾随）/重放/raw 文本转储。前端会话列表自动出现 codemaker 分组。

---

## 二、存储结构调研

### 2.1 `~/.codemaker/`（安装目录，无会话历史）

| 子目录 | 内容 |
|---|---|
| `agent-client/` | 网关（Python）：`gateway_pkg/history_readers.py`、`sessions.py`、`paths.py` |
| `clihub/`、`pi-agent/`、`codemaker-hub/` | CLI / pi 内核 / Hub GUI（node_modules 为主） |
| `log/codemaker_extension.log` | 扩展运行时日志（仅日志，无会话） |
| `codemaker.json` / `mcps.json` | MCP 配置（POPO MCP、airgattai stdio） |

`agent-client/data/cache/session_map.json` 是**元数据缓存**（`cli_session_id ↔ agent_id/title/
updated_at`），不含消息内容。`agent-client/conf/sessions/` 为空。

关键源码线索（`gateway_pkg/history_readers.py`）：
- `CHAT_JSONL_DIR = agent-client/data/cache/chat-jsonl`（本机未生成）
- 本机会话实际落在 OpenCode 系 SQLite：`~/.local/share/codemaker/opencode.db`（源码
  `opencode_db_paths()` 列出的路径之一）。
- 网关还读 `~/.claude/projects`（Claude 历史）、`~/.codex/sessions`（Codex 历史）作为旁路。

### 2.2 `~/.local/share/codemaker/opencode.db`（会话主库，SQLite）

表结构与行量（本机实测，20 会话）：

| 表 | 行数 | 关键列 |
|---|---|---|
| `session` | 20 | id(`ses_xxx`)/title/directory/path/model(JSON串)/agent/cost/tokens_*/time_created/time_updated/time_archived |
| `message` | 746 | id(`msg_xxx`)/session_id/time_created/data(JSON) |
| `part` | 2854 | id(`prt_xxx`)/message_id/session_id/time_created/data(JSON) |
| `todo` | 21 | session_id/content/status/priority/position/time_* |
| `event` | 11226 | id/aggregate_id(=session)/seq/type/data(JSON) —— **追加式事件日志** |
| `project` | 1 | id=`global`（单项目模式） |
| `report_event` | 21 | 上报记录（与本地分析无关） |

### 2.3 message.data 结构（最终快照）

```jsonc
// user 消息
{"role": "user", "time": {"created": 1784778434578}, "model": {"providerID": "netease-codemaker", "modelID": "deepseek-v4-pro"}}
// assistant 消息
{"role": "assistant", "time": {"created": ..., "completed": ...},
 "modelID": "deepseek-v4-pro", "providerID": "netease-codemaker",
 "finish": "tool-calls" | "stop",          // 无 = 中断
 "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},  // 中断时
 "tokens": {"total": 107860, "input": 1088, "output": 107, "reasoning": 937,
            "cache": {"write": 0, "read": 105728}},
 "cost": 0.0121712,                         // USD
 "parentID": "msg_..."}
```

`finish` 分布实测：`tool-calls` 574 / `stop` 81 / 无 91（中断）。`finish=stop` → 回合正常结束；
`tool-calls` → 中途（下一步继续）；无 + `error` → 中断（aborted）。

### 2.4 part.data 结构（内容块）

| type | 字段 | 说明 |
|---|---|---|
| `text` | text | 用户输入 / assistant 正文 |
| `reasoning` | text | 推理内容 |
| `tool` | tool/callID/state | 工具调用 + 结果（state: status=completed/error, input, output, metadata{exit}） |
| `step-start` | — | 一步开始（time_created 即开始时间） |
| `step-finish` | reason/tokens/cost | 一步结束（reason: stop/tool-calls） |
| `compaction` | auto/tail_start_id | 上下文压缩标记 |

工具名分布实测（751 次）：bash 310、read 174、write 58、mcphub 57、edit 24、skillhub 12、
skill 13、glob 50、grep 12、todowrite 16、question 7、list_mcp_resources 8、
list_mcp_resource_templates 6、webfetch 2、task 2。`skill` 归一为 `Skill`（与 DSH/Claude 口径
一致）；`question` = 人工介入（映射到 human_interventions）。

### 2.5 event.data 结构（实时增量）

```jsonc
// message.updated.1：消息快照（含最终 usage/cost）
{"sessionID": "...", "info": {"id": "msg_...", "role": "assistant", "modelID": "...",
 "time": {"created": ..., "completed": ...}, "finish": "tool-calls",
 "tokens": {...}, "cost": 0.01}}
// message.part.updated.1：part 快照（含工具完成态）
{"sessionID": "...", "part": {"id": "prt_...", "messageID": "msg_...", "type": "tool",
 "tool": "bash", "callID": "call_...", "state": {"status": "completed", "input": {...}, "output": "..."}},
 "time": 1785328680536}
```

event 表 `seq` 每会话连续（`event_sequence` 表登记 aggregate 最新 seq）——**等价 DSH 的
SessionEvent.seq 连续性**，轮询 `WHERE aggregate_id=? AND seq>? ORDER BY seq` 即可增量拉取。

---

## 三、与 DSH / Claude 的事件同构映射

| DSH 统一事件 | Codemaker 来源 | 备注 |
|---|---|---|
| `user/message` | user 消息的 text part | 真实用户指令计数 / 任务切分 |
| `assistant/message` | assistant 消息的 text+reasoning part + message.tokens/modelID | usage 驼峰化：tokens→inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens |
| `tool/call` | tool part（callID/tool/state.input） | arguments=json(input) |
| `tool/result` | 同 part（state.status/state.output） | isError=status=="error" |
| `step/start` `step/end` | step-start / step-finish part | llm_ms 累计 |
| `turn/end` | message.finish==stop → completed；error → aborted/error | tool-calls 不结束回合 |
| `request/header` | 可省略（skill_available 由工具名推断） | — |

`EventMetrics`（`dsh_backend.py`）直接消费以上统一事件，得到与 claude/dsh 通道一致的
指标：tool 成功率、turn 结束原因、token 用量、llm_ms、耗时、模型轮次、任务工具链。

---

## 四、兼容实现（本仓库落地）

### 4.1 `codemaker_backend.py`（新增）

- `is_codemaker_db(path)`：SQLite 存在 session/message/part 三表即判 codemaker。
- `CodemakerDB`：只读访问（`file:...?mode=ro` 短连接），提供
  `list_sessions / get_session / messages / parts / todos / max_seq / events_after`。
- `CodemakerEventAdapter`：
  - `replay(db, session_id)`：message/part 表最终快照 → 统一事件列表（离线重放）；
  - `adapt_event(event_row, state)`：event 表增量行 → 统一事件（实时，state 去重：
    user_done/assistant_done/tool_done/step_done，文本 part 晚到也能补发）。
- `CodemakerTails`：`start(session_id, db_path)` → 首轮重放现有快照 + 轮询 event 表增量，
  与 `JsonlTails` 同接口（on_events 回调），供 `eval_server` 实时挂接。
- `scan_codemaker_log(db_path=None, session_id=None)`：离线解析 → EventMetrics 快照
  （含官方 cost_usd / tokens / 子任务注入 / turn_end_reason）。
- CLI：`python codemaker_backend.py [--db path] [--session id] [--json]`。

### 4.2 `session_discovery.py`（扩展）

- `_detect_agent`：`.db` → `is_codemaker_db` → `codemaker`。
- `discover_codemaker_db(db_path)`：一个库展开为多个 `SessionInfo`（每会话一条，
  session_id=`ses_xxx`，path=库路径，query=title，model 解析 JSON 串）。
- `discover_samples_dir / discover_single_path / discover_all`：支持 `.db`（samples 目录下
  `.db` 展开为多会话；`discover_all` 默认自动并入本机 Codemaker 库，`codemaker_db=False`
  可关闭）。

### 4.3 `eval_server.py`（集成）

- `--codemaker <db_path>`：指定会话库（缺省自动探测 `~/.local/share/codemaker/opencode.db`）。
- 列表：`list_sessions` 并入 codemaker 会话（去重 + 别名/隐藏/挂接状态一致）。
- 挂接：agent=codemaker → live 走 `CodemakerTails`（先重放再尾随）；replay 走
  `_replay_codemaker`（DB 快照 → 统一事件批量广播 → run/end + 评测判级）。
- raw：`.db` 会话 → 可读文本转储（`_codemaker_raw_text`，消息/part 逐行格式化），
  RawLog 标签页可浏览。
- 文件选择器 `list_dir`：识别 `.db` → kind=`codemaker`。

### 4.4 前端

- `SessionList.jsx`：`AGENT_LABEL.codemaker` + 根路径「Codemaker 会话库」快捷入口。
- 其余（Report/Trajectory/Inspector/TasksPanel）由统一事件驱动，无需改动。

---

## 五、已知限制

1. **无 TTFT**：part 是最终快照，无 token 级流式块，`ttft_ms` 保持 None（与 Claude 无
   `--include-partial-messages` 时一致）。
2. **工具耗时 = 0**：tool part 无开始/结束时间戳（`dur_ms` 为 0 或 None），工具链可视化
   显示名称/参数/成败/结果摘要，不显示耗时。
3. **live 模式的中途文本**：assistant 文本在回合结束（finish=stop/error）时才发
   `assistant/message`（文本 part 晚到会补发），中途只发 tool/step 事件。
4. **`archived_at` 恒空**：本机库从未归档，无法据此区分"已完成/进行中"，故默认按历史
   （replay）展示；需要实时看板时显式走 live 挂接。
5. **只读不写**：CodemakerDB 全部 `mode=ro`，不改动 Codemaker 数据。

---

## 六、验证

- `tests/test_codemaker_backend.py`：合成 opencode.db（session/message/part/todo/event 全表）
  验证 is_codemaker_db / 发现 / 重放事件 / EventMetrics 折叠 / live 增量去重 /
  CodemakerTails 首轮重放+增量 / scan_codemaker_log / 发现集成 —— 全部通过。
- 全量回归：`tests/` 7 个套件全部通过。
- 真库端到端：`eval_server.py --port 8090` 列表 20 个 codemaker 会话，重放 380 事件 +
  run/end（cost_usd 0.550882 → cost_cny ¥3.97，end_reason=aborted，7 条子任务），
  `/api/raw` 返回可读文本转储。
