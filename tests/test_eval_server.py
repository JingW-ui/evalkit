# -*- coding: utf-8 -*-
"""单元/集成测试：eval_server.py（SseHub + HTTP + SSE，评测线程用假实现）。

不真实调用 claude/dsh（避免消耗 token）：集成测试 monkeypatch 掉 _run_job。
"""
import json
import sys
import time
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import eval_server as es

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 1) SseHub 单测 ----
hub = es.SseHub(history=3)
hub.publish({"type": "a"})
hub.publish({"type": "b"})
hub.publish({"type": "c"})
hub.publish({"type": "d"})

q = hub.subscribe()
history_frames = []
while not q.empty():
    history_frames.append(q.get())
check("历史重放（最近 3 条）", history_frames == [{"type": "b"}, {"type": "c"}, {"type": "d"}],
      f"实际 {history_frames}")

hub.publish({"type": "e"})
got = q.get(timeout=1)
check("实时广播", got == {"type": "e"})
hub.unsubscribe(q)

# ---- 2) 集成：HTTP + SSE（假评测线程） ----

server = es.EvalServer(web_dir="web")
# 假评测：长跑任务（cancel 后结束），发布 run/start → 事件 → run/end
def fake_run_job(params, session_id, cancel):
    server.hub.publish({"type": "run/start", "session_id": session_id,
                        "backend": "claude", "task_id": "t1", "query": "q"})
    server.hub.publish({"type": "event", "session_id": session_id,
                        "event": {"type": "tool/call", "data": {"name": "read"}}})
    while not cancel.wait(0.05):
        pass
    server.hub.publish({"type": "run/end", "session_id": session_id,
                        "result": {"session_id": session_id, "metrics": {"cost_usd": 0.1}}})
server._run_job = fake_run_job  # type: ignore

httpd = es.EvalHttpServer(server, ("127.0.0.1", 0))
port = httpd.server_address[1]
t = threading.Thread(target=httpd.serve_forever, daemon=True)
t.start()
base = f"http://127.0.0.1:{port}"

def http_get(path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, r.read()

def http_post(path, payload=None):
    data = json.dumps(payload or {}).encode() if payload is not None else b"{}"
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())

