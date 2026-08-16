#!/usr/bin/env python3
"""
agent_status.py — 各 agent 的连接状态探测（claude / codemaker / dsh）。

连接状态 = 「评测通道可用性」，三类信号：
  - CLI 可用性：claude / codemaker 可执行（--version）；dsh 的 deepseek_harness SDK 可 import；
  - 活跃度：进程在线（claude / codemaker-hub-gui）+ 数据源新鲜度（最新日志距今 / opencode.db 距今）；
  - headless 通道：claude `--print --output-format stream-json`、codemaker `run --format json`
    是否具备（CLI 存在即视为就绪，避免每次探测拉起进程）。

状态机（三档）：
  - "online"（绿）：CLI 可用 且（进程在线 或 数据源新鲜）
  - "idle"（黄）：CLI 可用，但无活跃进程且数据源偏旧
  - "offline"（灰/红）：CLI 缺失 / SDK 未装（dsh 特例：提示安装后可启用实时通道）

探测结果带缓存（默认 15s），避免高频轮询反复拉起子进程。
"""

import os
import subprocess
import threading
import time
from pathlib import Path


# ---------- 常量 ----------

_DEFAULT_TTL = 15.0          # 探测结果缓存秒数
_FRESH_MIN = 5               # 数据源"新鲜"阈值（分钟）
_CODEMAKER_BIN = Path.home() / ".codemaker" / "bin" / "codemaker.exe"
_CODEMAKER_DB = Path.home() / ".local" / "share" / "codemaker" / "opencode.db"
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

# Windows 进程名（tasklist 映像名，不含 .exe 后缀匹配）
_PROCESS_NAMES = {"claude": "claude", "codemaker": "codemaker-hub-gui"}


# ---------- 探测原语 ----------

def _cli_version(cmd: list, timeout: int = 15) -> str | None:
    """执行 CLI --version，返回首行版本串；失败返回 None（CLI 不可用）。"""
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        out = r.stdout.decode("utf-8", errors="replace")
        if not out.strip():
            out = r.stderr.decode("utf-8", errors="replace")
        first = out.strip().splitlines()[0].strip() if out.strip().splitlines() else ""
        return first or None
    except Exception:
        return None


def _sdk_importable(module: str) -> bool:
    """SDK 是否可 import（dsh 通道依赖 deepseek_harness）。"""
    try:
        __import__(module)
        return True
    except Exception:
        return False


def _running_processes(name: str) -> int:
    """Windows tasklist 统计匹配进程数；跨平台兜底返回 0。"""
    if os.name != "nt":
        return 0
    try:
        # tasklist 输出使用本地代码页（GBK），按字节读 + errors=replace 防解码炸
        r = subprocess.run(["tasklist"], capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        text = r.stdout.decode("utf-8", errors="replace")
        return sum(1 for line in text.splitlines() if name.lower() in line.lower())
    except Exception:
        return 0


def _newest_file_age_min(base: Path, glob: str = "*.jsonl") -> int | None:
    """目录下最新匹配文件的距今分钟数；无文件返回 None。"""
    if not base.is_dir():
        return None
    newest = None
    try:
        for p in base.rglob(glob):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    except Exception:
        return None
    if newest is None:
        return None
    return int((time.time() - newest) / 60)


def _file_age_min(path: Path) -> int | None:
    try:
        return int((time.time() - path.stat().st_mtime) / 60)
    except Exception:
        return None


def _status(available: bool, active: bool, fresh: bool, special_offline_reason: str | None = None) -> dict:
    """按三档状态机产出状态 dict。"""
    if not available:
        return {"state": "offline", "reason": special_offline_reason or "通道不可用（CLI/SDK 缺失）"}
    if active or fresh:
        return {"state": "online", "reason": "可用" + (" · 进程在线" if active else "")
                + (" · 数据源新鲜" if fresh else "")}
    return {"state": "idle", "reason": "可用但空闲（无活跃进程/数据源偏旧）"}


# ---------- 各 agent 探测 ----------

def _probe_claude() -> dict:
    ver = _cli_version(["claude", "--version"])
    available = ver is not None
    procs = _running_processes(_PROCESS_NAMES["claude"]) if available else 0
    age = _newest_file_age_min(_CLAUDE_PROJECTS)
    active = procs > 0
    fresh = age is not None and age <= _FRESH_MIN
    st = _status(available, active, fresh,
                 "claude CLI 不可用（需安装 Claude Code）")
    return {
        "agent": "claude",
        "cli_ok": available,
        "version": ver,
        "processes": procs,
        "data_fresh_min": age,
        "headless_channel": "claude --print --output-format stream-json",
        **st,
    }


def _probe_codemaker() -> dict:
    ver = _cli_version([str(_CODEMAKER_BIN), "--version"])
    available = ver is not None
    procs = _running_processes(_PROCESS_NAMES["codemaker"]) if available else 0
    age = _file_age_min(_CODEMAKER_DB)
    active = procs > 0
    fresh = age is not None and age <= _FRESH_MIN
    st = _status(available, active, fresh,
                 "codemaker CLI 不可用（~/.codemaker/bin/codemaker.exe 缺失）")
    return {
        "agent": "codemaker",
        "cli_ok": available,
        "version": ver,
        "processes": procs,
        "data_fresh_min": age,
        "headless_channel": "codemaker run --format json <msg>（只读任务+沙箱目录）",
        **st,
    }


def _probe_dsh() -> dict:
    sdk_ok = _sdk_importable("deepseek_harness")
    # dsh 的历史会话落盘（results/board）存在与否只是参考，不算在线
    root = Path(__file__).parent / "results" / "board"
    has_history = root.is_dir() and any(root.glob("*/session.jsonl"))
    st = _status(sdk_ok, False, False,
                 "deepseek_harness SDK 未安装（pip install deepseek-harness 后可启用实时评测）")
    return {
        "agent": "dsh",
        "sdk_ok": sdk_ok,
        "has_history_logs": has_history,
        "headless_channel": "deepseek_harness SDK（subscribe_session_notifications）",
        **st,
    }


# ---------- 聚合 + 缓存 ----------

class AgentStatus:
    """三 agent 连接状态聚合器（带 TTL 缓存，线程安全）。"""

    def __init__(self, ttl: float = _DEFAULT_TTL):
        self._ttl = ttl
        self._cache: dict | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def probe(self, force: bool = False) -> dict:
        now = time.time()
        with self._lock:
            if not force and self._cache is not None and now - self._at < self._ttl:
                return self._cache
        agents = {
            "claude": _probe_claude(),
            "codemaker": _probe_codemaker(),
            "dsh": _probe_dsh(),
        }
        result = {
            "agents": agents,
            "updated_at": int(now * 1000),
            "ttl_s": self._ttl,
            "fresh_min_threshold": _FRESH_MIN,
            "summary": {a: d["state"] for a, d in agents.items()},
        }
        with self._lock:
            self._cache = result
            self._at = now
        return result


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(AgentStatus().probe(force=True), ensure_ascii=False, indent=1))
