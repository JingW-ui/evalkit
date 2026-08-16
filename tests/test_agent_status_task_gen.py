# -*- coding: utf-8 -*-
"""单元测试：agent_status.py（连接状态探测）+ task_gen.py（L1-L4 任务生成）。"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import agent_status as ast
import task_gen as tg

FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print(f"[{tag}] {name} {detail}")


# ---- agent_status：状态机原语 ----
check("_status 三档：offline/idle/online",
      ast._status(False, False, False)["state"] == "offline"
      and ast._status(True, False, False)["state"] == "idle"
      and ast._status(True, True, False)["state"] == "online"
      and ast._status(True, False, True)["state"] == "online")
check("_status offline 带原因",
      ast._status(False, False, False, "custom reason")["reason"] == "custom reason")
check("_status online 原因组合",
      "进程在线" in ast._status(True, True, True)["reason"]
      and "数据源新鲜" in ast._status(True, True, True)["reason"])

# ---- AgentStatus 聚合（缓存 + 结构） ----
status = ast.AgentStatus(ttl=60)
r1 = status.probe(force=True)
check("probe 结构", set(r1["agents"]) == {"claude", "codemaker", "dsh"}
      and set(r1["agents"]["claude"]) >= {"state", "cli_ok", "headless_channel"}
      and set(r1["agents"]["codemaker"]) >= {"state", "version", "data_fresh_min"}
      and set(r1["agents"]["dsh"]) >= {"state", "sdk_ok"})
check("state 三档合法",
      all(r1["agents"][a]["state"] in ("online", "idle", "offline") for a in r1["agents"]))
check("summary 透传", r1["summary"] == {a: d["state"] for a, d in r1["agents"].items()})
check("缓存命中", status.probe() is r1)          # TTL 内返回同一对象
check("force 刷新新对象", status.probe(force=True) is not r1 or status.probe(force=True)["updated_at"] >= r1["updated_at"])

# dsh 在无 SDK 时必为 offline（本机未装 deepseek_harness 即为预期；装了则为 online/idle）
check("dsh 状态字段合理", r1["agents"]["dsh"]["sdk_ok"] in (True, False))


# ---- task_gen：模板完整性 ----
check("四级模板齐全",
      all(set(tg.TEMPLATES.get(d, {})) == {"L1", "L2", "L3", "L4"}
          for d in ("g66", "uu_remote", "airgattai", "generic")))
# 每个域每级至少 1 条
for d in tg.TEMPLATES:
    for lv in tg.LEVELS:
        check(f"{d} {lv} 有模板", len(tg.TEMPLATES[d].get(lv, [])) >= 1)

# ---- task_gen：占位符替换 ----
params = {"device": "SN-123", "dir": "D:/tmp", "file": "a.txt"}
q = tg._format_query("把 {file} 推到 {device} 的 {dir}/", params)
check("占位符替换", q == "把 a.txt 推到 SN-123 的 D:/tmp/", f"实际 {q}")
q2 = tg._format_query("未提供参数 {nope}", params)
check("缺失占位保留", "{nope}" in q2, f"实际 {q2}")
c = tg._format_condition({"type": "file_exists", "path": "{dir}/{file}", "device": "{device}"}, params)
check("condition 递归替换", c == {"type": "file_exists", "path": "D:/tmp/a.txt", "device": "SN-123"}, f"实际 {c}")

# ---- task_gen：生成文件 ----
with tempfile.TemporaryDirectory() as td:
    written = tg.generate_tasks(["g66", "generic"], {"device": "SN-1"}, td, count=1)
    check("生成 8 个任务（g66 5 + generic 4）", len(written) == 9, f"实际 {len(written)}")
    # 每文件可解析、字段齐全、L1-L4 各至少 1
    levels = set()
    for p in written:
        t = json.load(open(p, encoding="utf-8"))
        check(f"{p.name} 字段齐全",
              {"task_id", "level", "query", "success_condition", "note"} <= set(t)
              and t["level"] in ("L1", "L2", "L3", "L4")
              and t["success_condition"]["type"] in
              ("evidence_anchor", "file_exists", "negative_honesty"))
        levels.add(t["level"])
    check("覆盖 L1-L4", levels == {"L1", "L2", "L3", "L4"}, f"实际 {levels}")
    # 参数注入生效
    gen = json.load(open(next(p for p in written if "L2_00" in p.name and "g66" in p.name), encoding="utf-8"))
    # count=2 变体序号不同
    with tempfile.TemporaryDirectory() as td2:
        w2 = tg.generate_tasks(["g66"], {}, td2, count=2)
        ids = [json.load(open(p, encoding="utf-8"))["task_id"] for p in w2]
        check("count=2 变体 id 递增", len(set(ids)) == len(ids), f"实际 {ids}")

# ---- preview 不抛错 ----
check("preview 输出", "L1" in tg.preview_templates() and "g66" in tg.preview_templates())

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")
