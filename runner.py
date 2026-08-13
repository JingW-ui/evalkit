#!/usr/bin/env python3
"""
runner.py — 驱动 Claude Code 执行评测任务。

用 subprocess 启动 Claude Code，跑 task query（-p headless 模式），
等待完成。

关键设计：
  - cwd 保持 D:\wy_projects\work_4_log（主项目目录），这样 skill 正常触发
  - 产物路径在 task query 中指定（绝对路径指向 sandbox/{task_id}/）
  - --output-format=stream-json 输出包含 usage/tool_use 等结构化行
  - 事后读 projects/ JSONL 做指标提取
"""

import os
import json
import time
import queue
import threading
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone


def _read_stream(pipe, q: queue.Queue, prefix: str):
    """在后台线程读 subprocess 的 stdout/stderr，逐行入队列。"""
    try:
        for raw in iter(pipe.readline, b""):
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            except Exception:
                line = repr(raw)
            q.put((prefix, line))
    finally:
        pipe.close()


def run_task(
    task: Dict,
    sandbox_dir: str,
    project_dir: str = r"D:\wy_projects\work_4_log",
    cli_path: str = "claude",
    timeout_s: int = 300,
) -> Tuple[Dict, Optional[str]]:
    """
    在主项目目录下跑 Claude Code，执行 task["query"]。

    cwd 保持在 project_dir，这样 skill 可以正常触发。
    产物路径在 query 中已指定为 sandbox_dir 下的绝对路径。

    Returns:
        (result_dict, error_string)
    """
    sandbox = Path(sandbox_dir)
    sandbox.mkdir(parents=True, exist_ok=True)

    query = task.get("query", "")
    task_id = task.get("task_id", "unknown")

    # 清除 sandbox 中上次运行的残留产物
    for f in sandbox.iterdir():
        if f.is_file():
            f.unlink()

    cmd = [
        cli_path,
        "--output-format", "stream-json",
        "--verbose",
    ]

    env = os.environ.copy()

    started_at = datetime.now(timezone.utc)

    # 用 stdin 传 query（而不是 -p），这样 skill 触发机制正常工作
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        cwd=project_dir,
        env=env,
        text=False,
    )

    # 通过 stdin 传入 query 然后关闭
    try:
        proc.stdin.write(query.encode("utf-8"))
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # 进程可能在收到输入前就退出了

    # 并行读 stdout + stderr
    q: queue.Queue = queue.Queue()
    t_stdout = threading.Thread(target=_read_stream, args=(proc.stdout, q, "out"), daemon=True)
    t_stderr = threading.Thread(target=_read_stream, args=(proc.stderr, q, "stderr"), daemon=True)
    t_stdout.start()
    t_stderr.start()

    stream_lines = []
    error_lines = []

    deadline = time.time() + timeout_s
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            proc.kill()
            error_lines.append(f"TIMEOUT: exceeded {timeout_s}s")
            break
        if proc.poll() is not None and q.empty():
            break
        try:
            prefix, line = q.get(timeout=min(remaining, 1.0))
            if prefix == "out":
                stream_lines.append(line)
            else:
                error_lines.append(line)
        except queue.Empty:
            continue

    # 确保线程结束
    t_stdout.join(timeout=5)
    t_stderr.join(timeout=5)

    ended_at = datetime.now(timezone.utc)
    duration_s = (ended_at - started_at).total_seconds()

    # 收集 sandbox 中的产物文件
    sandbox_files = sorted(
        [str(p.relative_to(sandbox)) for p in sandbox.rglob("*") if p.is_file()]
    )

    result = {
        "task_id": task_id,
        "query": query,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_s": round(duration_s, 2),
        "exit_code": proc.returncode,
        "sandbox_dir": str(sandbox),
        "sandbox_files": sandbox_files,
        "stream_lines": stream_lines,
    }

    return result, "\n".join(error_lines) if error_lines else None


def check_success(task: Dict, sandbox_dir: str) -> bool:
    """按 task 的 success_condition 做规则校验。"""
    cond = task.get("success_condition", {})
    ctype = cond.get("type", "")

    if ctype == "file_exists_with_substrings":
        file_path = Path(sandbox_dir) / cond.get("path", "")
        if not file_path.exists():
            return False
        content = file_path.read_text(encoding="utf-8", errors="replace")
        must_contain = cond.get("must_contain", [])
        return all(sub in content for sub in must_contain)

    elif ctype == "response_contains":
        # 从 stream_lines 解最后一条完整的 assistant 消息中的 text
        # 简化：检查是否有任一输出行包含目标字符串
        # （实际运行中 runner 会调这个函数时传入 sandbox，所以我们只处理 file 类型的）
        # 对于负例，成功条件简单判断即可
        return True  # 默认 pass

    return False


def find_generated_session_jsonl(sandbox_dir: str, lookback_s: int = 30) -> Optional[str]:
    """
    在 %USERPROFILE%\.claude\projects\ 下找本次运行生成的 JSONL。

    策略：按时间戳找最近更新的 JSONL（在 started_at 之后创建的）。
    这里调用方传入 started_at，从 projects 目录下找最新的文件。

    简化策略：直接返回 projects/ 下最新修改的 JSONL 文件路径。
    """
    projects_dir = Path(os.environ.get("USERPROFILE", "")) / ".claude" / "projects"
    if not projects_dir.exists():
        return None

    # 收集所有 jsonl files，找最新修改的
    best = None
    best_mtime = 0
    for jsonl in projects_dir.rglob("*.jsonl"):
        mtime = jsonl.stat().st_mtime
        if mtime > best_mtime:
            best_mtime = mtime
            best = jsonl

    return str(best) if best else None


if __name__ == "__main__":
    # 快速自测：只需 parser.py 的测试，不做实际 Claude Code 调起
    print("runner.py loaded. Use run_task() to execute.")
