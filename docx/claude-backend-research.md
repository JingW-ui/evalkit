# Claude Code 接入 evalkit 实时评测后端 —— 调研报告

> 调研日期：2026-02（会话内调研）
> 调研对象：
> - Claude Code CLI **2.1.232**（本机实测 `--output-format stream-json` 真实输出）
> - Claude Agent SDK for Python（`claude-agent-sdk`，PyPI 0.2.139，源码/文档调研）
> - Claude Code Hooks 体系（本机 `settings.local.json` 已配置活跃 hook 通道）
> - 对照：DSH 事件流（`docs/dsh-integration-research.md`）
> 结论性质：只读调研 + 接入方案。本报告是 `claude_backend.py` 开发的设计依据。

---

## 一、结论摘要（TL;DR）

1. **Claude Code 具备与 DSH 同等量级的实时事件流能力**，且有三条可选的采集通道：
   - **通道 A：CLI `--output-format stream-json`**（已实测）——零新依赖、evalkit `runner.py` 已在用该模式收集行，只差解析。事件行完整覆盖：tool_use / tool_result（含 stdout/stderr/is_error）/ usage / **ttft_ms** / **total_cost_usd** / **terminal_reason**；加 `--include-partial-messages` 可获得 token 级流式块（`stream_event` 行，`message_start` 自带 ttft_ms）。
   - **通道 B：Claude Agent SDK（Python）**——更结构化的事件 API、官方支持**子代理事件**（CLI/hooks 都拿不到子代理内部事件），支持 interrupt 与交互。详见第五节（子代理调研结果）。
   - **通道 C：Hooks**（PreToolUse/PostToolUse → 本地 HTTP）——本机**已配置活跃**（`127.0.0.1:15721`），可作旁路信号源；但子代理内部工具调用不触发 hook。
2. **stream-json 事件与 DSH SessionEvent 高度同构**：tool/call ↔ `assistant(tool_use)`、tool/result ↔ `user(tool_result+tool_use_result)`、assistant/message ↔ `assistant(text)`、usage ↔ `assistant.usage`/`result.usage`、turn/end reason ↔ `result.terminal_reason`、request/header ↔ `system(init)`。
3. **Claude 通道独有的优势**：`total_cost_usd`、`ttft_ms`、`tool_use_result.stdout/stderr` 直接给出（DSH 需自算）。
4. **Claude 通道相对 DSH 的缺口**：无结构化 turn/step 边界事件、无独立持久化事件库/SQLite 搜索、无血缘追踪服务（子代理内部事件缺失）、无 token 级 `assistant/chunk`（除非开 `--include-partial-messages`）。
5. **接入建议**：优先**通道 A（stream-json 解析）**——改动最小、立即可用；**通道 B（SDK）**作为需要子代理事件/交互控制时的升级路径。二者都通过一个 `ClaudeEventAdapter` 把事件转成与 `EventMetrics`（`dsh_backend.py`）同构的输入。

---

## 二、通道 A：CLI stream-json（实测）

### 2.1 触发方式

```bash
claude --print --verbose --output-format stream-json \
       [--include-partial-messages] [--include-hook-events] "<prompt>"
```

- `--verbose` 必须（stream-json 依赖）。
- `--include-partial-messages`：输出 `stream_event` 行（Messages API 流式事件，token 级）。
- `--include-hook-events`：输出 hook 生命周期行（`system/hook_started`、`system/hook_response`）。
- stdout 逐行实时输出 JSON；`--print` 模式 headless 非交互。
- evalkit `runner.py::run_task` 已用 `subprocess.Popen` + 双线程读 stdout/stderr 收集 `stream_lines`——**实时采集骨架已存在**。

### 2.2 事件行类型（本机实测 2.1.232，逐字段）

