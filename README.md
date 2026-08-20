# evalkit — 多 Agent × 多模型批量评测平台

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)
![GitHub stars](https://img.shields.io/github/stars/JingW-ui/evalkit?style=flat-square)
![GitHub last commit](https://img.shields.io/github/last-commit/JingW-ui/evalkit?style=flat-square)
![License](https://img.shields.io/github/license/JingW-ui/evalkit?style=flat-square)

面向「远程 Windows PC 自动化」领域的 Agent 能力评测工具：以一整套 L1-L4 能力卷为题库，
**跑测 → 机器预判（确定性规则 + LLM 判结论）→ 统计总览 → 人工复核/答辩**。

- **被测对象**：claude（Claude Code CLI）、codemaker、DSH（DeepSeek Harness）多通道；
- **多模型**：deepseek-v4-pro/flash、claude-opus-4-6、glm-5.2 等经 codemaker 网关混跑对比；
- **判分**：`组合规则树（all/any/not + 原子条件）` 判「过程是否做了」 + `LLM-as-judge` 判「最终结论是否正确报告」，人工复核兜底。

---

## 核心特性

| 能力 | 说明 |
|------|------|
| 题库试卷化 | `papers/*.yaml` 为权威源（git 管理），SQLite `tasks` 表存导入快照 |
| 批量评测 | 前端勾选任务/模型/设备/判分模型 → 串行跑 → 自动判级 + 落库 |
| 机器预判 | 组合规则树（`tool_called / tool_result / tool_ok / tool_success_rate / text_contains / metric / file_exists / llm_judge`）+ LLM 判最终结论 |
| 统计总览 | 按 task×model 聚合 SR + 均值±σ + 95% Wilson 置信下界 + 一票否决 |
| 评测矩阵 | 能力画像（agent×L1-L4 SR）+ 会话明细 |
| 人工复核 | 答辩三态 `pass / fail / invalid`（机器误判纠正 / 题目硬伤排除）留痕 |
| 成本口径 | 统计用**挂牌价估算**（`models_usd`），模型/平台结算价虚高仅参考 |
| 轨迹回放 | 会话轨迹（模型活跃/工具/等待输入/空闲分段）+ 原始日志 |

---

## 快速开始

### 依赖

- Python 3.10+（后端零第三方依赖，仅 stdlib：`http.server` + `sqlite3`）；
- 被测 claude 通道需本机已装 `claude` CLI；UU 取号等设备脚本需 `airtest`/`airtest_ocr`；
- 前端需 Node 20+ + pnpm。

### 1. 构建前端

```bash
cd webui
pnpm install
pnpm run build        # 产物 webui/dist，由 eval_server 直接服务
```

### 2. 启动看板

```bash
python eval_server.py --port 8090 --web webui/dist --batch-root results/batch
```

打开 `http://127.0.0.1:8090`。

### 3. 导入题库

```bash
python -c "from eval_store import EvalStore; s=EvalStore(); s.import_papers(); s.close()"
```

### 4. 跑一次批量评测

前端「批量评测」页选任务（默认全选）→ 选模型 → 选设备 → 发起；或命令行：

```bash
python eval_batch.py run --tasks-dir tasks/gen --backend claude \
    --model deepseek-v4-pro --provider codemaker_deepseek \
    --cwd D:/wy_projects/work_4_log --repeat 2
```

### 5. 回归测试

```bash
python tests/test_eval_rules.py     # 组合规则树评估器
python tests/test_eval_stats.py     # 统计/复核
python tests/test_eval_papers.py    # 题库导入往返
python tests/test_dsh_backend.py    # EventMetrics
```

---

## 题库（papers）

题目 schema（`papers/*.yaml`）：`task_id / title / level / skill_expected / query / device_var /
expected_answer / tools_required / accept_criteria / success_condition / prep / enabled`。

当前 23 题（15 启用 / 5 停用 / 其余为待启用场景）：

| 级别 | 题 |
|------|----|
| L1（6） | connect_check / device_list / env_info / file_exists / process_probe / resolution |
| L2（6） | occupy_push / remote_download / write_read / software_probe / process_manage / session_check |
| L3-UU（3） | uu_take_code（已装取号）/ uu_uninstall（卸载）/ uu_take_code_uninstalled（未装取号） |
| L3/L3-S/L4 | g66 部署 / uu·g66 冒烟 / 不存在设备·进程·文件 / 占用·锁屏（部分停用待回填） |

`papers/references.json` 为实测参考答案（content + 工具链），供题库展示与 LLM 判分的「完整度/稳定属性」基准。

---

## 成功率判定

```
judge_eval
  └─ match_task(query) → success_condition
       └─ _evaluate_rule(all/any/not)
            ├─ 过程证据（确定性）：tool_called / tool_result / tool_ok / tool_success_rate ...
            └─ 结论证据（LLM）：llm_judge（final_text + expected_answer + references.json + query）
```

- **过程是否做了** → 确定性规则（工具链 + 工具成功率，`PowerShell→Bash`、`Glob≈Bash` 等等价路径归一）；
- **结论是否正确报告** → `llm_judge`（默认 `glm-5.2`，前端可选判分模型；`temperature=0` + 强制 JSON）；
- **稳定 vs 易变**：主机名/系统版本/用户/serialno 与参考硬比对；分辨率/软件版本/设备台数/设备ID/验证码 如实报告即可；
- **人工复核兜底**：`pass`（机器误判纠正）/ `fail` / `invalid`（题目硬伤排除），`reason` 留痕可查。

详见 `docx/成功率判定方法.md`。

---

## 目录结构

```
evalkit/
├── eval_server.py        # 看板服务（HTTP + SSE + 会话发现/挂接/轨迹/矩阵/批量）
├── eval_batch.py         # 批量评测闭环（load → run → judge → record）
├── eval_records.py       # judge_eval + 组合规则树评估器
├── eval_store.py         # SQLite 存储（executions + tasks + stats/review/matrix）
├── judge_llm.py          # LLM 判结论（codemaker 网关 / glm-5.2）
├── claude_backend.py     # claude 通道（stream-json 适配 EventMetrics）
├── codemaker_backend.py  # codemaker 会话库通道
├── dsh_backend.py        # DSH 通道 + EventMetrics 指标折叠
├── session_discovery.py  # 会话发现（projects / codemaker / batch / session_root）
├── provider.py / cost.py # 模型提供商 / 成本估算
├── papers/               # 题库权威源（*.yaml + references.json）
├── webui/                # React 19 + Vite 前端
├── assets/img/           # 截图演示图片
├── docx/                 # 文档（开发说明 / 判分方法 / 任务汇报）
├── tests/                # 回归测试
├── tasks/ / results/ / sessions/ / sandbox/
├── tmp/                  # 一次性/临时脚本（git 忽略，如重判/回填/收集）
└── conf.json             # 配置（pricing / provider / judge / aliases）
```

---

## 截图演示

完整界面走查见 **[截图演示](截图演示.md)**（图片位于 `assets/img/`）。

---

## 文档

- [docx/开发说明.md](docx/开发说明.md) — 架构/模块/API/配置
- [docx/成功率判定方法.md](docx/成功率判定方法.md) — 判分机制（规则树 + LLM judge）
- [docx/任务总结汇报.md](docx/任务总结汇报.md) — 跑测结果汇总
- [docx/题库设计与答辩验收标准.md](docx/题库设计与答辩验收标准.md) — 题库与验收标准
- [ROADMAP.md](ROADMAP.md) — 后续计划
