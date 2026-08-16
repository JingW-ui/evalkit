# -*- coding: utf-8 -*-
"""单元测试：session_discovery.py（Claude projects / session_root / eval_runs 发现与合并）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import session_discovery as sd

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


def write_claude_jsonl(path: Path, lines: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")


CLAUDE_LINES = [
    {"type": "user", "timestamp": "2026-08-15T10:00:00Z",
     "message": {"role": "user", "content": "帮我部署g66"}},
    {"type": "assistant", "timestamp": "2026-08-15T10:00:05Z",
     "message": {"role": "assistant", "content": [{"type": "text", "text": "开始"}],
                 "usage": {"input_tokens": 100, "output_tokens": 5}}},
]

with tempfile.TemporaryDirectory() as td:
    root = Path(td)

    # Claude projects：两个会话
    p1 = root / "projects" / "D--wy-projects" / "c986f40f-40a4-45a5-98f7-0a8ab7467ede.jsonl"
    write_claude_jsonl(p1, CLAUDE_LINES)
    p2 = root / "projects" / "D--wy-projects" / "subagents" / "child.jsonl"  # 子目录应排除
    write_claude_jsonl(p2, [{"type": "user", "message": {"role": "user", "content": "child"}}])

    found = sd.discover_claude_projects(root / "projects")
    check("发现顶层会话 1 个（排除 subagents 子目录）", len(found) == 1, f"实际 {len(found)}")
    s1 = found[0]
    check("query 摘要取自首条 user", s1.query == "帮我部署g66", f"实际 {s1.query}")
    check("agent=claude source=projects", s1.agent == "claude" and s1.source == "projects")
    check("state=history", s1.state == "history")

    # session_root：DSH 格式 + claude raw 格式
    sr = root / "board"
    (sr / "eval-dsh-1").mkdir(parents=True)
    with open(sr / "eval-dsh-1" / "session.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session", "version": 0, "id": "eval-dsh-1",
                            "createdAt": 1786800000000}) + "\n")
        f.write(json.dumps({"type": "user/message", "data": {
            "content": [{"type": "text", "text": "部署DSh任务"}]}}) + "\n")
    (sr / "eval-claude-1").mkdir(parents=True)
    with open(sr / "eval-claude-1" / "raw.jsonl", "w", encoding="utf-8") as f:
        f.write('{"type":"assistant"}\n')
    with open(sr / "eval-claude-1" / "session.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session", "version": 0, "id": "eval-claude-1"}) + "\n")

    found = sd.discover_session_root(sr)
    agents = {s.session_id: s.agent for s in found}
    check("session_root 发现 2 个会话", len(found) == 2, f"实际 {len(found)}")
    check("有 raw.jsonl → claude", agents.get("eval-claude-1") == "claude")
    check("仅 session.jsonl → dsh", agents.get("eval-dsh-1") == "dsh")
    dsh = next(s for s in found if s.session_id == "eval-dsh-1")
    check("DSH query 摘要", dsh.query == "部署DSh任务", f"实际 {dsh.query}")

    # discover_all：合并 + eval_runs live 优先 + 去重
    runs = {"eval-run-9": {"agent": "claude", "task_id": "t9", "query": "运行中任务",
                           "started_at": 1786800000000}}
    all_s = sd.discover_all(projects_dir=root / "projects", session_root=sr, eval_runs=runs,
                            codemaker_db=False)
    check("合并后 4 个会话（1 projects + 2 root + 1 eval_runs）", len(all_s) == 4,
          f"实际 {len(all_s)}")
    run_s = next(s for s in all_s if s.session_id == "eval-run-9")
    check("eval_runs 会话 live 优先", run_s.state == "live" and all_s[0].session_id == "eval-run-9")
    check("eval_runs agent/query 透传", run_s.agent == "claude" and run_s.query == "运行中任务")

    # 去重：同 session_id + agent 只保留一个
    dup = [s for s in all_s if s.session_id == "eval-dsh-1"]
    check("无重复会话", len(dup) == 1)

# ---- discover_samples_dir：受限样本目录（claude/dsh/airlab 混合） ----
with tempfile.TemporaryDirectory() as td2:
    sdir = Path(td2)
    # claude：Claude Code JSONL
    (sdir / "c1.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "部署g66"}}) + "\n",
        encoding="utf-8")
    # dsh：DSH 格式（首行 type=session）
    (sdir / "d1.jsonl").write_text(
        json.dumps({"type": "session", "version": 0, "id": "d1", "createdAt": 1}) + "\n" +
        json.dumps({"type": "user/message", "data": {"content": [{"type": "text", "text": "DSH任务"}]}}) + "\n",
        encoding="utf-8")
    # airlab：文本日志
    (sdir / "a1.log").write_text(
        "[10:00:01] CCAgent.run skills=['uu-remote-auto'] model=m prompt='取号'\n"
        "[10:00:02] [CC 🔧 shell] {'cmd': 'x'}\n", encoding="utf-8")
    # 非会话文件应忽略（.md 不在支持列表）
    (sdir / "readme.md").write_text("not a session", encoding="utf-8")

    found = sd.discover_samples_dir(sdir)
    agents = {s.session_id: s.agent for s in found}
    check("samples 发现 3 个（忽略 readme.txt）", len(found) == 3, f"实际 {len(found)}")
    check("samples 类型识别 claude/dsh/airlab",
          agents == {"c1": "claude", "d1": "dsh", "a1": "airlab"}, f"实际 {agents}")
    a1 = next(s for s in found if s.session_id == "a1")
    check("samples airlab query 取文本行", "CCAgent.run" in (a1.query or ""), f"实际 {a1.query}")

    # discover_all 配 samples_dir 时只用 samples（不扫 projects/root）
    all_s2 = sd.discover_all(projects_dir=root / "projects", session_root=sr,
                             samples_dir=sdir, codemaker_db=False)
    check("samples_dir 模式只返回 samples 会话", len(all_s2) == 3, f"实际 {len(all_s2)}")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")
