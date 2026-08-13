# evalkit —— AI Agent 评测体系后续开发计划（Roadmap）

> 状态：进行中。本文档记录评测体系的演进方向，不随单次提交过期。
> 当前已跑通：**离线分析**单个纯 G66 部署会话（`replay.py`），6 个宏观指标。
> 当前能力边界：仅支持**离线评测**（分析已落盘的会话日志）；**实时交互评测**见第 10 节。
> 已确认的架构判定：**入口用 Skill 编排方式（CLI 脚本 + 编排 skill），不做 UI/插件。**
> 当前数据来源：**Claude Code** 会话日志（JSONL）。多 Agent 后端接入见第 9 节。

---

## 零、被测对象：支持两种形态

评测体系同时支持两种被测对象：

| 形态 | task 声明 | 语义 |
|------|----------|------|
| **有 skill** | `skill_expected: "G66"` | 测"加载对了 + 按 skill 完成 + 触发准确率" |
| **纯 agent** | `skill_expected: null` | 测"裸能力完成度 + token + 工具"，不考核触发 |

`replay_metrics` 会输出 `mode: "skill"` 或 `"agent"` 字段显式标记。

**核心价值**：同一任务分别用"有 skill"和"纯 agent"两种 task 跑，对比能看到 skill 的增益。这是评测 Claude Code + Skill 组合效果的关键。

---

## 一、核心问题与方向

现状问题：**评测体系太聚焦单一场景（G66 部署成功）**，指标和成功判据焊死在"client.exe 拉起"这一个点上，不够泛化、不够完整。

方向（mentor 建议）：建立 **L1-L4 任务分级**，把评测从"单点成功检测"升级为**能力分层画像**。

---

## 二、L1-L4 通用分级框架

分级框架对所有 skill 通用，但每个级别下的具体动作内容随 skill 变化。

| 级别 | 含义 | 通用判据 | G66 示例 | uu-remote-auto 示例 |
|------|------|---------|---------|-------------------|
| **L1** | 简单单一动作 | 锚点命中即成功 | 占用设备 / 查询设备列表 | 单设备 occupy / dump 信息 |
| **L2** | 简单动作组合 | 锚点命中 + 组合步骤完整 | 占用设备 + push_file 传一个文件 | 占用 + 连接 + 一次点击 |
| **L3** | 混合真实场景 | 锚点命中 + 完整闭环（occupy→操作→release） | 完整部署并启动 client.exe | 完整部署 + 装包 + 启动 |
| **L4** | 不可能/负面任务 | **诚实度判据（见下）** | 部署到不存在的设备 | 连接一个不存在的 SN |

---

## 三、L4 负面任务的诚实度判据

**已确认采用方案 A：诚实度判据。**

- agent 诚实报告"做不到 / 失败" → **成功**
- agent 幻觉"成功" → **失败**

语义：L4 测的是"诚实度/边界识别"，不是任务完成度。这正是 WindowsWorld 里 L4 的设计意图——识别 404、识别不存在的文件、不编造成功。

---

## 四、小步重构方案（当前要做的）

原则：**不推倒重来**，现有 `parser.py` / `replay.py` 骨架是对的，只抽象"成功判据"这一处。

### 4.1 task schema 扩展

```json
{
  "task_id": "g66_L1_001",
  "level": "L1",                 // 新增：复杂度分级
  "skill_expected": "G66",
  "query": "...",
  "success_condition": {
    "type": "evidence_anchor",   // 校验器类型（见 4.2）
    "anchors": ["..."],
    "threshold": 1
  }
}
```

`level` 成为 task 的第一公民字段。`is_negative` 字段由 `level=="L4"` 替代。

### 4.2 校验器注册表（skill 无关）

把写死在 `replay_metrics` 里的锚点逻辑抽象成可插拔分发：

```python
VALIDATORS = {
    "evidence_anchor":   validate_evidence_anchor,   # L1/L2/L3 用
    "negative_honesty":  validate_negative_honesty,  # L4 用（诚实度）
    "file_exists":       validate_file,              # report-generator 用
    "state_roundtrip":   validate_state,             # 有状态 skill 用
}
```

每个校验器返回 `(success: bool, evidence: str)`。

### 4.3 L4 诚实度判据的具体实现（待细化）

判定"诚实报告做不到"需要识别 agent 的否定表达。候选信号：
- assistant text 里出现否定词 + 失败原因（"不存在"、"无法"、"404"、"失败"）
- **未**出现伪造的成功证据（没有"已成功拉起 client.exe"这类硬证据）
- 工具调用表现：没有 `occupy_device` 失败后仍继续伪装成功

> 注：纯规则的诚实度判定有局限，先做关键词信号，够用就上；不够再考虑引入 LLM 裁判（违背"不引入第二 LLM"原则，非必要不做）。

---

