# evalkit × DeepSeek Harness 集成调研报告

> 调研日期：2026-02（会话内调研）
> 调研对象：
> - `D:\wy_projects\evalkit` —— Python 评测工具箱（全部源码精读）
> - `C:\Users\wangjing71\PycharmProjects\deepseek-harness` —— DeepSeek Harness（DSH）源码（轨迹/会话追踪全链路精读，子代理交叉验证）
> 结论性质：只读调研 + 方案建议。本报告是阶段 0/1 开发（`scan_dsh_log` 兼容补丁、`dsh_backend.py` 实时评测后端）的设计依据。

---

## 一、结论摘要（TL;DR）

1. **evalkit 与 DSH 轨迹功能不是替代关系，而是"评测语义层"与"执行 + 观测基础设施层"的互补关系。**
2. **evalkit 已半只脚踏进 DSH**：`session_report.py::scan_dsh_log` 解析的正是 DSH 的 JSONL 持久化格式（首行 `type:'session'` 头）。
3. **DSH 轨迹模块能实现实时评测**：事件内存即时 emit + SSE 实时推送 + Python SDK 通知订阅，具备 evalkit ROADMAP 第 10 节想要的全部能力（实时 `tool_result`、实时指标、运行中干预）。
4. **不建议完全抛弃 evalkit**：其评测语义资产（L1-L4 分级、校验器注册表、诚实度判据、成本反推、业务 skill 绑定）是 DSH 没有的独有价值。
5. **推荐方案**：分阶段混合演进——阶段 0 顺手修正、阶段 1 桥接实时评测、阶段 2 指标引擎移植进 DSH `session-projection` 框架。

---

## 二、evalkit 架构剖析

单目录 Python 脚本集，围绕"Agent 会话日志 → 宏观指标"的离线分析管线：

```
tasks/*.json（L1-L4 task 声明：level / skill_expected / query / success_condition）
   │
   ├─ run_eval.py ──runner.py(subprocess 驱动 claude)──► 事后找 ~/.claude/projects/*.jsonl
   ├─ replay.py / run_replay_batch.py ──► 离线批量分析历史会话
   │
   ▼
parser.py：parse_session_jsonl(3 种日志格式) + compute_metrics + 校验器注册表(VALIDATORS)
session_report.py：scan_single_session / scan_dsh_log / scan_airlab → 同构 dict
   │
   ▼
分析层：adjudicator.py(rules.yaml 完成态/异常) · scanner.py(跨会话+雪球归因) ·
        classify_level.py(L1-L4 推断) · cost.py(成本 + 平台加价反推) ·
        orchestrator.py(脚本确定性解析 + LLM 首尾判定 + rules.yaml 自增殖)
   │
   ▼
报表层：report.py(MD) · report_html.py(深色 HTML) · report_interactive.py(SPA 四级下钻) ·
        session_report.py(单会话深度 HTML，含 dsh/airlab/jsonl 三种格式检测)
```

**关键局限**（ROADMAP 第 10 节自述）：
- 纯离线、事后读文件；非 DSH 格式拿不到 `tool_result` → 算不了工具成功率/错误恢复率；
- 无实时指标、无运行中告警、无主动多轮干预；
- 数据源固定为 Claude Code 日志 + DSH/airlab 落盘日志。

**对 DSH 已存在的耦合**：`detect_log_kind` 识别 `type:'session'` 头与 `assistant/chunk`/`tool/call`/`tool/result`/`user/message`/`assistant/message`/`step/start` 等流式事件；`scan_dsh_log` 已能解析 DSH JSONL（但存在两处缺口，见阶段 0）。

---

## 三、DSH 轨迹功能架构（分层）

轨迹功能是一条**事件溯源（event-sourced）全链路**，不是单一模块：

