#!/usr/bin/env python3
"""
eval_batch.py — 批量评测执行器：task → agent 执行 → 判级 → 评测记录 → 矩阵。

打通「生成 → 执行 → 判级 → 矩阵」闭环：
  1. 加载任务（tasks/gen/ 或任意目录，task_gen 生成物 / 手写均可）；
  2. 逐个任务经 agent 后端执行（默认 claude 通道，--output-format stream-json）；
  3. 判级 judge_eval（task 匹配 → 校验器；无匹配 → 自动推断）；
  4. 写入 EvalRecords（results/eval_records.json）→ 看板评测矩阵直接消费；
  5. 会话日志落盘 session_root（可被看板发现、回放、二次分析）。

用法：
    python eval_batch.py gen --domain g66,uu_remote --out tasks/gen   # 先生成任务
    python eval_batch.py run --tasks-dir tasks/gen --permission-mode bypassPermissions \
        --session-root results/batch --limit 5
    python eval_batch.py run --tasks-dir tasks/gen --dry-run           # 只列出将执行的任务
"""

import argparse
import json
import sys
import time
from pathlib import Path

from eval_records import EvalRecords, judge_eval


# ---------- 任务加载 ----------

def load_tasks_from_dir(tasks_dir: str | Path, domains: list = None, levels: list = None,
                        limit: int = 0) -> list:
    """加载目录下任务 JSON；可过滤 domain（skill_expected）/ level；limit=0 不限。"""
    base = Path(tasks_dir)
    if not base.is_dir():
        return []
    tasks = []
    for tf in sorted(base.glob("*.json")):
        try:
            with open(tf, "r", encoding="utf-8") as f:
                t = json.load(f)
        except Exception:
            continue
        t["_file"] = str(tf)
        if domains and t.get("skill_expected") not in domains:
            continue
        if levels and t.get("level") not in levels:
            continue
        tasks.append(t)
        if limit and len(tasks) >= limit:
            break
    return tasks


# ---------- 成本折算（与 eval_server._enrich_cost 同口径） ----------

def _enrich_cost(metrics: dict) -> dict:
    if not isinstance(metrics, dict):
        return metrics
    try:
        from cost import estimate_cost_usd
        est = estimate_cost_usd(metrics.get("model") or "", metrics)
        if est is not None:
            metrics["cost_usd_est"] = round(est, 6)
        import json as _json
        try:
            with open(Path(__file__).parent / "conf.json", "r", encoding="utf-8") as f:
                rate = _json.load(f).get("pricing", {}).get("cny_per_usd", 7.2)
        except Exception:
            rate = 7.2
        if metrics.get("cost_usd") is not None:
            metrics["cost_cny"] = round(metrics["cost_usd"] * rate, 4)
        if metrics.get("cost_usd_est") is not None:
            metrics["cost_est_cny"] = round(metrics["cost_usd_est"] * rate, 4)
    except Exception:
        pass
    return metrics


# ---------- 单任务执行 ----------

def run_one_task(task: dict, backend: str = "claude", timeout_s: int = 300,
                 permission_mode: str = None, model: str = None,
                 session_root: str = None, cwd: str = None, cancel=None,
                 provider: str = None) -> dict:
    """执行单个任务，返回 result（含 metrics/assistant_text/...）。"""
    query = task.get("query", "")
    session_id = f"eval-{task.get('task_id', 'task')}-{int(time.time() * 1000)}"
    if backend == "claude":
        from claude_backend import ClaudeEvalBackend
        with ClaudeEvalBackend(session_root=session_root, cwd=cwd,
                               permission_mode=permission_mode,
                               model=model, provider=provider) as b:
            return b.run_task(task, session_id=session_id, timeout_s=timeout_s,
                              cancel_event=cancel)
    if backend == "dsh":
        from dsh_backend import DshEvalBackend
        with DshEvalBackend(model=model or "deepseek-v4-flash", cwd=cwd,
                            session_root=session_root) as b:
            return b.run_task(task, session_id=session_id, timeout_s=timeout_s,
                              cancel_event=cancel)
    raise ValueError(f"未知 backend: {backend}（支持 claude/dsh）")


# ---------- 批量执行 ----------