| 行 type | 关键字段 | 对应 DSH 事件 |
|---|---|---|
| `system` subtype=`init` | cwd、session_id、**tools**（完整工具列表，含 Skill/Bash/mcp__*）、**model**、permissionMode、skills、agents、capabilities、memory_paths | `request/header`（环境装配） |
| `assistant` | `message.content[]`（text / **tool_use**{id,name,input}）、`message.usage`{input_tokens,cache_read_input_tokens,cache_creation_input_tokens,output_tokens}、parent_tool_use_id、session_id、uuid、timestamp | `assistant/message` + `tool/call` |
| `user` | `message.content[]`（**tool_result**{tool_use_id,type,content,is_error}）、**tool_use_result**{stdout,stderr,interrupted,isImage,noOutputExpected}、timestamp | `tool/result`（含失败标志与输出） |
| `stream_event`（需 flag） | `event.type`∈message_start/content_block_start/content_block_delta/content_block_stop/message_delta/message_stop；message_start 带 **ttft_ms**；content_block_delta.delta.text_delta 为 token 级；message_delta.stop_reason | `assistant/chunk`（token 级流式） |
| `system` subtype=`hook_started`/`hook_response`（需 flag） | hook_name（UserPromptSubmit/Stop/PreToolUse/PostToolUse…）、hook_response.output/stdout/stderr/exit_code/outcome | （旁路信号） |
| `result`（最终一行） | **is_error**、stop_reason（end_turn…）、**terminal_reason**（completed…）、**total_cost_usd**、usage 全量（含 output_tokens_details.thinking_tokens、server_tool_use）、modelUsage（按模型聚合）、permission_denials、**result**（最终文本）、**ttft_ms**、**ttft_stream_ms**、**duration_ms**、duration_api_ms、num_turns | `turn/end reason` + 全量 usage + 成本 |

### 2.3 实测样例（节选，2026-08-15 本机）

```json
{"type":"assistant","message":{"content":[{"type":"tool_use","id":"call_00_...","name":"Bash",
  "input":{"command":"ls -1 \"D:\\wy_projects\\work_4_log\" | head -n 5"}}],
  "usage":{"input_tokens":44919,"output_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0}},
 "parent_tool_use_id":null,"session_id":"812257ba-...","timestamp":"2026-08-15T14:11:44.604Z"}
```
```json
{"type":"user","message":{"role":"user","content":[{"tool_use_id":"call_00_...","type":"tool_result",
  "content":"change_resolution.ps1\ndevice_id\neval\nimg_logs\nlist_resolutions.ps1","is_error":false}]},
 "tool_use_result":{"stdout":"...","stderr":"","interrupted":false,"isImage":false,"noOutputExpected":false},
 "session_id":"812257ba-...","timestamp":"2026-08-15T14:12:03.154Z"}
```
```json
{"type":"result","is_error":false,"num_turns":2,"stop_reason":"end_turn",
 "total_cost_usd":0.503106,
 "usage":{"input_tokens":99469,"cache_read_input_tokens":1152,"output_tokens":124,"output_tokens_details":{"thinking_tokens":0}},
 "permission_denials":[],"terminal_reason":"completed","result":"前 5 个文件名如下：...",
 "ttft_ms":5616,"ttft_stream_ms":2772,"duration_ms":28614,"session_id":"812257ba-..."}
```

---

## 三、通道 C：Hooks（本机已配置）

