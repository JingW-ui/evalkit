#!/usr/bin/env python3
"""
run_eval.py — 顶层评测入口。

串联：加载 task → runner.run_task() → 找 JSONL → parser.parse_session_jsonl()
→ 成功判定 → parser.compute_metrics() → 写出 run JSONL → report.aggregate()
"""

import os
import json
import sys
import argparse
from pathlib import Path
from datetime import datetime

# 把 eval 目录加入 path
sys.path.insert(0, str(Path(__file__).parent))

from runner import run_task, check_success, find_generated_session_jsonl
from parser import parse_session_jsonl, compute_metrics
from report import load_records, aggregate, print_summary, render_report


def main():
    parser = argparse.ArgumentParser(description="Claude Code + Skill 评测")
    parser.add_argument("--tasks-dir", default="tasks", help="测试用例目录")
    parser.add_argument("--sandbox-dir", default="sandbox", help="运行沙盒目录")
    parser.add_argument("--results-dir", default="results", help="结果输出目录")
    parser.add_argument("--n", type=int, default=1, help="每个任务重复次数")
    parser.add_argument("--timeout", type=int, default=300, help="每个任务超时秒数")
    parser.add_argument("--tasks", nargs="*", help="指定任务 ID（不指定则跑全部）")
    args = parser.parse_args()

    base_dir = Path(__file__).parent
    tasks_dir = base_dir / args.tasks_dir
    sandbox_base = base_dir / args.sandbox_dir
    results_dir = base_dir / args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)

    # 收集任务
    task_ids_filter = set(args.tasks) if args.tasks else None
    task_files = sorted(tasks_dir.glob("*.json"))
    tasks = []
    for tf in task_files:
        t = json.loads(tf.read_text(encoding="utf-8"))
        if task_ids_filter and t.get("task_id") not in task_ids_filter:
            continue
        tasks.append(t)

    if not tasks:
        print("没有找到测试任务")
        return

    print(f"找到 {len(tasks)} 个任务, 每个跑 {args.n} 次")

    # 结果输出文件
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_file = results_dir / f"run_{date_str}.jsonl"

    all_records = []

    for task in tasks:
        task_id = task["task_id"]
        sandbox_dir = sandbox_base / task_id

        for run_idx in range(args.n):
            print(f"\n{'='*50}")
            print(f"[{task_id}] 第 {run_idx+1}/{args.n} 轮")
            print(f"{'='*50}")

            # 将产物路径注入 query（sandbox 的绝对路径）
            query_with_path = task["query"].replace("{sandbox}", str(sandbox_dir.resolve()))
            modified_task = {**task, "query": query_with_path}

            # 执行 — cwd 在主项目目录，skill 正常触发
            result, error = run_task(
                modified_task,
                str(sandbox_dir),
                project_dir=str(base_dir.parent.resolve()),
                timeout_s=args.timeout,
            )

            print(f"  耗时: {result['duration_s']}s")
            print(f"  产物文件: {result['sandbox_files']}")
            if error:
                print(f"  错误信息: {error[:300]}")

            # 查找 JSONL
            jsonl_path = find_generated_session_jsonl(str(sandbox_dir))
            if not jsonl_path:
                print("  WARNING: 找不到对应的 JSONL 会话日志，尝试从 projects/ 定位...")
                # 手动搜索 projects/ 下最新文件
                proj_dir = Path(os.environ["USERPROFILE"]) / ".claude" / "projects"
                candidates = sorted(proj_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    jsonl_path = str(candidates[0])
                    print(f"  找到最新 JSONL: {jsonl_path}")
                else:
                    print("  ERROR: 未找到任何 JSONL")
                    continue

            # 解析
            parsed = parse_session_jsonl(jsonl_path)
            print(f"  Skill 加载: {parsed['skill_loaded'] or '无'}")
            print(f"  工具调用数: {len(parsed['tool_sequence'])}")
            print(f"  Tokens (in): {parsed['total_input_tokens']:,}")

            # 成功判定
            success = check_success(task, str(sandbox_dir))
            print(f"  成功判定: {success}")

            # 计算指标
            rec = compute_metrics(parsed, task, success)

            # 附加运行信息
            rec["run_idx"] = run_idx
            rec["duration_s"] = result["duration_s"]
            rec["sandbox_files"] = result["sandbox_files"]
            rec["has_error"] = error is not None and len(error) > 0

            all_records.append(rec)

            # 实时写入
            with open(run_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 聚合 & 报表
    print(f"\n\n{'#'*60}")
    print(f"所有任务完成，开始生成报表...")
    print(f"{'#'*60}")

    records = load_records(str(run_file))
    agg = aggregate(records)
    if "error" in agg:
        print(f"聚合失败: {agg['error']}")
        return

    print_summary(agg)

    report_path = results_dir / f"report_{date_str}.md"
    render_report(agg, str(report_path))
    print(f"\n完整报表: {report_path}")


if __name__ == "__main__":
    main()