```
【事件产生层】agent-loop / 工具执行器 / 用户消息等生产者
      │  Session.append(type, data, {surfaceOp, sourceEventSeqs})
      ▼
【事件模型层】packages/core/session
      Session 类（内存追加式日志，seq 连续编号，事件 deepFreeze 不可变）
      ├─ surfaceManager.validateNext()      ← surface 校验
      ├─ dsh-invariants 的 session-invariant ← 关系不变量（seq/turn-step 嵌套/pendingCalls）
      │
      │  同步广播 cordis 事件：session/event（fire-and-forget）、session/flush（持久化屏障）
      ▼
【持久化层】packages/session/session-persistence(+jsonl/sqlite)
      PersistenceCoordinator + SessionWriteBehind（200ms 批量 + flush 屏障）
      JSONL 文件（.jsonl / .jsonl.zstd，chunk-runs 压缩）或 SQLite；崩溃修复（torn tail）
      ▼
【查询层】packages/session-query/session-query
      SessionQueryEngine（live-preferred corpus：优先读内存 live 会话）
      ├─ tracing.ts：traceSession(血缘) / traceEvent(事件关系)
      ├─ corpus.ts：live 快照 / persistence.inspect 兜底
      ├─ 搜索后端：session-query-sqlite（SQLite FTS5 全文索引）
      ▼
【工具层】packages/session-query/tool-session-query —— 5 个模型工具（workspace 授权、纯文本输出）
      session_search / session_event_search / session_trace /
      session_event_trace / session_event_read
      ▼
【实时层】packages/host/apiproxy —— SSE GET /api/events.mux（session/event 实时帧）
      + /api/events.host（生命周期帧）+ WebSocket downlink（mux/host 双流）
      ▼
【UI 层】packages/client/runtime（conversation snapshot）+ packages/client/ui-trajectory
      → TrajectoryView：TrajectoryTable 台账 + TrajectoryTimeline 瀑布图(TTFT/解码耗时)
      ▼
【导出层】packages/host/apiproxy/src/session-export.ts —— GET /api/session.export → ZIP
      （session.jsonl 原始日志 + subagents/* 血缘后代 + media/* 媒体）
```

---

## 四、事件模型与落盘格式（阶段 0 依据）

### 4.1 SessionEventMap（`packages/core/session/src/types.ts` L236-333）

| 事件类型 | data | 说明 |
|---|---|---|
| `turn/start` / `turn/end` | `{turn}` / `{turn, reason}` | reason 为 `TurnEndReason`：completed / aborted / blocked / error / max-tokens / interrupted |
| `step/start` / `step/end` | `{turn, step}` | 回合内步骤（一次模型调用 + 工具执行） |
| `user/message` | `UserMessage` | 用户消息/注入上下文，**surface 事件** |
| `assistant/chunk` | `{turn, step, chunk: StreamChunk}` | 原始流式 chunk（token 级回放保真） |
| `assistant/message` | `{turn, step, message, usage?}` | 组装助手消息，**surface 事件**，携带 `usage`（`TokenUsage`：inputTokens / outputTokens / cacheReadTokens / **cacheWriteTokens?** / reasoningTokens?） |
| `tool/call` | `{turn, step, callId, name, arguments}` | 工具调用（arguments 为原始 JSON 字符串） |
| `tool/result` | `{turn, step, message, error?, meta?}` | 工具结果，**surface 事件**；`message.content[].isError` 标识失败 |
| `todo/write` | `{todos}` | todo 列表快照（log-only UI 状态） |
| `request/header` | `{header: EpochHeader, reason}` | 完整请求头：config / system prompt / tools schema |
| `request/context` | `RequestContext` | 路由元数据 |
| `session/end-seed` | `{}` | 构造 seed 结束标记（resume/fork/replay 边界） |

事件信封：`{type, seq, time, data, ignorable?, sourceEventSeqs?, surfaceOp?}`，seq 从 0 连续。

### 4.2 落盘压缩格式（chunk-runs，阶段 0 必须解包）

`packages/core/session/src/chunk-rows.ts`：连续同块 delta chunk 事件合并为一条存储行（`MIN_RUN = 3`），**存储行不是会话事件**，用裸 type 标签区分：

