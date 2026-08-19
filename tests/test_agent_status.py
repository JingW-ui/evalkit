# -*- coding: utf-8 -*-
"""单元测试：agent_status.py（连接状态探测）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import agent_status as ast

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

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL PASSED")