- 本机 `~/.claude` / 项目 `settings.local.json` 已配置 **PreToolUse / PostToolUse / PostToolUseFailure / UserPromptSubmit / Stop** 等多类 hook，`type: http` 发往 `http://127.0.0.1:15721/hook/claude/{pre-tool,post-tool,...}`。
- 这正是 evalkit ROADMAP 第 10 节预留的"hook 采集"通道（15721 端口）——**通道已就绪，只差一个本地 HTTP 接收端**。
- Hook payload（PreToolUse/PostToolUse）含 session_id、transcript_path、cwd、hook_event_name、tool_name、tool_input / tool_response（含 is_error）等（见 [Claude Code Hooks 文档](https://code.claude.com/docs/en/hooks)）。
- 局限：**子代理内部的工具调用不触发 hook**（只存在于各子代理自己的 JSONL）；hook 是旁路信号，事件顺序需自行对时。
- 价值：可在**不改动被测 agent 命令**的情况下旁路采集（适合 Claude Code 已跑起来的场景）；与通道 A 双写互证。

---

## 四、与 DSH SessionEvent 映射表

| DSH 事件 | Claude stream-json 行 | 覆盖度 |
|---|---|---|
| `turn/start` / `turn/end` | 无显式事件；`result.num_turns`、`result.terminal_reason`、`stream_event message_delta.stop_reason` | 部分（结束原因有，边界无） |
| `step/start` / `step/end` | 无（每轮 assistant+user 可近似一个 step） | 缺 |
| `user/message` | `result.result` / `assistant` 之间的 user 提示（headless 下为注入 prompt） | 部分 |
| `assistant/message` | `assistant`（content text） | 完全 |
| `assistant/chunk` | `stream_event`（content_block_delta / message_start.ttft_ms） | 完全（需 flag） |
| `tool/call` | `assistant`（content tool_use） | 完全 |
| `tool/result` | `user`（tool_result + tool_use_result） | 完全（且多 stdout/stderr） |
| `request/header` | `system(init)`（tools/model/skills） | 完全 |
| usage | `assistant.usage` + `result.usage`（含 cache_read/cache_creation/thinking） | 完全 |
| turn/end reason | `result.terminal_reason`（completed/…） | 完全（枚举值待确认全集） |
| 成本 | `result.total_cost_usd` | **DSH 无（需自算）** |
| TTFT / 耗时 | `result.ttft_ms` / `duration_ms` / `message_start.ttft_ms` | **DSH 需自算** |
| 工具结果原文 | `tool_use_result.stdout/stderr` | **DSH 需自拼** |

---

## 五、通道 B：Claude Agent SDK（Python）

### 5.1 基本信息（权威来源：`claude-agent-sdk` 0.2.139 源码 `src/claude_agent_sdk/types.py` + 官方示例 `examples/streaming_mode.py`）

- PyPI 包名 `claude-agent-sdk`，本机 pip 可装（0.2.139）。
- 与 CLI stream-json **同源**：SDK 也是驱动 Claude Code CLI 子进程，消息模型与实测的 stream-json 行一一对应，但提供结构化 dataclass + 额外控制面。
- 新版 API：`ClaudeSDKClient`（`client.query(prompt)` + `async for msg in client.receive_messages()`）；老版 API：`connect()`/`query()` 异步迭代器。

### 5.2 消息类型（dataclass，字段级）

| 类型 | 关键字段 | 评测价值 |
|---|---|---|
| `UserMessage` | content、**tool_use_result**{stdout,stderr,interrupted,isImage}、parent_tool_use_id、**origin**{kind: human/peer/task-notification/observer/auto-continuation…} | origin 区分**真实用户消息 vs 注入消息**（噪音过滤利器，对应 DSH user/message） |
| `AssistantMessage` | content（Text/Thinking/**ToolUseBlock**{id,name,input}/ServerToolUseBlock）、model、**usage**、stop_reason、error | 对应 assistant/message + tool/call |
| `SystemMessage` | subtype（init/status/hook_started/hook_response…）+ data | 环境装配（对应 request/header） |
| `ResultMessage` | **subtype、duration_ms、duration_api_ms、is_error、num_turns、total_cost_usd、usage、result、model_usage{ModelUsage: inputTokens/outputTokens/cacheReadInputTokens/cacheCreationInputTokens/**costUSD**/provider}、permission_denials、terminal_reason**（completed/max_turns/**aborted_streaming**/**aborted_tools**）、errors、api_error_status | 对应 turn/end reason + 成本 + 耗时 + 全量 usage |
| `StreamEvent` | 原始 Anthropic API 流式事件（message_start 含 ttft_ms、content_block_delta…） | 对应 assistant/chunk（需 `include_partial_messages=True`） |
| `TaskStartedMessage` / `TaskProgressMessage` / `TaskNotificationMessage` / `TaskUpdatedMessage` | task_id、status（completed/failed/stopped/killed）、**usage: TaskUsage{total_tokens, tool_uses, duration_ms}**、summary、output_file | **后台任务（Task 工具）实时状态 + token/耗时**——DSH 无对应 |
| `RateLimitEvent` | 限流状态（allowed_warning/rejected）、reset 时间 | 评测稳定性告警 |

### 5.3 控制面（CLI 通道没有的能力）

| 能力 | SDK 入口 | 评测价值 |
|---|---|---|
| **中途打断** | `client.interrupt()`（terminal_reason 变 aborted_streaming/aborted_tools） | 死循环打断、超时中断（评测必需） |
| **工具权限回调** | `can_use_tool`（SDKControlPermissionRequest） | 评测系统可实时决策是否放行工具调用（干预） |
| **预算上限** | `ClaudeAgentOptions.max_budget_usd` / `max_turns` | 防失控（L4 诚实度测试、超时保护） |
| **子代理定义** | `ClaudeAgentOptions.agents: dict[str, AgentDefinition]` | 程序化注入被测子代理 |
| **skills 装配** | `skills="all"` / 名单（自动配 Skill 工具） | 评测 skill 触发（对齐 evalkit 的 skill_expected） |
| **hooks 回调** | `ClaudeAgentOptions.hooks`（内置 hook 回调） | 旁路采集（与通道 C 同语义，SDK 内建） |
| **外部存储镜像** | `session_store`（每次落盘行镜像到外部） | 评测日志实时双写（类似 DSH 持久化） |
| **会话控制** | resume / fork_session / resume_session_at / session_id | 断点续测、分支评测 |

### 5.4 与通道 A（CLI stream-json）的取舍