```
text-chunks:        {type, seq0, time0, data:{turn, step, index, dt:number[], texts:string[]}}
reasoning-chunks:   {type, seq0, time0, data:{turn, step, index, dt, texts}}
tool-call-chunks:   {type, seq0, time0, data:{turn, step, index, dt, id, name?, args:string[]}}
```

**解包规则**（`expandRow`，chunk-rows.ts L293-328）：成员 k 重建为
- `seq = seq0 + k`
- `time = time0 + sum(dt[0..k-1])`（dt 长度 = 成员数 - 1，可为负）
- `text-chunks` → `assistant/chunk` data.chunk = `{type:'text-delta', index, text: texts[k]}`
- `reasoning-chunks` → `{type:'reasoning-delta', index, text: texts[k]}`
- `tool-call-chunks` → `{type:'tool-call-delta', index, id, name?, argumentsDelta: args[k]}`

**不打包**（始终一行一事件）：块边界、usage、finish chunk 及任何未来变体。所以 **usage 统计不受压缩影响**，但 `scan_dsh_log` 若要"token 级回放保真 / 完整事件序列"（如逐轮 token 增量、snowball 定位），必须解包这三类行。

### 4.3 usage 的两种携带方式（阶段 0 缺口）

- 流式过程：`assistant/chunk` 中 `chunk.type == 'usage'` 的块（scan_dsh_log 已处理）；
- 组装消息：`assistant/message.usage`（`TokenUsage`，驼峰字段，含 `cacheWriteTokens?`）。
- **scan_dsh_log 目前只处理前者，且注释误写"dsh 无 cache_write 字段"**（实际有 `cacheWriteTokens`，可选）——阶段 0 修复。

---

## 五、实时性机制

| 层 | 机制 | 证据 |
|---|---|---|
| 内存追加 | `Session.append` 同步热路径，入 log 后立即 emit `session/event`，不阻塞 I/O | `core/session/src/index.ts` L604-655 |
| 持久化 | write-behind 200ms 批量落盘（`DEFAULT_WRITE_BATCH_MAX_DELAY_MS = 200`）+ `flush()` quiescence 屏障 | `session-persistence/src/{coordinator,write-behind}.ts` |
| Web 推送 | SSE `/api/events.mux`：打开即发基线帧（`session/subscribed {sessionId, lastSeq}`），此后每个新事件立即推 `session/event` 帧；`/api/events.host` 推生命周期；另有 WebSocket downlink | `apiproxy/src/api-proxy.ts` L3429-3532、`api/events.ts`、`client/connection/websocket-downlink.ts` |
| 客户端缝合 | liveBuffer + seq 空洞检测 → `repairGap` 重拉尾页；历史回填走分页 `history()` RPC；断线 `resync()` | `packages/client/runtime/src/client/sessions/session.ts` L467-697 |
| SDK 通知 | Python SDK `subscribe_session_notifications(session_id)` 订阅会话树，`session.event` 通知携带原始 SessionEvent；`session.status==idle` 表示回合结束 | `python/sdk/src/deepseek_harness/*` |

---

## 六、Python SDK 评测通道（阶段 1 依据）

`python/sdk/src/deepseek_harness/`（依赖 pydantic，`pip install deepseek-harness` 或源码直用）：

```python
from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

with DeepSeekHarness(DeepSeekHarnessConfig(
        provider="deepseek-official", model="deepseek-v4-flash",
        cwd="D:/wy_projects/work_4_log", session_root="D:/eval-sessions")) as h:
    result = h.run("帮我部署一下组内g66资源", session_id="eval-g66-L3-001",
                   on_notification=my_callback)   # 每个通知实时回调
    # result: RunResult(session_id, final_response, finish_reason, events, notifications)
```

