# evalkit

通用的 AI Agent 评测工具箱。

## 是什么

`evalkit` 通过读取 Agent 的会话日志，分析一次会话的**宏观指标**，回答：

- 有没有在对的场景加载正确的技能/工具？
- 任务有没有完成（采信 agent 自证，不做二次裁决）？
- Token 消耗多少？
- 有几个人工介入、几轮对话、几个工具调用？

当前支持**离线评测**（分析已落盘的会话日志）+ **DSH 实时评测后端**（`dsh_backend.py`，阶段 1）：
基于 DeepSeek Harness 的事件流实时采集 `tool_result`/`turn/end reason`/TTFT 等离线拿不到的信号，
实时折叠指标、运行中告警、事件流落盘。详见 `docs/dsh-integration-research.md`。

被测对象与具体 Agent 实现解耦，两种形态：

| 形态 | task 声明 | 测什么 |
|------|----------|--------|
| 有技能 | `skill_expected: "G66"` | 加载对了没 + 按技能流程完成 + 触发准确率 |
| 裸能力 | `skill_expected: null` | 裸能力完成度 + token + 工具，不考核触发 |

> 当前数据来源为 **Claude Code** 的会话日志（JSONL）。日志的存储路径与命名规则因 Agent 而异，多 Agent 后端的接入见 `ROADMAP.md`。

## 目录结构

```
evalkit/
├── replay.py            # 离线分析单条会话 → JSON + Markdown
├── run_eval.py          # 驱动 Agent 跑评测（正向）
├── run_replay_batch.py  # 批量离线分析多个会话
├── dsh_backend.py       # DSH 实时评测后端（阶段 1，需 deepseek-harness SDK）
├── claude_backend.py    # Claude Code 实时评测后端（通道 A：stream-json，零依赖）
├── eval_server.py       # 实时评测看板服务（R3：React 前端 + 会话发现 + 挂接 + raw 浏览）
├── session_discovery.py # 会话发现器（samples 目录 / 手动路径 / Claude projects / session_root）
├── tail_attach.py       # 外部 Claude 会话 JSONL 实时尾随挂接
├── sessions/            # 受限会话样例目录（10 个：claude ×4 / dsh ×3 / airlab ×3）
├── webui/               # React 前端（Vite 构建 → dist/，由 eval_server 服务）
├── web/evalboard.html   # 旧版原生单页看板（弃用中，可删除）
├── parser.py            # 会话日志解析 + 校验器注册表
├── report.py            # 聚合+报表
├── runner.py            # 子进程驱动 Agent 执行
├── conf.json            # 费率配置
├── tasks/               # 测试用例（L1-L4 分级 + skill_expected）
├── results/             # 输出（git 忽略）
├── docs/                # 调研文档（dsh-integration-research.md / claude-backend-research.md）
├── tests/               # 回归测试（scan_dsh_log / dsh_backend / claude_backend / eval_server）
├── sandbox/             # 运行沙盒（git 忽略）
└── ROADMAP.md           # 后续开发计划
```

## 快速开始

### 离线分析一条已存在的会话日志

```bash
python replay.py --jsonl "<会话日志路径>" --task g66_L3_001
```

产出：
- `results/replay_<task_id>.json` — 结构化指标
- `results/replay_<task_id>.md` — 人看的报表

### 批量分析

```bash
python run_replay_batch.py --scan --skill G66 --task g66_L3_001
```

## 任务分级（L1-L4）

通用分级框架，具体动作随被测技能变：

| 级别 | 含义 |
|------|------|
| L1 | 简单单一动作 |
| L2 | 简单动作组合 |
| L3 | 混合真实场景 |
| L4 | 不可能/负面任务（诚实度判据） |

详见 `ROADMAP.md`。

## 成功判据

采用校验器注册表（与被测技能无关），按 task 的 `success_condition.type` 分发：

- `evidence_anchor` — 证据锚点匹配（L1/L2/L3）
- `negative_honesty` — 诚实度判据（L4，诚实报告失败=成功）
- `file_exists` — 文件产物校验

采信 agent 自证，不引入第二个 LLM 裁判。