def run_batch(tasks: list, backend: str = "claude", timeout_s: int = 300,
              permission_mode: str = None, model: str = None,
              session_root: str = None, cwd: str = None, dry_run: bool = False,
              on_task=None, provider: str = None) -> dict:
    """批量执行 + 判级 + 记录。on_task(task, result, verdict) 回调（进度展示）。"""
    records = EvalRecords()
    results = []
    started_wall = time.time()
    for task in tasks:
        if dry_run:
            print(f"[dry-run] {task.get('task_id')} L{task.get('level')} "
                  f"{task.get('skill_expected')}: {task.get('query')}")
            continue
        print(f"\n=== {task.get('task_id')} L{task.get('level')} "
              f"[{task.get('skill_expected')}] ===")
        print(f"query: {task.get('query')}")
        try:
            result = run_one_task(task, backend=backend, timeout_s=timeout_s,
                                  permission_mode=permission_mode, model=model,
                                  session_root=session_root, cwd=cwd, provider=provider)
        except Exception as exc:
            print(f"执行失败: {type(exc).__name__}: {exc}")
            results.append({"task": task, "result": None, "verdict": None, "error": str(exc)})
            # 执行异常也留痕：记一条失败记录（成功判定 false，level 按任务级别）
            sid = f"eval-{task.get('task_id', 'task')}-{int(time.time() * 1000)}"
            records.add({
                "session_id": sid,
                "agent": backend,
                "level": task.get("level", "L?"),
                "level_source": "task",
                "level_reason": f"执行失败: {type(exc).__name__}: {str(exc)[:120]}",
                "success": False,
                "success_by": "exec_error",
                "tool_calls_total": None,
                "input_tokens": None,
                "cost_cny": None,
                "human_interventions": None,
                "turn_end_reason": None,
                "query": (task.get("query") or "")[:200],
            })
            if on_task:
                on_task(task, None, None)
            continue
        metrics = _enrich_cost(result.get("metrics") or {})
        query = result.get("query") or task.get("query", "")
        sid = result.get("session_id", "")
        verdict = judge_eval(query, sid, metrics, result.get("assistant_text", ""),
                             tasks=[task])
        # 写入评测记录（与 eval_server._record_eval 同构）
        records.add({
            "session_id": sid,
            "agent": backend,
            "level": verdict["level"],
            "level_source": verdict["level_source"],
            "level_reason": verdict["level_reason"],
            "success": verdict["success"],
            "success_by": verdict["success_by"],
            "tool_calls_total": metrics.get("tool_calls_total"),
            "input_tokens": metrics.get("input_tokens"),
            "cost_cny": metrics.get("cost_cny") or metrics.get("cost_est_cny"),
            "human_interventions": metrics.get("human_interventions"),
            "turn_end_reason": metrics.get("turn_end_reason"),
            "query": (query or "")[:200],
        })
        results.append({"task": task, "result": result, "verdict": verdict})
        mark = "PASS" if verdict["success"] else "FAIL"
        print(f"  -> {mark} L{verdict['level']} ({verdict['level_source']}) "
              f"tools={metrics.get('tool_calls_total')} "
              f"end={metrics.get('turn_end_reason')} "
              f"cost_cny={metrics.get('cost_cny') or metrics.get('cost_est_cny')}")
        if on_task:
            on_task(task, result, verdict)
    elapsed = round(time.time() - started_wall, 1)
    return {"results": results, "elapsed_s": elapsed, "records": records}


# ---------- 矩阵输出 ----------