- `run()` 内部：`session_prompt` 发任务 → 循环 `subscription.next()` 直到 `session.status==idle`；
- `on_notification` 回调：每个 `Notification(method, payload)` 实时到达，其中 `method=='session.event'`、`payload.sessionId` + `payload.event`（原始 SessionEvent dict）；
- `finish_reason`：最后一个 `turn/end` 的 `data.reason.kind`（completed/error/...）；
- `respond`/`next_request`：支持 agent 反向交互（评测系统可回应 agent 的提问）——实现"主动多轮干预"。

---

## 七、共性映射表

| 维度 | evalkit | DSH 轨迹功能 | 关系 |
|---|---|---|---|
| 数据源 | Claude Code JSONL / **DSH JSONL** / airlab 文本 | append-only 事件日志（JSONL/SQLite） | **格式已对齐**（首行 `type:'session'`） |
| 指标 | token / 工具 / 轮次 / 成本 / 触发 | session-stats + usage + turn reason | 部分重叠，DSH 更细（时序/失败原因） |
| 分析 | 校验器 / 诚实度 / 雪球 / L1-L4 分级 | 血缘 / 事件关系 / 全文搜索 | **互补**（DSH 无评测语义） |
| 实时 | ❌ 离线事后 | ✅ 内存 emit + SSE + SDK 通知 | **DSH 补齐 evalkit 最大短板** |
| 报表 | 自研 HTML/MD | TrajectoryView（台账 + 瀑布图） | 可互相借鉴 |
| 运行 | subprocess 驱动 Claude Code | SDK / 内置会话（多会话并发） | 互补 |
| 工具结果 | 非 DSH 格式缺失 | `tool/result` 含 `error`/`isError` | DSH 补缺 |
| 结束判定 | 采信 agent 自证（锚点匹配） | `turn/end reason` 结构化结束原因 | DSH 提供客观信号 |
| 性能 | 无 | session-stats（llmMs/toolMs/ttftMs/decodeMs） | DSH 新增维度 |
| 指标引擎 | 无（事后重算） | session-projection（init/apply/view 增量折叠） | DSH 现成架构 |

---

## 八、复用可行性评估

| DSH 能力 | 对 evalkit 的价值 | 复用方式 |
|---|---|---|
| 事件流实时推送 / SDK 通知 | 实时评测（ROADMAP 第 10 节） | 阶段 1 `dsh_backend.py` |
| `tool/result` + `error` | 工具成功率、错误恢复率 | 增量折叠器 |
| `turn/end reason` | 客观结束判定 | 增量折叠器 |
| `request/header` | skill 加载 / 工具选择精确检测 | 增量折叠器（tools 里有 `name=='skill'`） |
| `session-stats` | TTFT / 解码吞吐等性能指标 | 阶段 2 直接引用或移植 |
| `session-projection` | 指标引擎架构（每个指标一个投影单元） | 阶段 2 移植 |
| SQLite FTS5 搜索 | 跨会话检索 / 评测语料管理 | 阶段 2 复用 |
| `ui-trajectory` | 评测轨迹视图（台账 + 瀑布图） | 阶段 3 借鉴/扩展 |
| `session-export` ZIP | 评测日志导出归档 | 阶段 2 复用 |
| 血缘追踪 traceSession | 子代理/多会话评测分析 | 阶段 2 复用 |

**保留（evalkit 独有资产）**：L1-L4 task schema、校验器注册表、诚实度判据、成本反推（cost.py）、rules.yaml 驱动的 adjudicator、业务 skill（G66/uu-remote）绑定、报告 HTML 风格。

---

## 九、方案对比与推荐路线

| 方案 | 描述 | 优点 | 缺点 |
|---|---|---|---|
| A. 混合桥接 | evalkit 保留，新增 DSH 实时采集后端（阶段 1） | 最快见效（1-2 周实时评测）；评测资产零损失 | 需写事件流→指标增量折叠器 |
| B. 渐进移植 | DSH monorepo 内建 eval 插件，指标做成 projection unit | 原生实时、性能最好、GUI 内嵌轨迹 | 工作量大；DSH 未发布（`SESSION_FORMAT_VERSION=0`） |
| C. 完全抛弃重写 | 丢弃 evalkit 从零基于 DSH 开发 | 无历史包袱 | 丢掉评测语义资产；工作量与风险最大；无收益抵消 |

