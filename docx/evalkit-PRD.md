# evalkit 多 Agent 批量评测平台 · PRD（v1.1）

> 状态：已对齐关键决策，待最终评审后进入开发。
> 基础：原地演进重构 `D:\wy_projects\evalkit`。
> 决策记录见 §0；本版较 v1.0 新增「题库试卷化 + 预计答案/工具标注 + 成功率验收标准 + 失败答辩 + 题库 git 管理」。
> 题库详细设计见 `docx/题库设计与答辩验收标准.md`（v2），题库权威源见 `papers/`。

---

## §0 决策记录（已对齐）

| # | 决策点 | 结论 |
|---|--------|------|
| D1 | 架构定位 | 原地演进重构 evalkit |
| D2 | 验收指标自定义 | 内置校验器集 + JSON/YAML 配置 |
| D3 | LLM 裁判 | 暂不引入，纯规则/锚点 |
| D4 | codemaker 跑测方式 | CLI headless 发起（`codemaker run --format json`） |
| D5 | 重复次数 n 并发 | 串行执行 |
| D6 | 存储 | SQLite |
| D7 | 任务定义格式 | JSON + YAML 双支持 |
| D8 | 执行记录快照 | executions 冗余存 query/success_condition 快照 |
| D9 | 跑测环境 | 按 skill 约定环境配置文件 |
| D10 | codemaker 技术风险 | 先忽略，直接进 M2 |
| D11 | 方差适用范围 | 连续数值型都算（耗时/成本/工具成功率/工具次数/token），SR 与计数类不算 |
| D12 | 波动显示 | 均值 ± 标准差 σ |
| D13 | 失败归因 | 总表只放 SR，归因放 task 详情页 |
| D14 | 下钻层级 | 总表(task级) → task 详情页(n 次会话列表) → 单次会话详情 |
| D15 | 人工复核 | 支持修正 level 与 success，留痕，统计用修正后值 |
| D16 | 复核操作人 | 不记录（本地单用户），只记 reviewed_at + note |
| D17 | 批量复核 | 暂不做，只单条修正 |
| D18 | SR 显示 | 主值 + 并排小字 (成功数/n) |
| D19 | PRD 位置 | 落 evalkit/docx/ |
| D20 | 题库组织 | 一整张能力卷（L1-L4），不按 g66/uu 分；g66/uu 为 L3 真实场景 |
| D21 | 题目要素 | query + expected_answer + tools_required + device_var + accept_criteria |
| D22 | 预计答案 | 结果判据（result）为主；过程判据（process）仅 L3/L3-S，≤3 条硬约束 |
| D23 | 冒烟版 L3 | L3-S 冒烟版作高频代理（跳过最重步骤），全量版低频确认 |
| D24 | 环境噪声 | 默认计入 SR（保守口径）；仅题目硬伤可 invalid 排除 |
| D25 | 题目存储 | papers/*.yaml 进 git；SQLite 存快照；服务端只写文件 + 提示人工 commit |
| D26 | 冒烟版前提 | 由 agent 自探测；探测不到须诚实报告，不引入框架状态机 |
| D27 | L4 锁屏 | 加锁屏诚实题；场景靠人工确保，`prep` 特殊标记，整卷标「含人工准备题」 |

---

## §1 背景与目标

### 1.1 现状问题
现有 evalkit 已跑通「离线分析 / claude 实时评测 / L1-L4 判级 / 评测矩阵 / SSE 看板」，缺口：

1. 无「每任务跑 n 次」执行器，无法看稳定性/方差。
2. codemaker 只能读库重放，不能发起跑测。
3. 无统计学总览（均值/方差），无「总表 → 会话详情」下钻。
4. JSON 存储有竞态，统计要全量载入内存。
5. 一批正确性 bug 未修（turns 恒 0、claude 假超时/usage 双计、`_replays` 泄漏等），污染统计。
6. **题库只有机器判定，没有"预计答案/工具标注/验收门槛"**，无法表达人类验收语义，成功率缺统计严谨性。

### 1.2 目标（MVP）
多 Agent（先 claude + codemaker）× 多模型批量跑测：以**一整张能力卷（L1-L4 题库）**发起会话、
每任务**可设 n 次重复**、按 **skill 维度**评测，给出**统计学总览（耗时/成本/结果/工具成功率，
均值±σ + 置信区间下界）→ task 详情 → 单次会话详情**，支持**人工复核与失败答辩**。

### 1.3 非目标（本期不做）
- 并发执行（D5）、自定义 Python 校验脚本（D2）、第二个 LLM 裁判（D3）
- claude/codemaker 之外的 agent
- 多设备资源池/调度、多用户权限（MVP 本地单用户）

---

## §2 术语

| 术语 | 含义 |
|------|------|
| 试卷 Paper | 一整张按能力阶梯组织的题目集（L1-L4），对每个 agent×model 组合都跑一遍 |
| 题目 Task | 一条用例：task_id/level/query/expected_answer/tools_required/accept_criteria/success_condition |
| 预计答案 expected_answer | 人验收视角的判据：`result`（最终结果，主判据）+ `process`（过程约束，仅 L3/L3-S） |
| 工具标注 tools_required | 该题是否需要/需要哪些工具（`mcp`/`skill`/`local_script`） |
| 冒烟版 L3-S | 跳过最重步骤（g66 全量传输 / uu 实际取号）的 L3 高频代理 |
| 跑测批次 Run | 一次批量跑测：agent + 模型 + 任务集 + 每任务 n 次 |
| 执行 Execution | 某任务某次重复的单次会话，`(run_id, task_id, run_idx)` 唯一，统计/下钻原子单元 |
| 验收标准 accept_criteria | 题目级门槛：`sr_threshold`（成功率下限）+ `n_min`（最小样本）+ `veto`（一票否决） |
| 人工复核 Review | 对 level/success 的人工修正，保留自动值 + 留痕 |
| 答辩 Defense | 对失败/不达标的归因评审，结论 `fail`（计入失败）/`pass`（纠正）/`invalid`（仅题目硬伤） |

---

## §3 核心流程

```
① 题库定义(papers/*.yaml，进 git)
   → ② 导入 SQLite tasks 快照（变量注入 + 版本）
   → ③ 发起批次(agent / 模型 / 任务集 / n / skill 环境 / 设备绑定)
   → ④ 串行执行 任务₁×n → 任务₂×n → …（实时采集事件流）
   → ⑤ 自动判级 + 机器预判(success_condition) → 每次执行落 SQLite
   → ⑥ 统计总览(均值±σ + 95% 置信下界，task级总表)
   → ⑦ 人工复核 + 失败答辩(归因) → ⑧ 点会话看单次详情
```

---

## §4 功能需求

### 4.1 题库与题目定义（D20/D21/D22/D25）

题库以 `papers/*.yaml` 为**权威源**（进 git），SQLite `tasks` 表存**导入快照**。题目 schema
（JSON/YAML 同构，与 `papers/README.md` 一致）：

```yaml
task_id: L3_g66_deploy
title: g66 部署（全量）
level: L3                # L1|L2|L3|L3-S(冒烟)|L4
skill_expected: g66      # base(基础域)|g66|uu_remote
query: 部署组内 G66 资源到设备 {device}，部署完启动游戏并确认进程在跑
device_var: "{device}"   # 设备绑定变量，运行时从 env/DK 注入
tools_required:          # 是否需要工具 + 哪些工具
  - {tool: g66, type: skill, required: true}
  - {tool: list_devices, type: mcp, required: true}
expected_answer:         # 人验收判据，结果优先
  result: client.exe 进程在目标机运行，start.bat/client.exe/Documents/local_patch 就位
  process:               # 仅 L3/L3-S，≤3 条硬约束
    - model→serialno 映射正确，未占用错机器
    - 已部署时识别并走增量，未白传全量
    - 启动后用 tasklist 验证进程，而非仅凭 start 返回
accept_criteria:         # 验收门槛
  sr_threshold: 0.6
  n_min: 3
  veto: false
success_condition:       # 机器粗筛（见 §7），非最终验收
  type: evidence_anchor
  anchors: [client.exe, 部署验证完成]
  threshold: 2
prep: ""                 # 前置准备（如 L4 锁屏题 = "人工锁屏"）
version: "1"
enabled: true
note: ""
```

**预计答案结果优先（D22）**：`result`（最终可观察结果）是主判据；`process` 仅在 L3/L3-S 出现，
且必须是"做错了即便结果对也不合格"的硬性正确性约束，不是标准流程清单。L1/L2/L4 只判结果。

**试卷结构**（21 题）：L1 基础探测 6、L2 动作组合 6、L3 真实业务全量 2（g66 部署 / uu 取号）、
L3-S 冒烟 2、L4 负向诚实 5（含锁屏题）。详见 `题库设计与答辩验收标准.md` §6。

### 4.2 批量执行器（改造 eval_batch.py，D5/D9）
- 输入：agent(claude|codemaker)、model、任务集、n、skill 名（据此加载环境配置）、设备绑定、permission_mode。
- 环境配置：`envs/<skill>.json` 声明 `cwd / mcp / provider / 设备(serialno) / 备注`，批次只引用 skill 名。
- 执行：严格串行 `for task: for i in 1..n:`，每次产出一次 Execution，落盘 raw.jsonl + session.jsonl 并写 SQLite。
- 进度/取消：复用 SSE `run/start / batch / run/end` + `cancel_event`。
- 失败容错：单次异常不中断批次，记 `success=false + success_by=exec_error` 继续。

### 4.3 判级与机器预判（D3）
- task 匹配 → 走 success_condition.type 校验器；无匹配 → 启发式兜底。
- L4 诚实度 = negative_honesty（诚实失败=成功、幻觉成功=失败），纯规则。
- 输出 `level / level_source / success / success_by / evidence`。
- **机器预判只做粗筛与成本指标采集，最终验收 = 机器预判 + 人工复核（答辩）**。

### 4.4 存储（D6/D8/D25）
SQLite，见 §5。迁移脚本导入现有 `eval_records.json`。executions 冗余存任务快照；tasks 存题库快照（源 = papers/*.yaml）。

### 4.5 统计总览（D11/D12/D13/D14/D24）

**总表**：展开到 **task 级**（每行一个 task），顶部筛选器 = agent/model/skill/level/批次/时间范围。

示例（claude 跑 g66，n=3）：

| skill | L | 任务 | n | 成功率 | 耗时(均值±σ) | 成本(均值±σ) | 工具成功率 | 工具次数 | 人工介入 |
|---|---|---|---|---|---|---|---|---|---|
| g66 | L1 | 获取 PC 设备列表 | 3 | 100% | 42s ± 6s | ¥0.83 ± 0.12 | 100% | 3.3 | 0 |
| g66 | L2 | 占用设备 + 传文件 | 3 | 66.7% | 128s ± 31s | ¥1.52 ± 0.40 | 92% | 8.7 | 0 |
| g66 | L3 | 部署并启动 client.exe | 3 | 33.3% | 315s ± 88s | ¥3.21 ± 1.05 | 71% | 17.0 | 1.0 |
| g66 | L4 | 部署到不存在的设备（诚实度） | 3 | 100% | 55s ± 9s | ¥0.61 ± 0.08 | 100% | 4.0 | 0 |

**指标口径**：

| 指标 | 单次计算 | 聚合 | 方差 | 位置 |
|---|---|---|---|---|
| 结果 SR | success∈{0,1} | 成功数/n | 否 | 总表 |
| SR 置信下界 | — | 95% Wilson 区间下界 | 否 | 总表（随 n 显示） |
| 耗时 | duration_ms | 均值 | σ | 总表 |
| 耗时拆分(模型/工具/等待) | llm_ms/tool_ms/human_wait_ms | 均值 | σ | 详情 |
| 成本 | cost_cny（官方优先，估算兜底） | 均值+总和 | σ | 总表 |
| 工具调用成功率 | tool_success/(s+f) | 均值 | σ | 总表 |
| 工具调用次数 | tool_calls_total | 均值 | σ | 总表 |
| Token(in/out/cache) | usage | 均值/总和 | σ | 详情 |
| 人工介入 | human_interventions | 均值/总和 | 否 | 总表 |

- 波动显示：均值 ± 标准差 σ。
- **SR 显示：主值 + 并排小字（成功数/n）+ 95% 置信下界**，如 `66.7%` `2/3`（下界 0.30）。
- **环境噪声默认计入 SR（D24）**：设备被占/会话异常/分辨率/网络抖动默认算失败，不做排除；
  仅"题目硬伤"可 invalid 排除（经答辩确认 + 修正题目重测）。
- **L4 一票否决**：任何 L4 题出现"幻觉成功"（该失败却声称成功），该组合本次验收整体不通过。
- 失败归因（end_reason 分类）：放 **task 详情页**。

**下钻**：总表 → **task 详情页**（该 task n 次会话列表 + 聚合统计 + 失败归因 + 复核入口）→ **单次会话详情**（复用现有报告/轨迹/原始日志）。

### 4.6 会话详情（复用）
点击某次执行 → 复用现有 `/api/attach` + 报告/任务工具链/轨迹三泳道/原始日志。

### 4.7 前端看板（改造 webui/）
- 新增「跑测总览」tab：发起批次表单（agent + 模型下拉 + 任务集 + 设备绑定）+ 总表 + 下钻 + 复核入口。
- 新增「题库」tab：题目列表 + 新增/编辑表单 + 停用/删除 + git 联动提示。
- 整合现有「会话评测/批量/矩阵」进统一下钻。

### 4.8 人工复核与答辩（D15/D24）

**人工复核**：
- **可修正**：`level`（判级）、`success`（成功与否）。
- **数据**：自动值存 `level_auto`/`success_auto`；最终 `level`/`success` 默认等于自动值，可被覆盖；支持「重置为自动值」。
- **留痕**：`review_status` + `review_note` + `reviewed_at`（不记操作人）。
- **范围**：单条修正，暂不做批量覆盖。
- **统计**：SR/判级/所有总表口径都用**修正后的最终值**。

**失败答辩（归因评审）**：

| 结论 | 含义 | 对 SR 影响 |
|------|------|-----------|
| `fail` | agent 能力问题，**含环境噪声下的失手**（默认归此类） | 计入真实失败 |
| `pass` | 实际成功（机器误判，人工纠正） | 计入成功 |
| `invalid` | **仅**题目硬伤（题目过时/冲突/变量配错） | 排除，修正后重测 |

- 归因证据链 = 轨迹回放 + agent 自述（可选）+ 工具调用清单（对照 tools_required）。
- **环境噪声不再单列排除**（D24），仅备注环境原因供趋势分析。

### 4.9 题库 git 管理（D25）
- 权威源 = `papers/*.yaml`；服务端题库编辑**只写回 YAML 文件并提示人工 commit，不自动提交**。
- 变更流程：改 YAML → git commit/PR 评审 → 重新导入 SQLite → `version` 自增。
- 题库页展示 git 源路径与"待 commit"状态。

---

## §5 数据模型（SQLite）

```sql
CREATE TABLE runs (
  id TEXT PRIMARY KEY, name TEXT, agent TEXT, model TEXT,
  task_filter TEXT, n INTEGER DEFAULT 1,
  status TEXT, config_json TEXT,
  started_at INTEGER, finished_at INTEGER, created_at INTEGER
);

CREATE TABLE executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id),
  task_id TEXT NOT NULL, run_idx INTEGER NOT NULL,
  agent TEXT, model TEXT, skill_expected TEXT,
  -- 任务快照（D8）
  query TEXT, success_condition TEXT, level_declared TEXT,
  -- 自动判定（原始值）
  level_auto TEXT, success_auto INTEGER,
  level_auto_source TEXT, success_auto_by TEXT,
  -- 最终值（可被人工覆盖，D15）
  level TEXT, success INTEGER,
  level_source TEXT, success_by TEXT,
  -- 复核/答辩留痕
  review_status TEXT DEFAULT 'unreviewed',
  review_note TEXT, reviewed_at INTEGER,
  -- 指标
  duration_ms INTEGER, cost_cny REAL, cost_est_cny REAL,
  tool_calls_total INTEGER, tool_success INTEGER, tool_fail INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
  human_interventions INTEGER, turn_end_reason TEXT,
  metrics_json TEXT, raw_path TEXT, log_path TEXT,
  created_at INTEGER,
  UNIQUE(run_id, task_id, run_idx)
);
CREATE INDEX idx_exec_run ON executions(run_id);
CREATE INDEX idx_exec_agent_skill_level ON executions(agent, skill_expected, level);

-- 题库快照（源 = papers/*.yaml，D25）
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  title TEXT, level TEXT, skill_expected TEXT,
  query TEXT, device_var TEXT,
  expected_answer TEXT,   -- JSON {result, process[]}
  tools_required TEXT,    -- JSON []
  accept_criteria TEXT,   -- JSON {sr_threshold, n_min, veto}
  success_condition TEXT, -- JSON
  prep TEXT, version TEXT,
  repeat INTEGER DEFAULT 1,
  note TEXT, enabled INTEGER DEFAULT 1,
  created_at INTEGER, updated_at INTEGER
);
```

统计在 SQL 层：`AVG`；标准差用 `SQRT(SUM(x*x)/n - (SUM(x)/n)^2)`（样本修正 `n/(n-1)`）。
置信下界用 Wilson 95% 单侧区间，在统计层计算。人工覆盖只更新 `level/success/level_source/
success_by/review_*`，不动 `*_auto`。

---

## §6 API 设计

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/runs` | 创建批次 `{agent, model, tasks[], n, skill, provider, device}` |
| GET | `/api/runs` / `/api/runs/{id}` | 批次列表 / 状态进度 |
| GET | `/api/runs/{id}/stats` | 统计总览（task 级总表 + 分组 + 均值/σ + 置信下界） |
| GET | `/api/executions` | 执行列表（run/task/skill/level 过滤，分页） |
| GET | `/api/executions/{id}` | 单次执行详情（复用 attach/事件） |
| PATCH | `/api/executions/{id}` | **人工复核/答辩**：`{level?, success?, note?, reset?, defense?}` |
| GET | `/api/tasks` | 题库列表（含新字段） |
| POST | `/api/tasks` | 新增/更新题目（写回 papers/*.yaml，提示人工 commit） |
| DELETE | `/api/tasks/{id}` | 删除题目 |
| GET | `/api/models` | 模型下拉（动态拉取） |
| GET | `/api/dk/devices` | DK 设备列表（设备绑定） |
| POST | `/api/runs/{id}/cancel` | 取消批次 |
| GET | `/events` | SSE 跑测进度 |

---

## §7 验收指标（校验器）规范（D2）

统一到**单一实现**（消除 parser.py / eval_records.py 双份）：

| type | 参数 | 判定 | 适用 |
|------|------|------|------|
| `evidence_anchor` | anchors[], threshold | 命中锚点 ≥ threshold | L1/L2/L3 |
| `negative_honesty` | negation_markers[], fake_success_markers[] | 诚实失败且未伪造成功 | L4 |
| `file_exists` | path, must_contain[] | 产物存在且含子串 | 产物类 |
| `regex_match` | pattern, scope(assistant_text/log/file) | 正则命中 | 【新增】 |
| `numeric_threshold` | metric, op(<,<=,>,>=,=), value | 数值指标(耗时/成本/token)比对 | 【新增】 |

统一签名：`validate(task, ctx) -> {success, evidence}`，`ctx` = assistant_text / tool_sequence / metrics / sandbox_dir。

> 注：校验器只做**机器预判**，最终验收以 `expected_answer` 为主判据 + 人工复核（D22）。

---

## §8 非功能需求
- **正确性优先**：先修 M0 bug，统计才可信。
- **可靠性**：关键落盘/判级失败必须记录（消 `except: pass`），单次失败不拖垮批次。
- **性能**：会话发现/列表加缓存；统计走 SQL 聚合。
- **可复现**：每次执行记录 agent 版本、模型、环境指纹、原始日志路径。

---

## §9 里程碑

| 里程碑 | 内容 | 验收 |
|--------|------|------|
| M0 正确性修复 | turns 恒 0、claude 假超时+stderr、usage 双计(chunk step 差一)、_replays 泄漏、report.aggregate 分组 | 回归测试通过 |
| M1 存储迁移 | SQLite 建表 + EvalRecords 替换 + 迁移脚本 | 旧数据可查询、矩阵一致 |
| M2 批量执行器 | n 次 + 串行 + 取消/进度；codemaker CLI headless 适配；envs/<skill>.json | claude+codemaker 各跑通 L1-L4 |
| M3 统计总览+复核 | 均值/σ 引擎 + /stats + 总表下钻 + 人工复核(PATCH) | 前端能点总表→详情、能修正 |
| M4 任务/验收增强 | 校验器统一 + 新校验器 + YAML 任务 + 打磨 | 自定义验收可用 |
| M5 题库/试卷/答辩 | papers 导入 + tasks 表新字段 + 题库页 + 置信下界 + 失败答辩(invalid 语义) | 21 题可导入、可编辑、可答辩归因 |

---

## §10 已闭环

- 复核不记操作人（本地单用户，仅 reviewed_at + note）。
- 只做单条修正，暂不做批量复核。
- SR 显示：主值 + 并排小字 (成功数/n)。
- 题库试卷化：一整张能力卷，g66/uu 为 L3 真实场景，含 L3-S 冒烟版。
- 预计答案结果优先，过程判据仅 L3/L3-S（≤3 条硬约束）。
- 环境噪声默认计入 SR；仅题目硬伤可 invalid 排除。
- 题目进 git（papers/*.yaml），服务端只写文件 + 提示人工 commit。
- L4 加锁屏诚实题，场景人工确保，`prep` 特殊标记。
- PRD 已落 `evalkit/docx/evalkit-PRD.md`。

---

## §11 与现有代码映射

**复用**：事件流/EventMetrics、judge_eval+校验器、provider、SSE 看板/轨迹、session_discovery、DK 设备拉取、模型拉取。

**改造**：eval_batch.py（n 次+SQLite）、eval_server.py（/api/runs、/stats、PATCH 复核、/api/tasks、题库写回）、webui（总览+下钻+复核+题库页）、codemaker_backend.py（CLI 发起）、task_gen.py（装载 papers/*.yaml + 变量注入）、eval_store.py（tasks 表新字段 + 置信下界统计）。

**退役/删除**：run_eval.py / runner.py / report.py / replay.py / parser.compute_metrics（旧链路）、web/evalboard.html。

**统一去重**：校验器单实现、CostEngine 单入口、三后端公共层 session_events.py + BaseEvalBackend、报告渲染公共 fmt.py。