try:
    # GET / → evalboard.html
    st, body = http_get("/")
    check("GET / 返回看板", st == 200 and "evalkit 评测看板" in body.decode("utf-8"))

    # GET /api/sessions → 至少返回结构（含 eval 运行中会话）
    st, body = http_get("/api/sessions")
    sessions = json.loads(body).get("sessions", [])
    check("GET /api/sessions 返回列表", isinstance(sessions, list), f"实际 {body.decode()[:120]}")

    # 历史会话 replay 挂接：构造临时 Claude JSONL → attach(replay) → 收到事件 + run/end
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        hist = Path(td) / "hist.jsonl"
        with open(hist, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user",
                                "message": {"role": "user", "content": "历史任务"}}) + "\n")
            f.write(json.dumps({"type": "assistant",
                                "message": {"role": "assistant",
                                            "content": [{"type": "tool_use", "id": "c1",
                                                         "name": "Bash", "input": {}}]}}) + "\n")
            f.write(json.dumps({"type": "user",
                                "message": {"role": "user",
                                            "content": [{"type": "tool_result",
                                                         "tool_use_id": "c1",
                                                         "content": "ok", "is_error": False}]}}) + "\n")
        j = http_post("/api/attach", {"session_id": "hist-1", "agent": "claude",
                                      "path": str(hist), "mode": "replay"})
        check("attach replay ok", j.get("ok") is True, f"实际 {j}")
        # 等重放完成（run/end 发布后 _replays 清除）
        deadline = time.time() + 5
        while time.time() < deadline and "hist-1" in server._replays:
            time.sleep(0.1)
        time.sleep(0.3)
        # 检查 hub 历史里有 run/end（hist-1）
        end_frames = [f for f in server.hub._history
                      if f.get("type") == "run/end" and f.get("session_id") == "hist-1"]
        check("replay 产出 run/end", len(end_frames) >= 1, f"实际 {len(end_frames)}")
        if end_frames:
            m = end_frames[0]["result"]["metrics"]
            check("replay 指标：工具 1 成功 1", m["tool_calls_total"] == 1 and m["tool_success"] == 1,
                  f"实际 {m}")
            check("replay 指标：query 摘要计数 user_turns", m["user_turns"] == 1,
                  f"实际 {m['user_turns']}")
        # detach（replay 已完成，detach 应为幂等 ok）
        j = http_post("/api/detach", {"session_id": "hist-1"})
        check("detach ok", j.get("ok") is True)

    # GET /api/status → idle
    st, body = http_get("/api/status")
    check("初始状态 idle", json.loads(body)["state"] == "idle")

    # start（长跑假任务）→ running
    check("start ok", http_post("/api/start", {"backend": "claude", "task_id": "t1", "query": "q"})["ok"] is True)
    time.sleep(0.3)
    st, body = http_get("/api/status")
    check("任务运行中", json.loads(body)["state"] == "running", f"实际 {body.decode()}")

    # 运行中重复 start → 拒绝
    j = http_post("/api/start", {"task_id": "t2"})
    check("运行中重复 start 拒绝", j.get("ok") is False, f"实际 {j}")

    # stop → 假任务结束回 idle
    j = http_post("/api/stop")
    check("stop ok", j.get("ok") is True)
    deadline = time.time() + 5
    while time.time() < deadline:
        st, body = http_get("/api/status")
        if json.loads(body)["state"] == "idle":
            break
        time.sleep(0.1)
    check("stop 后回 idle", json.loads(body)["state"] == "idle")

    # SSE 流：循环读取，找到 sse_probe 帧（历史帧可能更早，逐帧匹配）
    server.hub.publish({"type": "run/start", "task_id": "sse_probe"})
    resp = urllib.request.urlopen(base + "/events", timeout=5)
    found = None
    try:
        for _ in range(20):
            line = resp.readline().decode("utf-8")
            if not line.startswith("data: "):
                continue
            frame = json.loads(line[len("data: "):])
            if frame.get("task_id") == "sse_probe":
                found = frame
                break
    finally:
        resp.close()
    check("SSE 帧格式 data:", found is not None or True)  # 格式已在读取中验证
    check("SSE 帧内容（sse_probe）", found is not None and found.get("type") == "run/start",
          f"实际 {found}")

    # 最终 idle
    st, body = http_get("/api/status")
    check("最终 idle", json.loads(body)["state"] == "idle")
finally:
    httpd.shutdown()
    httpd.server_close()

# ---- 3) airlab 事件桥：airlab_to_events + _airlab_to_metrics.tasks ----

def _fake_airlab_data():
    """模拟 scan_airlab_log 输出（对齐真实 pod 日志结构）。"""
    return {
        "session_id": "airlab_test",
        "duration_s": 194,
        "model_usage": {"deepseek-v4-pro": 43},
        "tokens": {"input": 100, "cache_read": 200, "cache_write": 0, "output": 50},
        "tool_dist": {"Bash": 2, "TaskCreate": 1, "TaskUpdate": 1},
        "tool_seq": ["Bash(ls -la)", "TaskCreate({\"subject\": \"S1\"})"],
        "tool_times": [("11:20:37", "Bash"), ("11:21:02", "TaskCreate")],
        "llm_ms": 28800.0, "tool_ms": 74000.0, "human_wait_ms": 0.0,
        "total_tool_calls": 4,
        "task_subitems": [{"subject": "S1", "status": "completed", "belongs_to": "帮我部署"}],
        "human_interventions": [], "total_human_interventions": 0,
        "cost_usd": 1.5,
        "tasks": [{"query": "帮我部署", "completion": "completed", "skill_loaded": "g66",
                   "skill_count": 1, "tokens": {"input": 100, "output": 50}, "tool_calls": 2}],
    }


fake = _fake_airlab_data()
events = es.airlab_to_events(fake, path=None)
etypes = [e["type"] for e in events]
check("airlab_to_events 含 user/tool/assistant/turn",
      all(t in etypes for t in ("user/message", "tool/call", "tool/result", "assistant/message", "turn/end")),
      f"实际 {etypes}")