def print_matrix(records: EvalRecords) -> None:
    m = records.matrix()
    portrait = m.get("portrait") or []
    print("\n==================== 评测矩阵（能力画像） ====================")
    if not portrait:
        print("暂无评测记录")
        return
    agents = sorted({p["agent"] for p in portrait})
    levels = ["L1", "L2", "L3", "L4"]
    header = "agent".ljust(12) + "".join(l.ljust(14) for l in levels) + "总计".ljust(10)
    print(header)
    print("-" * len(header))
    for ag in agents:
        row = {p["level"]: p for p in portrait if p["agent"] == ag}
        cells = []
        for lv in levels:
            p = row.get(lv)
            cells.append(f"{p['success']}/{p['count']}" if p and p["count"] else "-".ljust(14))
        total = sum(p["count"] for p in row.values())
        ok = sum(p["success"] for p in row.values())
        print(ag.ljust(12) + "".join(c.ljust(14) for c in cells) + f"{ok}/{total}".ljust(10))
    print("-" * len(header))
    print("\n明细（最近 15 条）：")
    for r in (m.get("records") or [])[-15:]:
        mark = "PASS" if r.get("success") else "FAIL"
        print(f"  [{mark}] {r.get('agent')} L{r.get('level')} "
              f"{r.get('session_id', '')[:24]} {(r.get('query') or '')[:40]}")


# ---------- CLI ----------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="批量评测：生成→执行→判级→矩阵闭环")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # gen：调用 task_gen 生成任务
    p_gen = sub.add_parser("gen", help="用 task_gen 模板生成任务")
    p_gen.add_argument("--domain", default="g66,uu_remote,airgattai,generic")
    p_gen.add_argument("--out", default="tasks/gen")
    p_gen.add_argument("--count", type=int, default=1)
    p_gen.add_argument("--params", default=None, help='参数 JSON 串，如 \'{"device":"SN1"}\'')

    # run：执行批量任务
    p_run = sub.add_parser("run", help="批量执行+判级+记录+矩阵")
    p_run.add_argument("--tasks-dir", default="tasks/gen", help="任务目录")
    p_run.add_argument("--domain", default=None, help="过滤域（逗号分隔）")
    p_run.add_argument("--level", default=None, help="过滤级别（逗号分隔）")
    p_run.add_argument("--limit", type=int, default=0, help="任务数上限（0=全部）")
    p_run.add_argument("--backend", default="claude", choices=["claude", "dsh"])
    p_run.add_argument("--timeout", type=int, default=300, help="每任务超时秒数")
    p_run.add_argument("--permission-mode", default=None,
                       help="claude 权限模式（如 bypassPermissions/acceptEdits）")
    p_run.add_argument("--model", default=None, help="覆盖模型")
    p_run.add_argument("--provider", default=None,
                       help="模型提供商名（conf.json provider 段，如 codemaker_deepseek）")
    p_run.add_argument("--cwd", default=None,
                       help="agent 工作目录（claude 的项目 cwd；如 D:/wy_projects/work_4_log "
                            "以加载该项目的 .mcp.json airgattai 通道）")
    p_run.add_argument("--session-root", default="results/batch",
                       help="会话日志落盘根目录（看板「批量评测」tab 独立展示）")
    p_run.add_argument("--dry-run", action="store_true", help="只列出任务不执行")

    args = parser.parse_args(argv)

    if args.cmd == "gen":
        from task_gen import generate_tasks
        params = {}
        if args.params:
            try:
                params = json.loads(args.params)
            except Exception as e:
                print(f"params JSON 解析失败: {e}", file=sys.stderr)
                return 1
        domains = [d.strip() for d in args.domain.split(",") if d.strip()]
        written = generate_tasks(domains, params, args.out, args.count)
        print(f"生成 {len(written)} 个任务到 {args.out}")
        return 0

    if args.cmd == "run":
        domains = [d.strip() for d in args.domain.split(",") if d.strip()] if args.domain else None
        levels = [l.strip() for l in args.level.split(",") if l.strip()] if args.level else None
        tasks = load_tasks_from_dir(args.tasks_dir, domains, levels, args.limit)
        if not tasks:
            print(f"未找到任务（{args.tasks_dir}），先运行: python eval_batch.py gen", file=sys.stderr)
            return 1
        print(f"加载 {len(tasks)} 个任务，backend={args.backend}，"
              f"permission={args.permission_mode or 'default'}")
        result = run_batch(tasks, backend=args.backend, timeout_s=args.timeout,
                           permission_mode=args.permission_mode, model=args.model,
                           session_root=args.session_root, cwd=args.cwd,
                           dry_run=args.dry_run, provider=args.provider)
        if not args.dry_run:
            print(f"\n批量完成：{len(result['results'])} 任务，耗时 {result['elapsed_s']}s")
            print_matrix(result["records"])
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
