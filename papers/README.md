# papers/ — 题库权威源（git 管理）

本目录是评测题库的**权威源**，以 YAML 文件形式维护，进 git 版本管理。
SQLite `tasks` 表只存导入快照供批次执行读取（见 `eval_store.py` 的 `upsert_task`）。

## 字段说明（详见 docx/题库设计与答辩验收标准.md §4.2）

| 字段 | 含义 |
|------|------|
| `task_id` | 唯一 id |
| `title` | 题目名（人工可读） |
| `level` | L1 / L2 / L3 / L3-S（冒烟）/ L4 |
| `skill_expected` | `base`（基础域）/ `g66` / `uu_remote` |
| `query` | 输入 prompt，可含 `{device}` / `{file}` / `{dir}` / `{url}` / `{code}` / `{fake_device}` 变量 |
| `device_var` | 设备绑定变量（默认 `{device}`），运行时从 env/DK 注入 |
| `tools_required` | 是否需要工具：`type` 枚举 `skill`/`mcp`/`local_script`，`required` 布尔 |
| `expected_answer` | `result`（主判据，最终可观察结果）+ `process`（仅 L3/L3-S，≤3 条硬性约束） |
| `accept_criteria` | `sr_threshold`（发布成功率下限）、`n_min`（最小样本）、`veto`（一票否决） |
| `success_condition` | 机器粗筛（evidence_anchor / file_exists / negative_honesty），非最终验收 |
| `prep` | 前置准备（跑题前由评测方执行的场景准备，如"人工锁屏"） |
| `version` / `enabled` | git 源版本 / 是否启用 |

## 目录结构

```
L1/   基础探测（6 题）
L2/   动作组合（6 题）
L3/   真实业务全量（g66 部署、uu 取号）
L3-S/ 真实业务冒烟（高频代理指标）
L4/   负向诚实（5 题，veto）
```

## 约定

- **落盘方式**：服务端只写回 YAML 文件并提示人工 `git commit`，不自动提交。
- **结果优先**：`expected_answer.result` 是主判据；`process` 只在 L3/L3-S 出现。
- **变量运行时绑定**：`{device}` 等占位符在批次运行时注入真实值（见文档 §5）。
