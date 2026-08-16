# -*- coding: utf-8 -*-
"""单元测试：codemaker_backend.py（opencode.db 解析 + 事件适配 + 实时尾随 + 发现集成）。"""
import json
import sqlite3
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import codemaker_backend as cmb
import session_discovery as sd

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- 构造合成 opencode.db（与真实库同构） ----

def build_db(path: Path) -> None:
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE session (
      id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
      slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT, share_url TEXT,
      summary_additions INTEGER, summary_deletions INTEGER, summary_files INTEGER,
      summary_diffs TEXT, metadata TEXT, cost REAL, tokens_input INTEGER,
      tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER,
      tokens_cache_write INTEGER, grep_token_count INTEGER, revert TEXT, permission TEXT,
      agent TEXT, model TEXT, time_created INTEGER, time_updated INTEGER,
      time_compacting INTEGER, time_archived INTEGER);
    CREATE TABLE message (
      id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
      time_updated INTEGER, data TEXT);
    CREATE TABLE part (
      id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
      time_updated INTEGER, data TEXT);
    CREATE TABLE todo (
      session_id TEXT, content TEXT, status TEXT, priority TEXT, position INTEGER,
      time_created INTEGER, time_updated INTEGER);
    CREATE TABLE event (
      id TEXT PRIMARY KEY, aggregate_id TEXT, seq INTEGER, type TEXT, data TEXT);
    CREATE TABLE event_sequence (aggregate_id TEXT, seq INTEGER, owner_id TEXT);
    """)
    sid = "ses_test0000000000000000001"
    cur.execute(
        "INSERT INTO session (id, project_id, slug, directory, path, title, cost, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, "
        "tokens_cache_write, agent, model, time_created, time_updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "global", "test-slug", "D:/proj", "proj", "帮我部署g66资源",
         0.1234, 5000, 800, 200, 30000, 100, "build",
         '{"id":"deepseek-v4-pro","providerID":"netease-codemaker"}',
         1700000000000, 1700000100000))
    # 消息：user(文本 part) → assistant(step-start, reasoning, tool, text, step-finish stop)
    cur.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                ("msg_user1", sid, 1700000000000, 1700000001000,
                 json.dumps({"role": "user", "time": {"created": 1700000000000}})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_user1", "msg_user1", sid, 1700000000005, 1700000000005,
                 json.dumps({"type": "text", "text": "帮我部署g66资源"})))
    cur.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                ("msg_asst1", sid, 1700000005000, 1700000006000,
                 json.dumps({
                     "role": "assistant",
                     "time": {"created": 1700000005000, "completed": 1700000008000},
                     "modelID": "deepseek-v4-pro", "providerID": "netease-codemaker",
                     "finish": "tool-calls",
                     "tokens": {"total": 900, "input": 500, "output": 50,
                                "reasoning": 10, "cache": {"read": 100, "write": 0}},
                     "cost": 0.01})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_ss1", "msg_asst1", sid, 1700000005000, 1700000005000,
                 json.dumps({"type": "step-start"})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_rs1", "msg_asst1", sid, 1700000005100, 1700000005100,
                 json.dumps({"type": "reasoning", "text": "先检查设备"})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_tool1", "msg_asst1", sid, 1700000005200, 1700000005200,
                 json.dumps({"type": "tool", "tool": "bash", "callID": "call_1",
                             "state": {"status": "completed",
                                       "input": {"command": "ls"},
                                       "output": "file1 file2",
                                       "metadata": {"exit": 0}}})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_tx1", "msg_asst1", sid, 1700000006000, 1700000006000,
                 json.dumps({"type": "text", "text": "先执行命令"})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_sf1", "msg_asst1", sid, 1700000008000, 1700000008000,
                 json.dumps({"type": "step-finish", "reason": "tool-calls",
                             "tokens": {"total": 900, "input": 500, "output": 50},
                             "cost": 0.01})))
    # assistant 最终回复（stop）
    cur.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                ("msg_asst2", sid, 1700000010000, 1700000011000,
                 json.dumps({
                     "role": "assistant",
                     "time": {"created": 1700000010000, "completed": 1700000012000},
                     "modelID": "deepseek-v4-pro", "providerID": "netease-codemaker",
                     "finish": "stop",
                     "tokens": {"total": 200, "input": 100, "output": 20,
                                "reasoning": 0, "cache": {"read": 0, "write": 0}},
                     "cost": 0.002})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_ss2", "msg_asst2", sid, 1700000010000, 1700000010000,
                 json.dumps({"type": "step-start"})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_tx2", "msg_asst2", sid, 1700000011000, 1700000011000,
                 json.dumps({"type": "text", "text": "部署完成"})))
    cur.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                ("prt_sf2", "msg_asst2", sid, 1700000012000, 1700000012000,
                 json.dumps({"type": "step-finish", "reason": "stop",
                             "tokens": {"total": 200, "input": 100, "output": 20},
                             "cost": 0.002})))
    # todo 子任务
    cur.execute("INSERT INTO todo VALUES (?,?,?,?,?,?,?)",
                (sid, "安装依赖", "completed", "high", 0, 1700000000000, 1700000009000))
    cur.execute("INSERT INTO todo VALUES (?,?,?,?,?,?,?)",
                (sid, "执行部署脚本", "in_progress", "high", 1, 1700000000000, 1700000012000))
    # event 表（实时增量用）
    evs = [
        ("evt_1", sid, 1, "session.created.1", {"sessionID": sid}),
        ("evt_2", sid, 2, "message.updated.1",
         {"sessionID": sid, "info": {"id": "msg_user1", "sessionID": sid, "role": "user",
                                     "time": {"created": 1700000000000}}}),
        ("evt_3", sid, 3, "message.part.updated.1",
         {"sessionID": sid, "part": {"id": "prt_user1", "messageID": "msg_user1",
                                     "sessionID": sid, "type": "text",
                                     "text": "帮我部署g66资源"}}),
        ("evt_4", sid, 4, "message.updated.1",
         {"sessionID": sid, "info": {"id": "msg_asst1", "sessionID": sid, "role": "assistant",
                                     "modelID": "deepseek-v4-pro",
                                     "time": {"created": 1700000005000, "completed": 1700000008000},
                                     "finish": "tool-calls",
                                     "tokens": {"input": 500, "output": 50,
                                                "cache": {"read": 100, "write": 0}}}}),
        ("evt_5", sid, 5, "message.part.updated.1",
         {"sessionID": sid, "part": {"id": "prt_tool1", "messageID": "msg_asst1",
                                     "sessionID": sid, "type": "tool", "tool": "bash",
                                     "callID": "call_1", "time": 1700000005200,
                                     "state": {"status": "completed",
                                               "input": {"command": "ls"},
                                               "output": "file1"}}}),
        ("evt_6", sid, 6, "message.part.updated.1",
         {"sessionID": sid, "part": {"id": "prt_tx1", "messageID": "msg_asst1",
                                     "sessionID": sid, "type": "text",
                                     "text": "先执行命令"}}),
        ("evt_7", sid, 7, "message.updated.1",
         {"sessionID": sid, "info": {"id": "msg_asst2", "sessionID": sid, "role": "assistant",
                                     "modelID": "deepseek-v4-pro",
                                     "time": {"created": 1700000010000, "completed": 1700000012000},
                                     "finish": "stop",
                                     "tokens": {"input": 100, "output": 20,
                                                "cache": {"read": 0, "write": 0}}}}),
        ("evt_8", sid, 8, "message.part.updated.1",
         {"sessionID": sid, "part": {"id": "prt_tx2", "messageID": "msg_asst2",
                                     "sessionID": sid, "type": "text",
                                     "text": "部署完成"}}),
    ]
    for eid, agg, seq, etype, data in evs:
        cur.execute("INSERT INTO event VALUES (?,?,?,?,?)",
                    (eid, agg, seq, etype, json.dumps(data, ensure_ascii=False)))
        cur.execute("INSERT INTO event_sequence VALUES (?,?,?)", (agg, seq, ""))
    con.commit()
    con.close()


def build_tmp_db() -> Path:
    td = tempfile.mkdtemp(prefix="cm_test_")
    p = Path(td) / "opencode.db"
    build_db(p)
    return p


# ---- 1) is_codemaker_db / _detect_agent ----
dbp = build_tmp_db()
check("is_codemaker_db True", cmb.is_codemaker_db(dbp) is True)
check("is_codemaker_db 非 db False", cmb.is_codemaker_db(Path(__file__)) is False)
check("_detect_agent .db → codemaker", sd._detect_agent(dbp) == "codemaker")
# 空 sqlite 不应误判
td2 = Path(tempfile.mkdtemp()) / "x.db"
sqlite3.connect(td2).close()
check("_detect_agent 空库 None", sd._detect_agent(td2) is None)

# ---- 2) CodemakerDB ----
db = cmb.CodemakerDB(dbp)
sessions = db.list_sessions()
check("list_sessions 1 条", len(sessions) == 1, f"实际 {len(sessions)}")
s0 = sessions[0]
check("session 字段", s0["session_id"].startswith("ses_") and s0["model"] == "deepseek-v4-pro"
      and s0["cost_usd"] == 0.1234 and s0["tokens"]["input"] == 5000)
check("session 时间", s0["started_at"] == 1700000000000 and s0["updated_at"] == 1700000100000)
check("get_session 命中", db.get_session(s0["session_id"]) is not None)
check("messages 3 条", len(db.messages(s0["session_id"])) == 3)
check("parts 9 条", len(db.parts(s0["session_id"])) == 9)
check("todos 2 条", len(db.todos(s0["session_id"])) == 2)
check("max_seq 8", db.max_seq(s0["session_id"]) == 8)
evs_after = db.events_after(s0["session_id"], 5)
check("events_after seq>5", len(evs_after) == 3 and evs_after[0]["seq"] == 6)

# ---- 3) replay：message/part 表 → 统一事件 ----
adapter = cmb.CodemakerEventAdapter()
events = adapter.replay(db, s0["session_id"])
types = [e["type"] for e in events]
check("replay 事件类型", all(t in types for t in
      ("user/message", "assistant/message", "tool/call", "tool/result",
       "step/start", "step/end", "turn/end")), f"实际 {types}")
um = next(e for e in events if e["type"] == "user/message")
check("user/message 文本", um["data"]["content"][0]["text"] == "帮我部署g66资源")
am = [e for e in events if e["type"] == "assistant/message"]
check("assistant/message ×2", len(am) == 2, f"实际 {len(am)}")
am1 = am[0]
check("assistant 内容含 text+reasoning",
      any(b["type"] == "reasoning" and b["text"] == "先检查设备" for b in am1["data"]["message"]["content"])
      and any(b["type"] == "text" and b["text"] == "先执行命令" for b in am1["data"]["message"]["content"]))
check("assistant usage 驼峰", am1["data"]["message"]["usage"] ==
      {"inputTokens": 500, "outputTokens": 50, "cacheReadTokens": 100, "cacheWriteTokens": 0},
      f"实际 {am1['data']['message']['usage']}")
check("assistant model", am1["data"]["message"]["model"] == "deepseek-v4-pro")
tc = next(e for e in events if e["type"] == "tool/call")
tr = next(e for e in events if e["type"] == "tool/result")
check("tool/call 参数", tc["data"]["name"] == "bash" and tc["data"]["callId"] == "call_1"
      and json.loads(tc["data"]["arguments"]) == {"command": "ls"})
check("tool/result ok", tr["data"]["callId"] == "call_1"
      and tr["data"]["message"]["content"][0]["isError"] is False
      and "file1" in tr["data"]["message"]["content"][0]["content"])
te = [e for e in events if e["type"] == "turn/end"]
check("turn/end 仅 stop 一次", len(te) == 1 and te[0]["data"]["reason"]["kind"] == "completed",
      f"实际 {[(e['data']['reason']['kind']) for e in te]}")

# ---- 4) EventMetrics 折叠 ----
from dsh_backend import EventMetrics
metrics = EventMetrics()
for e in events:
    metrics.on_event(e)
metrics.finalize()
snap = metrics.snapshot()
check("折叠 tool 计数", snap["tool_calls_total"] == 1 and snap["tool_success"] == 1
      and snap["tool_fail"] == 0)
check("折叠 token", snap["input_tokens"] == 600 and snap["output_tokens"] == 70
      and snap["cache_read_tokens"] == 100)
check("折叠 user_turns", snap["user_turns"] == 1)
check("折叠 end_reason", snap["turn_end_reason"] == "completed")
check("折叠 model", snap["model"] == "deepseek-v4-pro")
check("skill 工具归一化", "bash" in snap["tool_calls_by_name"])

# ---- 5) 实时：event 表增量 → 统一事件 ----
st = {}
adapter2 = cmb.CodemakerEventAdapter()
live_events = []
for row in db.events_after(s0["session_id"], 0):
    live_events.extend(adapter2.adapt_event(row, st))
lt = [e["type"] for e in live_events]
check("live 事件类型", all(t in lt for t in ("user/message", "assistant/message",
      "tool/call", "tool/result", "turn/end")), f"实际 {lt}")
check("live user/message 文本", next(e for e in live_events if e["type"] == "user/message")
      ["data"]["content"][0]["text"] == "帮我部署g66资源")
check("live turn/end completed", next(e for e in live_events if e["type"] == "turn/end")
      ["data"]["reason"]["kind"] == "completed")
# 重放同一批（模拟重复轮询）不重复
n1 = len(live_events)
for row in db.events_after(s0["session_id"], 0):
    live_events.extend(adapter2.adapt_event(row, st))
check("live 去重（重复轮询不重发）", len(live_events) == n1, f"实际 {n1} → {len(live_events)}")

# ---- 6) CodemakerTails 实时尾随 ----
collected = []
def on_events(sid, evs):
    collected.extend(evs)
tails = cmb.CodemakerTails(on_events=on_events, poll_interval=0.05)
check("start True", tails.start(s0["session_id"], dbp) is True)
deadline = time.time() + 3
while time.time() < deadline and len(collected) == 0:
    time.sleep(0.05)
check("尾随首轮收到重放事件", len(collected) >= 1, f"实际 {len(collected)}")
# 插入新 event → 尾随应增量收到
con = sqlite3.connect(dbp)
con.execute("INSERT INTO event VALUES (?,?,?,?,?)",
            ("evt_9", s0["session_id"], 9, "message.updated.1",
             json.dumps({"sessionID": s0["session_id"],
                         "info": {"id": "msg_asst3", "sessionID": s0["session_id"],
                                  "role": "assistant", "modelID": "deepseek-v4-pro",
                                  "time": {"created": 1700000020000, "completed": 1700000021000},
                                  "finish": "stop",
                                  "tokens": {"input": 10, "output": 5,
                                             "cache": {"read": 0, "write": 0}}}})))
con.execute("INSERT INTO event_sequence VALUES (?,?,?)", (s0["session_id"], 9, ""))
con.execute("INSERT INTO event VALUES (?,?,?,?,?)",
            ("evt_10", s0["session_id"], 10, "message.part.updated.1",
             json.dumps({"sessionID": s0["session_id"],
                         "part": {"id": "prt_tx3", "messageID": "msg_asst3",
                                  "sessionID": s0["session_id"], "type": "text",
                                  "text": "新回复"}})))
con.execute("INSERT INTO event_sequence VALUES (?,?,?)", (s0["session_id"], 10, ""))
con.commit(); con.close()
n0 = len(collected)
deadline = time.time() + 3
while time.time() < deadline and len(collected) == n0:
    time.sleep(0.05)
check("尾随收到新 event 增量", len(collected) > n0, f"实际 {n0} → {len(collected)}")
check("尾随增量含 assistant/message", any(
    e["type"] == "assistant/message"
    and any(b.get("text") == "新回复" for b in e["data"]["message"]["content"])
    for e in collected[n0:]))
tails.stop(s0["session_id"])
time.sleep(0.2)
check("stop 后 running 空", tails.running() == [])
check("stop 后不再增量", len(collected) == (lambda n: n)(len(collected)))

# ---- 7) scan_codemaker_log ----
result = cmb.scan_codemaker_log(dbp)
check("scan 返回 1 会话", len(result["sessions"]) == 1)
m = result["sessions"][0]["metrics"]
check("scan 指标", m["tool_calls_total"] == 1 and m["turn_end_reason"] == "completed")
check("scan 官方 cost", m["cost_usd"] == 0.1234)
check("scan 子任务注入", len((m["tasks"] or [{}])[0].get("subitems") or []) == 2
      and (m["tasks"][0]["subitems"][0]["status"]) == "completed")

# ---- 8) 发现集成 ----
from session_discovery import discover_codemaker_db, discover_single_path, discover_samples_dir
infos = discover_codemaker_db(dbp)
check("discover_codemaker_db 1 条", len(infos) == 1 and infos[0].agent == "codemaker"
      and infos[0].session_id.startswith("ses_") and infos[0].model == "deepseek-v4-pro")
info = discover_single_path(dbp)
check("discover_single_path .db 取最新", info is not None and info.agent == "codemaker")
sdir = Path(tempfile.mkdtemp())
import shutil
shutil.copy(dbp, sdir / "opencode.db")
infos = discover_samples_dir(sdir)
check("discover_samples_dir 含 codemaker", any(i.agent == "codemaker" for i in infos))
all_s = sd.discover_all(samples_dir=str(sdir), codemaker_db=str(dbp))
check("discover_all 合并 codemaker", sum(1 for s in all_s if s.agent == "codemaker") >= 1)

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")
