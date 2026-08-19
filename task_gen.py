#!/usr/bin/env python3
"""
task_gen.py — 题库装载器（papers/*.yaml 为权威源）。

历史：早期按 skill 模板生成 L1-L4 任务（build_tasks / generate_tasks），已废弃——
题库统一由 papers/*.yaml 维护（git 版本管理），本模块只负责装载 + 导入 SQLite。

用法：
    python task_gen.py import    # 装载 papers/*.yaml 导入 SQLite tasks 表
"""

import argparse
import sys
from pathlib import Path

import yaml


def load_papers(papers_dir=None) -> list:
    """装载 papers/*.yaml（题库权威源），返回题目 dict 列表（跳过缺 task_id 的坏文件）。"""
    papers_dir = Path(papers_dir) if papers_dir else Path(__file__).parent / "papers"
    if not papers_dir.is_dir():
        return []
    tasks: list = []
    for p in sorted(papers_dir.rglob("*.yaml")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
        except Exception as exc:
            print(f"task_gen: 跳过 {p}（解析失败: {exc}）", file=sys.stderr)
            continue
        if not isinstance(d, dict) or not d.get("task_id"):
            print(f"task_gen: 跳过 {p}（缺 task_id）", file=sys.stderr)
            continue
        tasks.append(d)
    return tasks


def import_papers(papers_dir=None, db_path=None) -> int:
    """装载 papers/*.yaml 并导入 SQLite tasks 表，返回导入条数（幂等 upsert）。"""
    from eval_store import EvalStore
    store = EvalStore(db_path)
    try:
        n = 0
        for t in load_papers(papers_dir):
            store.upsert_task(t)
            n += 1
        return n
    finally:
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="题库装载：papers/*.yaml → SQLite tasks")
    parser.add_argument("cmd", nargs="?", default="import", choices=["import"],
                        help="import（装载 papers/*.yaml 导入 SQLite tasks）")
    args = parser.parse_args(argv)
    if args.cmd == "import":
        n = import_papers()
        print(f"已导入 {n} 个题目（papers/*.yaml → SQLite tasks）")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