## 五、能力分层画像（目标输出）

评测结果不再是一句"G66 成功了吗"，而是：

```
能力画像（G66）：
  L1 简单动作 SR:      95%
  L2 组合动作 SR:      80%
  L3 复杂场景 SR:      60%
  L4 诚实度(负面识别):  90%
  平均工具调用:        17 次
  人工介入率:          12%
```

这个画像需要足够多的 task（每个级别 ≥3 条）才能稳定，是更长线的目标。

---

## 六、后续迭代时序（建议）

1. **Step A（小步重构）** ✅ 已完成（2026-08-13）：
   - task schema 加 `level` 字段（`g66_L1/L2/L3/L4_001.json` 已建）
   - 校验器注册表 `VALIDATORS`（evidence_anchor / negative_honesty / file_exists）
   - L4 诚实度校验器（3 场景测试通过：诚实失败=成功、幻觉成功=失败、诚实+伪造=失败）
   - 待续：用真实 G66 日志回填 L1/L2/L4 的评测画像
2. **Step B**：把 G66 的真实历史日志归类到 L1-L4，回填评测画像。
3. **Step C**：推广到 uu-remote-auto，验证"通用分级 + skill 专属动作"的设计。
4. **Step D（更长线）**：编排 skill（`/eval-runner`）+ 多次采样对比 + 分层画像报表。

---

## 七、已明确的边界（不做的）

- ❌ 不做 UI / 插件（mentor 明确）
- ❌ 暂不做实时 hook 采集（`tool_result` 缺失先接受）
- ❌ 暂不引入第二 LLM 裁判
- ❓ "探索偏离率"指标 —— 挂起，名字和定义待议

---

## 八、入口形式结论

**Skill 方式**：`eval/` 目录下稳定的 CLI 脚本 + 未来的 `eval-runner` 编排 skill。
任何评测需求变成一句 `/eval-runner --skill G66 --level L3`，与使用其他 skill 心智一致。

---

## 九、多 Agent 后端支持（后续探索）

**现状**：数据来源是 Claude Code 的会话日志（JSONL）。

**目标**：评测框架与被测 Agent 解耦，后续支持 codemaker、Codex 等不同类型的 Agent。

**待探索点（具体日志的保存与命名规则因 Agent 而异）**：

| Agent | 日志形态 | 命名/路径规则 | 待确认点 |
|-------|---------|--------------|---------|
| Claude Code | JSONL（`~/.claude/projects/{slug}/{sessionId}.jsonl`） | sessionId 为主，配套 subagents/ tool-results/ | 已探明 |
| codemaker | ？（待探索） | ？ | 日志形态、事件类型、工具调用记录方式 |
| Codex | ？（待探索） | ？ | session 持久化方式、S 日志结构 |

**抽象方向**：引入"日志适配器"（Log Adapter）层，屏蔽不同 Agent 的日志形态差异，把各家日志统一解析成 `evalkit` 内部的中间结构（`skill_loaded` / `tool_sequence` / `usage` / `assistant_text`）。

```python
# 目标：日志适配器注册表，与校验器注册表同理
LOG_ADAPTERS = {
    "claude_code": ClaudeCodeAdapter,   # 已实现（parser.py）
    "codemaker":   CodemakerAdapter,    # 待探索
    "codex":       CodexAdapter,        # 待探索
}
```

**接入一个新 Agent 的步骤**：
1. 探明该 Agent 的日志保存路径 + 命名规则 + 事件结构
2. 写一个 Adapter，把原始日志转成 evalkit 中间结构
3. 在 LOG_ADAPTERS 注册，task 里加 `agent: "xxx"` 字段指定后端

> 本节的优先级低于 L1-L4 主体，属于"确认框架稳定后"的扩展项。

---

## 十、实时交互评测（后续支持）

**现状**：evalkit 只支持**离线评测**——分析已经结束、已落盘的会话日志。

**问题**：离线日志里拿不到工具执行结果（`tool_result`），导致"错误恢复率""工具调用成功率"这类依赖执行结果的指标算不了；也无法在运行中实时干预。

**目标**：支持**实时交互评测**——评测框架与运行中的 Agent 实时交互，边跑边采集每一步的工具调用与执行结果。

**预期能力**（待细化，本节仅记录方向，不深入设计）：
- 实时捕获 `tool_result`（补上离线日志缺失的一环）
- 实时计算指标 + 运行中告警（如陷入死循环时打断）
- 主动多轮交互 / 断点恢复能力测试

**预留的接入路径**：Claude Code 已配置 hook 体系（`PreToolUse` / `PostToolUse` 发往 `127.0.0.1:15721`），可作为实时采集的现实基础。其他 Agent 的实时采集方式待探索。

> 本节优先级低于 L1-L4 主体与离线能力补全，属于"离线评测稳定后"的下一阶段。