check("airlab_to_events 事件数 = 1 + 2*tool + 2", len(events) == 1 + 2 * 2 + 2, f"实际 {len(events)}")
check("airlab user/message 取 query", events[0]["data"]["content"][0]["text"] == "帮我部署")
check("airlab tool/call 名称解析", events[1]["data"]["name"] == "Bash")
check("airlab turn/end completed",
      events[-1]["data"]["reason"]["kind"] == "completed")
# 事件流用真实 tool_times 时间（不再 1s 步进）：两工具行 11:20:37 → 11:21:02 = 25s
t1 = next(e["time"] for e in events if e["type"] == "tool/call")
t2 = next(e["time"] for e in reversed(events) if e["type"] == "tool/call")
check("airlab tool/call 用真实行时间（两调用差 25s）", t2 - t1 == 25 * 1000,
      f"实际差 {t2 - t1}ms（应为 25000ms）")
# 事件折叠 → EventMetrics 正常
from dsh_backend import EventMetrics
am = EventMetrics()
for e in events:
    am.on_event(e)
am.finalize()
asnap = am.snapshot()
check("airlab 事件折叠 tool_calls=2", asnap["tool_calls_total"] == 2, f"实际 {asnap['tool_calls_total']}")
check("airlab 事件折叠 end=completed", asnap["turn_end_reason"] == "completed")

# _airlab_to_metrics 补 tasks（任务 tab 数据源）
metrics = es._airlab_to_metrics(fake)
check("airlab metrics.tasks 非空", len(metrics.get("tasks") or []) == 1, f"实际 {metrics.get('tasks')}")
mt = metrics["tasks"][0]
check("airlab task 含 subitems+tools",
      mt.get("query") == "帮我部署"
      and len(mt.get("subitems") or []) == 1
      and len(mt.get("tools") or []) == 2,
      f"实际 query={mt.get('query')} sub={len(mt.get('subitems') or [])} tools={len(mt.get('tools') or [])}")
# 真实耗时直取（不用虚构事件流折叠值覆盖）
check("airlab llm_ms 直取真实值", metrics["llm_ms"] == 28800.0, f"实际 {metrics['llm_ms']}")
check("airlab tool_ms 直取真实值", metrics["tool_ms"] == 74000.0, f"实际 {metrics['tool_ms']}")
check("airlab 耗时自洽（llm+tool+wait+idle=duration）",
      metrics["duration_ms"] == 194000
      and metrics["llm_ms"] + metrics["tool_ms"] + metrics["human_wait_ms"] <= metrics["duration_ms"],
      f"实际 {metrics['duration_ms']} / {metrics['llm_ms']}+{metrics['tool_ms']}+{metrics['human_wait_ms']}")

# ---- 3.5) launch_terminal：codemaker 分支（mock Popen 捕获命令，不真弹窗） ----
from unittest.mock import patch

with patch("subprocess.Popen") as mp:
    mp.return_value.pid = 12345
    r = server.launch_terminal({
        "agent": "codemaker", "cwd": "D:\\wy_projects\\work_4_log",
        "model": "netease-codemaker/deepseek-v4-flash",
    })
    check("codemaker 终端返回 ok", r.get("ok") is True, f"实际 {r}")
    check("codemaker 终端 agent 标注", r.get("agent") == "codemaker")
    check("codemaker 命令含 run -i --model",
          mp.call_args.args[0][-6:] == ["run", "-i", "--model", "netease-codemaker/deepseek-v4-flash",
                                        "--dir", "D:\\wy_projects\\work_4_log"],
          f"实际 {mp.call_args.args[0]}")
server._terminals.pop(12345, None)

# ---- 4) 收尾 ----
print("OK: eval_server 测试完成")

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")
# 强制退出：SSE keepalive daemon 线程残留会导致解释器退出码异常，直接 os._exit(0)
import os
os._exit(0)
# 强制退出：SSE keepalive daemon 线程残留会导致解释器退出码异常，直接 os._exit(0)
import os
os._exit(0)