| 维度 | 通道 A：CLI stream-json | 通道 B：SDK |
|---|---|---|
| 依赖 | 无（已有 claude CLI） | `pip install claude-agent-sdk` |
| 事件模型 | 裸 JSON 行（无官方文档，实测为准） | 结构化 dataclass（类型即文档） |
| 实时交互 | ❌（headless 不可干预） | ✅ interrupt / can_use_tool / 权限决策 |
| 超时/预算 | 只能 kill 进程 | `max_turns` / `max_budget_usd` 原生支持 |
| 子代理内部事件 | ❌ | ❌ 主流不含（Agent 工具子代理内部事件不进主流）；但 **Task 后台任务有 Task*Message 流出** |
| 实现成本 | 低（解析行） | 中（异步 API） |
| 适用 | 快速接入、零依赖 | 需要交互/预算/任务的正式评测 |

> 注：CLI/hooks/SDK 主流都拿不到 **Agent 工具子代理的内部工具调用**（只存在于各子代理自己的 JSONL，见 [feature request #43553](https://github.com/anthropics/claude-code/issues/43553)）；需要子代理级评测时需事后读子代理 JSONL 或走 SDK 的 Task 事件。

### 5.5 事件转换建议（SDK → EventMetrics 统一事件）

```
ClaudeSDKClient.receive_messages()
   │  AssistantMessage(ToolUseBlock) ──► {type:"tool/call", callId:block.id, name, arguments:json(block.input)}
   │  UserMessage(tool_use_result)   ──► {type:"tool/result", callId:content.tool_use_id,
   │                                       message:{content:[{isError} ]}, stdout/stderr 另存}
   │  AssistantMessage(text+usage)   ──► {type:"assistant/message", message:{content,usage}}
   │  StreamEvent                     ──► {type:"assistant/chunk", chunk:event}
   │  ResultMessage                   ──► {type:"turn/end", reason:{kind:terminal_reason}}
   │                                        + 指标直接注入（total_cost_usd/ttft_ms/duration_ms）
   ▼
EventMetrics.on_event(...)（复用 dsh_backend.py，零改动）
```

---

## 六、接入方案（`claude_backend.py`）

复用 `dsh_backend.py` 已建成的**统一事件折叠器 `EventMetrics`**（消费 SessionEvent dict），新增一个 **`ClaudeEventAdapter`** 把 stream-json 行转成同构事件：

```
claude CLI (--print --verbose --output-format stream-json --include-partial-messages)
   │  stdout 逐行 JSON（实时）
   ▼
ClaudeEventAdapter.stream_events()  ──►  统一事件 dict（喂给 EventMetrics）
   │                                        ├─ tool/call  ← assistant(tool_use)
   │                                        ├─ tool/result ← user(tool_result)（含 is_error/stdout）
   │                                        ├─ assistant/message ← assistant(text)+usage
   │                                        ├─ assistant/chunk ← stream_event delta
   │                                        └─ turn/end reason ← result.terminal_reason
   ▼
EventMetrics.on_event() ──► 实时指标 + 告警（工具成功率/结束原因/token/TTFT）
   ▼
落盘：raw stream-json 行 → 结果 JSON（scan_dsh_log 的 jsonl 格式可另存，供离线复用）
```

- `ClaudeEvalBackend.run_task(task, timeout_s, on_event, on_warning)` 与 `DshEvalBackend` 同接口，上层可无缝切换被测对象（Claude Code vs DSH agent）。
- 超时：subprocess 侧 `proc.kill()`（runner.py 已有 timeout 逻辑）。
- 成本：直接取 `result.total_cost_usd`（免去 cost.py 反推，但口径是 Claude 官方价，注意与 airlab 加价区分）。

---

## 七、风险与建议

1. **stream-json 行格式无官方完整文档**（[anthropics/claude-code#24596](https://github.com/anthropics/claude-code/issues/24596)）——以实测行为准，升级 CLI 时需回归。
2. **子代理内部事件**：CLI/hooks 均缺失（[feature request #43553](https://github.com/anthropics/claude-code/issues/43553)）；需要子代理级评测时应走 SDK 或事后读子代理 JSONL。
3. **本机 Claude 走本地代理**（`ANTHROPIC_BASE_URL=http://127.0.0.1:15721`、`auto_deepseek_plan[1m]`）——成本口径是代理结算价，`total_cost_usd` 与 airlab 计价可能不一致，需按 cost.py 口径复核。
4. **建议路线**：先实现通道 A（stream-json 解析，改动最小）；需要子代理事件/交互控制时升级通道 B（SDK）；hook 通道（C）作为旁路补充，不承担主采集。