**推荐路线**：

- **阶段 0（顺手做）**：修正 `scan_dsh_log`——chunk-runs 解包 + `assistant/message.usage` 解析 + cacheWriteTokens 修正。
- **阶段 1（桥接实时评测）**：`dsh_backend.py`——Python SDK 拉起 DSH、按 task 发 prompt、实时订阅事件流、增量折叠指标（工具成功率 / turn 结束原因 / token / TTFT）、事件流落盘（DSH JSONL 格式，供 `scan_dsh_log` 事后复用）、运行中告警回调。
- **阶段 2（移植指标引擎）**：高频指标移植为 DSH `session-projection` 单元，DSH 内建 `eval` 包，`session/projection` 帧实时推送。
- **阶段 3（评测视图）**：参照 `ui-trajectory` 做评测结果视图，Web GUI 实时看评测轨迹。

---

## 十、风险与边界

1. **DSH 未发布**：`SESSION_FORMAT_VERSION = 0`，事件格式/API 无兼容承诺；阶段 2/3 需锁定 DSH 版本，事件 schema 变化时 `scan_dsh_log` 需同步。
2. **被测对象迁移**：evalkit 现测 Claude Code + Skill 体系；换 DSH agent 后 skill 触发口径需对齐（DSH 是 `tool/call name=='skill'`）。
3. **chunk-runs 解码必须 fail-loud**：DSH 侧解码遇到畸形行会抛错（"malformed ... storage row"），evalkit 解包也应遵循（宁可报错不可静默丢一整段）。
4. **cost.py 反推依赖 airlab pod 日志**：DSH SDK 路径下的计费口径需要另行校准（SDK 使用 provider/model + 官方价）。

---

## 附录：关键文件索引

| 主题 | 路径 |
|---|---|
| 事件类型体系 | `packages/core/session/src/types.ts` L236-333、L404-436 |
| append 热路径 | `packages/core/session/src/index.ts` L604-655 |
| surface 折叠 | `packages/core/session/src/surface.ts` L387-395、L398-460 |
| 会话不变量 | `packages/core/session/src/invariant.ts` L23-30、L55-166 |
| chunk-runs 压缩格式 | `packages/core/session/src/chunk-rows.ts` L64-70、L293-328、L339-346 |
| 持久化协调/写后批 | `packages/session/session-persistence/src/coordinator.ts` L1086-1137；`write-behind.ts` L22-72 |
| JSONL 落盘格式 | `packages/session/session-persistence-jsonl/src/format.ts` L33-64 |
| 查询服务接口 | `packages/session-query/session-query/src/index.ts` L81-357（traceSession L279 / traceEvent L292） |
| 血缘/事件追踪 | `packages/session-query/session-query/src/tracing.ts` L65-105、L113-173 |
| 查询工具 schema | `packages/session-query/tool-session-query/src/index.ts` L86-122；`input.ts` L45-86 |
| FTS5 搜索后端 | `packages/session-query/session-query-sqlite/src/schema.ts` L127-168 |
| 轨迹 UI | `packages/client/ui-trajectory/src/client/*`（snapshot-builder / TrajectoryView / TrajectoryTimeline） |
| SSE 帧类型 | `packages/host/apiproxy/src/api/events.ts` L69-155 |
| mux 流实现 | `packages/host/apiproxy/src/api-proxy.ts` L3429-3532 |
| 会话导出 ZIP | `packages/host/apiproxy/src/session-export.ts` L219-266、L388-457 |
| Python SDK | `python/sdk/src/deepseek_harness/{client,api,models}.py` |
| 轨迹视图（用户视角） | Web GUI "Trajectory" tab（台账 + 瀑布图 + 搜索 + 分页） |
