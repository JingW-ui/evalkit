# 批量评测闭环 —— task_gen → claude 执行 → 判级 → 矩阵

> 日期：2026-08-15
> 范围：打通「生成 → 执行 → 判级 → 矩阵」闭环；「批量评测」独立 tab 承载自主触发评测日志。

---

## 一、闭环链路

```
task_gen.py（模板化 L1-L4 任务生成）
        │  生成 tasks/gen/*.json（域 × 级别 × 设备变体）
        ▼
eval_batch.py run（批量执行器）
        │  逐任务经 claude_backend.ClaudeEvalBackend.run_task 执行
        │  （claude --print --output-format stream-json，--permission-mode 可配）
        ▼
        ├─ 会话日志落盘 session_root/<sid>/{raw.jsonl, session.jsonl}
        │        （--session-root results/batch → 看板「批量评测」tab 独立展示）
        ├─ judge_eval(query, sid, metrics, assistant_text, tasks=[task])
        │        任务匹配 → 校验器（evidence_anchor / negative_honesty / file_exists）
        │        未匹配 → 自动推断（L4 启发式 / classify_level）
        ▼
EvalRecords.add() → results/eval_records.json → 看板「评测矩阵」（agent×L1-L4 SR + 明细）
```

## 二、新增/改动

| 文件 | 内容 |
|---|---|
| `eval_batch.py`（新增） | 批量执行器：`load_tasks_from_dir`（域/级过滤、limit）、`run_one_task`（claude/dsh 后端）、`run_batch`（执行+判级+记录+回调）、`print_matrix`；CLI `gen`（调 task_gen）/ `run`（--tasks-dir/--backend/--timeout/--permission-mode/--model/--session-root/--dry-run） |
| `eval_records.py` | `judge_eval(..., tasks=None)`：批量评测显式传本次任务，避免跨任务误匹配 |
| `claude_backend.py` / `dsh_backend.py` | `run_task` 返回新增 `assistant_text`（judge_eval 锚点匹配用） |
| `eval_server.py` | `--batch-root`（缺省 results/batch）；`/api/sessions?scope=batch` 独立列批量会话（不混入主列表） |
| `webui/src/App.jsx` | 新增第三 tab「批量评测」：左栏展示批量会话，点选走同一套评测面板；`getSessions('batch')` 10s 轮询 |
| `webui/src/api.js` | `getSessions(scope='all')` 支持 scope 参数 |
| `tests/test_eval_batch.py`（新增） | 任务加载/过滤、成本折算、judge 带显式任务、dry-run、执行失败容错、print_matrix |

## 三、实测（本机）

- 生成：`eval_batch.py gen --domain g66,uu_remote,airgattai,generic` → 17 基础任务；
  再按三台目标 PC（GIH-D-18125 / GIH-D-18421 / GIH-D-17115）生成设备变体并去重 → 27 任务
  （L1×4 / L2×11 / L3×8 / L4×4，三台设备各 5 条真实设备操作任务）。
- 执行：`eval_batch.py run --tasks-dir tasks/gen --permission-mode bypassPermissions --session-root results/batch`
- 效果：每任务会话落盘 results/batch（看板「批量评测」tab 实时可见），判级结果写入
  eval_records.json，矩阵 tab 展示 claude×L1-L4 成功率。

## 四、已知限制

1. claude `--print` 默认权限模式可能拒绝部分工具调用（headless 下权限请求自动拒绝）——
   L3 写文件类任务需 `--permission-mode acceptEdits` 或 `bypassPermissions`；
2. 批量任务串行执行，耗时 = 任务数 × 单任务时长；大任务集建议 `--limit` 分批；
3. 真实设备操作任务（占用/部署/写备注）有副作用，生成时设备参数需人工确认。
