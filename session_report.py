#!/usr/bin/env python3
"""
session_report.py — 单会话深度评测 HTML 报告。

与 report_interactive.py（跨会话总览 + 四级下钻）不同，这里把**单个会话**做深做细：
  - 任务按「真实用户对话」切分（一条不以 / 或 < 开头的 type:user 消息 = 一个任务）
  - 噪音（/clear、local-command-caveat、ai-title、last-prompt 等）自动忽略
  - 每个任务独立统计：query、起止时间、耗时、skill 触发、token 增量、工具调用、嵌套子任务
  - TaskCreate/TaskUpdate 关联：TaskCreate 常不带 id，靠出现顺序配对 TaskUpdate 的数字 id
  - 阻塞判定：只看真实 status == "blocked"，不虚构阈值阻塞

用法：
    python session_report.py --jsonl <单个 .jsonl 路径> [--out results/session_report.html]
"""

import json
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone

try:
    import cost as cost_mod
except ImportError:
    cost_mod = None


# ===== 颜色主题（GitHub 风格：绿色为主，仅少量红色点缀） =====
ACCENT = "#2da44e"        # GitHub 绿，主强调色
ACCENT_DIM = "#238636"    # 绿（深一档，用于 hover/次要）
RED = "#cf222e"           # 仅少量点缀：错误/阻塞等负面信号
SURFACE = "#0d1117"       # GitHub dark 背景
CARD = "#161b22"          # 卡片/面板
INK_PRIMARY = "#e6edf3"
INK_SECONDARY = "#8b949e"
INK_MUTED = "#6e7681"
GRID = "#30363d"

# 匹配工具参数里引用 skill 目录的路径：.../skills/<名>/... （用于「脚本直用」检测）
_SKILL_DIR_RE = re.compile(r"skills[/\\]([A-Za-z0-9._-]+)[/\\]")


def _parse_ts(ts):
    """ISO 时间戳字符串 → epoch 秒；失败返回 None。"""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def _is_real_user(content):
    """判定一条 user 消息是否为真实任务指令（排除 / 开头、< 开头、空、Skill 注入等系统消息）。"""
    if not isinstance(content, str):
        return False
    c = content.strip()
    if not c:
        return False
    if c.startswith("/") or c.startswith("<"):
        return False
    # Skill 预加载注入的系统消息（非真实用户指令）
    if c.startswith("Base directory for this skill:"):
        return False
    return True


def _fmt_tokens(n):
    """token 数人类可读：>1亿 → 亿；>1万 → 万；否则千分位。"""
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f} 亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f} 万"
    return f"{n:,}"


def _fmt_duration(seconds):
    """秒 → 人类可读时长。"""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m} 分 {s} 秒"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h} 小时 {m} 分"


# ===== 核心解析 =====

def scan_single_session(jsonl_path):
    """
    解析单个 Claude Code session JSONL。

    Returns:
        dict：{session_id, model_usage, tool_dist, token 汇总, tasks[], task_subitems[], blocks, ...}
    """
    path = Path(jsonl_path)
    session_id = path.stem

    # 累积量
    model_usage = {}        # model -> 轮数
    tool_dist = {}          # 工具名 -> 次数
    tokens = {
        "input": 0, "cache_read": 0, "cache_write": 0, "output": 0,
    }
    ts_first = None
    ts_last = None

    tasks = []              # 真实用户任务（按对话切分）
    cur_task = None         # 当前任务指针
    # 全事件时间戳（供「等待人输入」空窗计算）
    _events = []            # 有序 [(epoch_s, type)]，全部事件

    # TaskCreate/TaskUpdate 关联：TaskCreate 常不带 id，按出现顺序配对数字 id
    creates = []            # 出现顺序的 TaskCreate（subject/description/...）
    updates = []            # 所有 TaskUpdate（taskId, status, ts）
    skill_events = []       # {skill_name, ts}

    # 人工介入（AskUserQuestion）+ 任务完成判定
    human_interventions = []     # {header, question, options, ts}
    stop_reasons = []            # 有序 stop_reason（含时间戳）
    final_texts = []             # 每轮 assistant 的最终 text 合并（用于完成判定）

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type", "")
            ts = obj.get("timestamp", "")

            # 时间线
            if ts:
                if ts_first is None:
                    ts_first = ts
                ts_last = ts

            # 全事件时间戳（供「等待人输入」空窗计算）
            ets = _parse_ts(ts)
            if ets is not None:
                _events.append((ets, t))
            # 真实用户任务：新任务起点
            if t == "user":
                content = obj.get("message", {}).get("content", "")
                if _is_real_user(content):
                    # 结束上一个任务：用它的最后一条 assistant 时间戳（而非本条 user 时间），
                    # 否则跨夜 / 跨任务的等待空窗会被误算进上一个任务
                    if cur_task is not None:
                        cur_task["end_ts"] = cur_task.get("_last_assistant_ts") or ts
                    cur_task = {
                        "query": content.strip(),
                        "start_ts": ts,
                        "end_ts": None,
                        "tool_calls": 0,
                        "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0},
                        "skill_loaded": None,        # 显式 Skill 工具加载的 skill 名（首次）
                        "skill_count": 0,
                        "skill_via_script": None,    # 直接 Bash/Read 引用 skills/<名>/ 目录判定的 skill 名（首次）
                        "skill_via_script_count": 0,
                        "human_interventions": 0,    # 该任务内 AskUserQuestion 次数
                        "stop_reason": None,         # 该任务最后一条 assistant 的 stop_reason
                        "has_final_text": False,     # 该任务是否有最终 text 输出（非工具调用）
                        "_events": [],               # 该任务区间内的事件时间戳 (epoch_s, type)
                        "_last_assistant_ts": None,  # 该任务最后一条 assistant 消息时间戳
                    }
                    tasks.append(cur_task)

            elif t == "assistant":
                msg = obj.get("message", {})
                model = msg.get("model", "")
                if model:
                    model_usage[model] = model_usage.get(model, 0) + 1

                # 记录该任务最后一条 assistant 时间戳（用于精确界定任务结束，避免跨夜空窗）
                if cur_task is not None:
                    cur_task["_last_assistant_ts"] = ts

                # stop_reason + 最终 text（用于任务完成判定）
                sr = msg.get("stop_reason", "")
                stop_reasons.append({"stop_reason": sr, "ts": ts})
                if cur_task is not None:
                    cur_task["stop_reason"] = sr
                # 看这轮有没有 text block（非工具调用）
                for blk in msg.get("content", []):
                    if isinstance(blk, dict) and blk.get("type") == "text" and blk.get("text", "").strip():
                        if cur_task is not None:
                            cur_task["has_final_text"] = True
                        break

                usage = msg.get("usage", {})
                ui = usage.get("input_tokens", 0)
                ur = usage.get("cache_read_input_tokens", 0)
                uw = usage.get("cache_creation_input_tokens", 0)
                uo = usage.get("output_tokens", 0)
                tokens["input"] += ui
                tokens["cache_read"] += ur
                tokens["cache_write"] += uw
                tokens["output"] += uo

                # 归到当前任务
                if cur_task is not None:
                    cur_task["tokens"]["input"] += ui
                    cur_task["tokens"]["cache_read"] += ur
                    cur_task["tokens"]["cache_write"] += uw
                    cur_task["tokens"]["output"] += uo

                # 工具调用
                for blk in msg.get("content", []):
                    if not (isinstance(blk, dict) and blk.get("type") == "tool_use"):
                        continue
                    name = blk.get("name", "")
                    if not name:
                        continue
                    tool_dist[name] = tool_dist.get(name, 0) + 1
                    if cur_task is not None:
                        cur_task["tool_calls"] += 1

                    inp = blk.get("input", {})

                    if name == "Skill":
                        sk = inp.get("skill", "") or "（未命名）"
                        skill_events.append({"skill_name": sk, "ts": ts})
                        if cur_task is not None:
                            cur_task["skill_count"] += 1
                            if cur_task["skill_loaded"] is None:
                                cur_task["skill_loaded"] = sk
                    else:
                        # 脚本直用检测：任意工具参数里引用 skills/<名>/ 目录
                        # 说明 agent 绕开 Skill 工具，直接执行了某 skill 目录下的脚本
                        inp_str = json.dumps(inp, ensure_ascii=False)
                        m = _SKILL_DIR_RE.search(inp_str)
                        if m:
                            sk_name = m.group(1)
                            if cur_task is not None:
                                cur_task["skill_via_script_count"] += 1
                                if cur_task["skill_via_script"] is None:
                                    cur_task["skill_via_script"] = sk_name

                    if name == "AskUserQuestion":
                        # 人工介入检测：agent 中途反向追问用户
                        qs = inp.get("questions", [])
                        for q in qs:
                            opts = [o.get("label", "") for o in q.get("options", [])]
                            human_interventions.append({
                                "header": q.get("header", ""),
                                "question": q.get("question", ""),
                                "options": opts,
                                "ts": ts,
                            })
                        if cur_task is not None:
                            cur_task["human_interventions"] += len(qs) if qs else 1

                    if name == "TaskCreate":
                        creates.append({
                            "subject": inp.get("subject", ""),
                            "description": inp.get("description", ""),
                            "ts": ts,
                        })

                    elif name == "TaskUpdate":
                        updates.append({
                            "task_id": inp.get("taskId", inp.get("id", "")),
                            "status": inp.get("status", ""),
                            "ts": ts,
                        })

            # 把本事件时间戳归入当前任务（在 user 分支创建任务之后 + assistant 分支之后都正确）
            if ets is not None and cur_task is not None:
                cur_task["_events"].append((ets, t))

    # 收尾：最后一个任务（用它的最后一条 assistant 时间戳，若全程无 assistant 则退回 ts_last）
    if cur_task is not None:
        cur_task["end_ts"] = cur_task.get("_last_assistant_ts") or ts_last

    # ===== TaskCreate/TaskUpdate 配对（顺序对应） =====
    # TaskCreate 无 id，TaskUpdate 的 task_id 是 "1"~"N"，按出现顺序一一对应
    update_by_order = []
    for u in updates:
        try:
            order = int(str(u["task_id"]))
        except (ValueError, TypeError):
            order = None
        update_by_order.append((order, u))

    task_subitems = []
    for idx, c in enumerate(creates):
        # 第 idx 个子任务（0-based）对应 taskId = idx+1
        target_order = idx + 1
        status = "pending"
        start_ts = c["ts"]
        completed_ts = None
        for order, u in update_by_order:
            if order == target_order:
                if u["status"] == "in_progress":
                    status = "in_progress"
                    start_ts = u["ts"]
                elif u["status"] == "completed":
                    status = "completed"
                    completed_ts = u["ts"]
                elif u["status"] == "blocked":
                    status = "blocked"

        dur = None
        if start_ts and completed_ts:
            t0 = _parse_ts(start_ts)
            t1 = _parse_ts(completed_ts)
            if t0 is not None and t1 is not None:
                dur = t1 - t0

        # 归属：找到起始时间之后的第一个用户任务
        bel = None
        c_ts = _parse_ts(c["ts"])
        for tk in tasks:
            s_ts = _parse_ts(tk["start_ts"])
            if s_ts is not None and c_ts is not None:
                dif = s_ts - c_ts
                # 任务在 create 之前或 create 之后最近的那个
                if s_ts <= c_ts:  # create 发生在任务开始之后
                    bel = tk["query"]
                else:
                    break

        task_subitems.append({
            "subject": c["subject"],
            "description": c["description"],
            "status": status,
            "duration_s": dur,
            "belongs_to": bel,
            "created_ts": c["ts"],
        })

    # ===== 汇总 =====
    total_tool_calls = sum(tool_dist.values())
    skill_triggered_tasks = sum(1 for tk in tasks if tk["skill_count"] > 0)
    skill_script_tasks = sum(1 for tk in tasks if tk["skill_via_script_count"] > 0)
    # 任一方式（显式 Skill 工具 或 脚本直用）都算「使用了 skill」
    skill_used_tasks = sum(
        1 for tk in tasks if (tk["skill_count"] > 0 or tk["skill_via_script_count"] > 0)
    )
    blocks = [s for s in task_subitems if s["status"] == "blocked"]
    completed_sub = sum(1 for s in task_subitems if s["status"] == "completed")

    # 计算每个任务的耗时 + 等待人输入
    WAIT_GAP_S = 15.0
    for tk in tasks:
        t0 = _parse_ts(tk["start_ts"])
        t1 = _parse_ts(tk["end_ts"])
        tk["duration_s"] = (t1 - t0) if (t0 is not None and t1 is not None) else None
        # 等待人输入：任务区间 [start_ts, end_ts] 内「无模型活动的空闲空窗」总和。
        # 只在区间内统计，且排除落在 end_ts 之后的事件（那段属于下个任务或跨夜空窗）。
        wait_s = 0.0
        evs = [e for e in tk.get("_events", []) if t0 is not None and t1 is not None and t0 <= e[0] <= t1]
        for i in range(1, len(evs)):
            gap = evs[i][0] - evs[i - 1][0]
            if gap > WAIT_GAP_S:
                wait_s += gap
        tk["wait_s"] = wait_s
        tk["active_s"] = (tk["duration_s"] - wait_s) if tk["duration_s"] is not None else None
        del tk["_events"]  # 不暴露内部时间戳
        tk.pop("_last_assistant_ts", None)  # 不暴露内部字段

    # ===== 任务完成判定（启发式） =====
    # 判据：has_final_text（输出了最终结论文本）优先视为「已完成」；
    # stop_reason == end_turn 且 has_final_text 更可信；tool_use 结尾且无 text → 疑似中断。
    for tk in tasks:
        has_text = tk.get("has_final_text", False)
        stop = tk.get("stop_reason", "")
        if has_text and stop == "end_turn":
            tk["completion"] = "completed"
            tk["completion_reason"] = "有最终结论文本 + 正常收尾"
        elif has_text:
            tk["completion"] = "completed"
            tk["completion_reason"] = "有最终结论文本"
        elif stop == "end_turn":
            tk["completion"] = "partial"
            tk["completion_reason"] = "正常收尾但无明确文本结论"
        else:
            tk["completion"] = "interrupted"
            tk["completion_reason"] = "以工具调用结尾，未见最终结论"

    # 会话总耗时
    session_duration = None
    t_first = _parse_ts(ts_first)
    t_last = _parse_ts(ts_last)
    if t_first is not None and t_last is not None:
        session_duration = t_last - t_first

    # 会话级「等待人输入」汇总（全部相邻事件 > 15s 空窗之和）
    total_wait_s = 0.0
    WAIT_GAP_S = 15.0
    for i in range(1, len(_events)):
        gap = _events[i][0] - _events[i - 1][0]
        if gap > WAIT_GAP_S:
            total_wait_s += gap
    session_active_s = (session_duration - total_wait_s) if session_duration is not None else None

    # ===== JSONL 成本估算（无 cost 字段，用挂牌价理论成本 × 平台稳定加价） =====
    cost_analysis = None
    if cost_mod is not None:
        dom_model = None
        if model_usage:
            dom_model = max(model_usage.items(), key=lambda kv: kv[1])[0]
        if dom_model:
            tokens_for_cost = {
                "input_tokens": tokens["input"],
                "output_tokens": tokens["output"],
                "cache_read_input_tokens": tokens["cache_read"],
                "cache_creation_input_tokens": tokens["cache_write"],
            }
            theo = 0.0
            try:
                theo = cost_mod.theoretical_cost(dom_model, tokens_for_cost)
            except Exception:
                theo = 0.0
            stable = None
            try:
                stable = cost_mod.get_markup(dom_model)
            except Exception:
                stable = None
            cost_analysis = {
                "model": dom_model,
                "theoretical_cost": round(theo, 4),
                "stable_markup": stable,
                "estimated_cost": round(theo * stable, 4) if (theo > 0 and stable) else None,
            }

    return {
        "session_id": session_id,
        "jsonl_path": str(path),
        "range": {"first": ts_first, "last": ts_last},
        "duration_s": session_duration,
        "wait_s": total_wait_s,
        "active_s": session_active_s,
        "model_usage": model_usage,
        "tool_dist": tool_dist,
        "total_tool_calls": total_tool_calls,
        "tokens": tokens,
        "tasks": tasks,
        "task_subitems": task_subitems,
        "skill_events": skill_events,
        "skill_triggered_tasks": skill_triggered_tasks,
        "skill_script_tasks": skill_script_tasks,
        "skill_used_tasks": skill_used_tasks,
        "task_count": len(tasks),
        "blocks": blocks,
        "completed_sub": completed_sub,
        "total_sub": len(task_subitems),
        "human_interventions": human_interventions,
        "total_human_interventions": len(human_interventions),
        "cost_analysis": cost_analysis,
    }


# ===== airLab pod 纯文本日志解析 =====

def scan_airlab_log(path):
    """
    解析 airLab pod 的 CCAgent 纯文本运行日志，输出与 scan_single_session 同构的 dict。

    与标准 Claude Code JSONL 不同，这种日志是纯文本行，关键差异：
      - 只有一条用户指令：`prompt='...'`（= 唯一任务）
      - token 只在结尾 `ResultMessage ... usage={...}` 给总量，无逐轮 token
      - 工具调用靠 `[CC 🔧 <工具名>] <参数>` 提取
      - 无 TaskCreate/TaskUpdate，故子任务面板为空
      - skill 来源：启动行 `skills=['xxx']` + skill 目录脚本直用
    """
    txt = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = txt.splitlines()

    session_id = Path(path).stem

    # --- 任务：唯一 prompt ---
    prompt_m = re.search(r"prompt='([^']*)'", txt)
    prompt = prompt_m.group(1).strip() if prompt_m else ""

    # --- skill 配置 ---
    skills_m = re.search(r"skills=\[([^\]]*)\]", txt)
    skill_cfg = None
    if skills_m:
        skill_cfg = skills_m.group(1).strip().strip("'\"") or None

    # --- 模型 / 起止时间 ---
    model_m = re.search(r"model=(\S+)", txt)
    model = model_m.group(1) if model_m else ""
    ts_first = None
    ts_last = None
    tm = re.match(r"\[(\d\d:\d\d:\d\d)\]", lines[0]) if lines else None
    if tm:
        ts_first = tm.group(1)
    tm2 = None
    for ln in reversed(lines):
        mm = re.match(r"\[(\d\d:\d\d:\d\d)\]", ln)
        if mm:
            tm2 = mm.group(1)
            break
    if tm2:
        ts_last = tm2

    # --- 总耗时 / 成本 / turns / token 总量 ---
    dur_s = None
    dm = re.search(r"用时=(\d+)m(\d+)s", txt)
    if dm:
        dur_s = int(dm.group(1)) * 60 + int(dm.group(2))
    cost_m = re.search(r"cost=\$([0-9.]+)", txt)
    cost = float(cost_m.group(1)) if cost_m else None
    turns = None
    turns_m = re.search(r"(?<![a-z_])turns=(\d+)", txt)
    if turns_m:
        turns = int(turns_m.group(1))
    token_input = 0
    token_cache_read = 0
    token_cache_write = 0
    token_output = 0
    um = re.search(r"usage=\{([^}]*)\}", txt)
    if um:
        body = um.group(1)
        def _g(key):
            mm = re.search(r"['\"]?" + key + r"['\"]?\s*[:=]\s*(\d+)", body)
            return int(mm.group(1)) if mm else 0
        token_input = _g("input_tokens")
        token_cache_read = _g("cache_read_input_tokens")
        token_cache_write = _g("cache_creation_input_tokens")
        token_output = _g("output_tokens")

    # --- 工具调用序列 ---
    tool_dist = {}
    tool_seq = []
    for ln in lines:
        m = re.search(r"\[CC 🔧 ([^\]]+)\]", ln)
        if m:
            name = m.group(1).strip()
            tool_dist[name] = tool_dist.get(name, 0) + 1
            tool_seq.append(name)

    total_tool_calls = sum(tool_dist.values())

    # --- 子任务解析（TaskCreate / TaskUpdate） ---
    # pod 文本日志里 TaskCreate 行无 taskId，TaskUpdate 行含 taskId "1"~"N"，
    # 与 JSONL 相同：靠「出现顺序」配对（第 i 个 TaskCreate ← taskId = str(i+1)）。
    creates = []            # 顺序的 TaskCreate（subject/description/ts）
    updates = []            # TaskUpdate（task_id/status/ts）
    for ln in lines:
        m = re.match(r"\[(\d\d:\d\d:\d\d)\] \[CC 🔧 (TaskCreate|TaskUpdate)\] (\{.*\})", ln)
        if not m:
            continue
        ts = m.group(1)
        kind = m.group(2)
        try:
            param = json.loads(m.group(3))
        except json.JSONDecodeError:
            continue
        if kind == "TaskCreate":
            creates.append({
                "subject": param.get("subject", ""),
                "description": param.get("description", ""),
                "ts": ts,
            })
        else:
            updates.append({
                "task_id": param.get("taskId", param.get("id", "")),
                "status": param.get("status", ""),
                "ts": ts,
            })

    # 配对：第 idx 个 TaskCreate（0-based）对应 taskId = idx+1 的 TaskUpdate
    update_by_order = {}
    for u in updates:
        try:
            order = int(str(u["task_id"]))
        except (ValueError, TypeError):
            order = None
        update_by_order[order] = u

    def _hm_to_s(ts):
        """HH:MM:SS → 秒（无日期，按当日）；失败返回 None。"""
        try:
            h, mi, s = ts.split(":")
            return int(h) * 3600 + int(mi) * 60 + int(s)
        except (ValueError, AttributeError):
            return None

    task_subitems = []
    for idx, c in enumerate(creates):
        target_order = idx + 1
        status = "pending"
        start_ts = c["ts"]
        completed_ts = None
        for order, u in update_by_order.items():
            if order == target_order:
                if u["status"] == "in_progress":
                    status = "in_progress"
                    start_ts = u["ts"]
                elif u["status"] == "completed":
                    status = "completed"
                    completed_ts = u["ts"]
                elif u["status"] == "blocked":
                    status = "blocked"
        dur = None
        t0 = _hm_to_s(start_ts)
        t1 = _hm_to_s(completed_ts) if completed_ts else None
        if t0 is not None and t1 is not None:
            dur = t1 - t0
            if dur < 0:
                dur = None  # 跨天或乱序，退化为不显示
        task_subitems.append({
            "subject": c["subject"],
            "description": c["description"],
            "status": status,
            "duration_s": dur,
            "belongs_to": prompt,  # 单任务，全部归属该指令
            "created_ts": c["ts"],
        })

    blocks = [s for s in task_subitems if s["status"] == "blocked"]
    completed_sub = sum(1 for s in task_subitems if s["status"] == "completed")

    # --- skill 事件 ---
    skill_events = []
    if skill_cfg:
        skill_events.append({"skill_name": skill_cfg, "ts": ts_first or ""})

    # --- 脚本直用检测（从工具参数里找 skills/<名>/） ---
    script_skills = set()
    for ln in lines:
        m = re.search(r"\[CC 🔧 ([^\]]+)\] (.*)", ln)
        if m:
            arg = m.group(2)
            sm = _SKILL_DIR_RE.search(arg)
            if sm:
                script_skills.add(sm.group(1))

    # --- 人工介入 ---
    human_interventions = []
    # airLab pod 日志一般是单指令纯执行；若出现 AskUserQuestion 文本则记录
    for ln in lines:
        if "AskUserQuestion" in ln or "你想" in ln or "请选择" in ln:
            human_interventions.append({
                "header": "澄清",
                "question": ln.strip()[:200],
                "options": [],
                "ts": "",
            })

    # --- 结果（完成判定） ---
    # 完成信号：结尾有「任务已完成」或 ResultMessage 且无明显 error
    completed = ("任务已完成" in txt or "CC RESULT" in txt) and "Error" not in txt and "Traceback" not in txt
    final_text = "任务已完成" in txt

    # --- 组装同构 dict ---
    # 单任务
    task = {
        "query": prompt,
        "start_ts": ts_first or "",
        "end_ts": ts_last or "",
        "duration_s": dur_s,
        "tool_calls": total_tool_calls,
        "tokens": {
            "input": token_input, "cache_read": token_cache_read,
            "cache_write": token_cache_write, "output": token_output,
        },
        "skill_loaded": skill_cfg,
        "skill_count": 1 if skill_cfg else 0,
        "skill_via_script": sorted(script_skills)[0] if script_skills else None,
        "skill_via_script_count": len(script_skills),
        "human_interventions": len(human_interventions),
        "stop_reason": "end_turn" if final_text else "unknown",
        "has_final_text": final_text,
        "completion": "completed" if completed else "interrupted",
        "completion_reason": "结尾输出「任务已完成」并成功收尾" if completed else "未见完整收尾信号",
    }

    model_usage = {}
    if model:
        model_usage[model] = turns or 0

    skill_used_tasks = 1 if (skill_cfg or script_skills) else 0

    # --- 成本分析：动态反推平台加价系数 ---
    cost_analysis = None
    if cost_mod is not None and cost is not None and model:
        tokens_for_cost = {
            "input_tokens": token_input,
            "output_tokens": token_output,
            "cache_read_input_tokens": token_cache_read,
            "cache_creation_input_tokens": token_cache_write,
        }
        try:
            cost_analysis = cost_mod.effective_cost(model, cost, tokens_for_cost)
        except Exception:
            cost_analysis = None

    return {
        "session_id": session_id,
        "jsonl_path": str(path),
        "range": {"first": ts_first, "last": ts_last},
        "duration_s": dur_s,
        "model_usage": model_usage,
        "tool_dist": tool_dist,
        "total_tool_calls": total_tool_calls,
        "tokens": {
            "input": token_input, "cache_read": token_cache_read,
            "cache_write": token_cache_write, "output": token_output,
        },
        "tasks": [task] if prompt else [],
        "task_subitems": task_subitems,
        "skill_events": skill_events,
        "skill_triggered_tasks": 1 if skill_cfg else 0,
        "skill_script_tasks": 1 if script_skills else 0,
        "skill_used_tasks": skill_used_tasks,
        "task_count": 1 if prompt else 0,
        "blocks": blocks,
        "completed_sub": completed_sub,
        "total_sub": len(task_subitems),
        "human_interventions": human_interventions,
        "total_human_interventions": len(human_interventions),
        # 附加信息（airlab 专属）
        "cost_usd": cost,
        "turns": turns,
        "airlab": True,
        "cost_analysis": cost_analysis,
    }


# ===== HTML 渲染 =====

def render_html(data):
    """把解析结果渲染成自包含 HTML。"""
    # 把数据序列化，注入 textarea（避免特殊字符破坏 JS）
    data_json = json.dumps(data, ensure_ascii=False)

    # 预计算一些渲染数值
    fmt_tok = _fmt_tokens
    fmt_dur = _fmt_duration

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>单会话评测报告 · {data['session_id'][:8]}</title>
<style>
  :root {{
    color-scheme: dark;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: {SURFACE};
    color: {INK_PRIMARY};
    font-size: 12.5px;
    line-height: 1.45;
  }}
  .wrap {{ max-width: 1280px; margin: 0 auto; padding: 20px 16px 48px; }}

  .hero {{
    background: {CARD};
    border: 1px solid {GRID};
    border-radius: 8px;
    padding: 14px 18px;
    margin-bottom: 12px;
  }}
  .hero h1 {{
    margin: 0 0 2px;
    font-size: 16px;
    font-weight: 700;
  }}
  .hero .sub {{
    color: {INK_MUTED};
    font-size: 11px;
    margin-bottom: 10px;
    word-break: break-all;
  }}
  .hero-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 8px 16px;
  }}
  .hitem {{ text-align: left; }}
  .hitem .v {{ font-size: 17px; font-weight: 700; color: {ACCENT}; line-height: 1.2; }}
  .hitem .k {{ color: {INK_MUTED}; font-size: 11px; }}

  .panel {{
    background: {CARD};
    border: 1px solid {GRID};
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 10px;
  }}
  .panel h2 {{
    margin: 0 0 10px;
    font-size: 14px;
    font-weight: 700;
    color: {INK_PRIMARY};
  }}

  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 10px;
  }}
  .stat {{
    background: {SURFACE};
    border: 1px solid {GRID};
    border-radius: 6px;
    padding: 10px 12px;
  }}
  .stat .label {{ color: {INK_MUTED}; font-size: 11px; }}
  .stat .value {{ font-size: 15px; font-weight: 600; margin-top: 2px; }}

  .task-card {{
    border: 1px solid {GRID};
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 8px;
    background: {SURFACE};
  }}
  .task-card .q {{ font-size: 13px; font-weight: 600; margin-bottom: 2px; }}
  .task-card .meta {{ color: {INK_SECONDARY}; font-size: 11px; margin-bottom: 6px; }}

  details.section {{
    border: 1px solid {GRID};
    border-radius: 8px;
    margin-bottom: 10px;
    background: {CARD};
  }}
  details.section > summary {{
    cursor: pointer;
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 700;
    color: {INK_PRIMARY};
    list-style: none;
  }}
  details.section > summary::before {{
    content: "▸ ";
    color: {INK_MUTED};
  }}
  details.section[open] > summary::before {{
    content: "▾ ";
    color: {ACCENT};
  }}
  details.section > summary .cnt {{
    float: right;
    font-size: 11px;
    font-weight: 500;
    color: {INK_MUTED};
  }}
  details.section > .detail-body {{
    padding: 8px 16px 14px;
    border-top: 1px solid {GRID};
  }}
  .badge {{
    display: inline-block;
    padding: 1px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
    margin-right: 4px;
  }}
  .badge-skill {{ background: rgba(45,164,78,0.15); color: #3fb950; }}
  .badge-noskill {{ background: rgba(110,118,129,0.18); color: {INK_MUTED}; }}
  .badge-blocked {{ color: {RED}; font-weight: 700; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid {GRID}; }}
  th {{ color: {INK_MUTED}; font-weight: 600; font-size: 11px; }}
  td.num, th.num {{ text-align: right; font-family: ui-monospace, monospace; }}
  .status-icon {{ font-size: 13px; }}

  .muted {{ color: {INK_MUTED}; }}
  .empty {{ color: {INK_MUTED}; font-style: italic; }}
  .mono {{ font-family: ui-monospace, monospace; }}
</style>
</head>
<body>
<div class="wrap" id="root">加载中…</div>
<textarea id="data-store" style="display:none;">{data_json}</textarea>
<script>
(function() {{
  const data = JSON.parse(document.getElementById('data-store').value);
  const root = document.getElementById('root');

  function esc(s) {{
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }}[c]));
  }}
  function fmtTok(n) {{
    if (n >= 100000000) return (n/100000000).toFixed(2) + ' 亿';
    if (n >= 10000) return (n/10000).toFixed(1) + ' 万';
    return n.toLocaleString();
  }}
  function fmtDur(s) {{
    if (s === null || s === undefined) return '-';
    if (s < 60) return s.toFixed(1) + ' 秒';
    if (s < 3600) return Math.floor(s/60) + ' 分 ' + Math.floor(s%60) + ' 秒';
    return Math.floor(s/3600) + ' 小时 ' + Math.floor((s%3600)/60) + ' 分';
  }}
  function fmtTime(iso) {{
    if (!iso) return '-';
    return iso.replace('T', ' ').replace('Z', '').slice(0, 19);
  }}
  const STATUS_ICON = {{
    'completed': '✅', 'in_progress': '🔄', 'blocked': '⛔', 'pending': '⏳'
  }};
  const STATUS_LABEL = {{
    'completed': '已完成', 'in_progress': '进行中', 'blocked': '阻塞', 'pending': '待处理'
  }};
  function statusCell(s) {{
    const icon = STATUS_ICON[s.status] || '';
    const label = STATUS_LABEL[s.status] || s.status;
    const cls = s.status === 'blocked' ? ' class="badge-blocked"' : '';
    return '<span' + cls + '>' + icon + ' ' + label + '</span>';
  }}

  let h = '';

  const tok = data.tokens;

  // ===== Hero（精简一行 + 紧凑指标行） =====
  h += '<div class="hero">';
  h += '<h1>单会话评测报告</h1>';
  h += '<div class="sub mono">' + esc(data.session_id) + '</div>';
  h += '<div class="hero-grid">';
  h += '<div class="hitem"><div class="v">' + data.task_count + '</div><div class="k">真实任务数</div></div>';
  h += '<div class="hitem"><div class="v">' + fmtDur(data.active_s) + '</div><div class="k">模型活跃耗时</div></div>';
  h += '<div class="hitem"><div class="v">' + fmtDur(data.wait_s) + '</div><div class="k">等待人输入</div></div>';
  h += '<div class="hitem"><div class="v">' + (data.task_count ? (100*data.skill_used_tasks/data.task_count).toFixed(0) + '%' : '-') + '</div><div class="k">Skill 使用率</div></div>';
  h += '<div class="hitem"><div class="v">' + fmtTok(tok.input + tok.cache_read + tok.output) + '</div><div class="k">总 Token</div></div>';
  h += '<div class="hitem"><div class="v">' + data.total_tool_calls + '</div><div class="k">工具调用总数</div></div>';
  h += '<div class="hitem"><div class="v">' + (data.total_human_interventions || 0) + '</div><div class="k">人工介入</div></div>';
  h += '</div>';
  h += '</div>';

  // ===== 总览仪表盘（总指标 + 成本 + Skill 口径 + 模型，网格并排） =====
  h += '<div class="panel"><h2>总览仪表盘</h2>';
  h += '<div class="grid">';

  const cacheTotal = tok.input + tok.cache_read;
  const cacheHit = cacheTotal ? (100 * tok.cache_read / cacheTotal).toFixed(1) : 0;
  const rExplicit = data.task_count ? (100*data.skill_triggered_tasks/data.task_count).toFixed(0) + '%' : '-';
  const rUsed = data.task_count ? (100*data.skill_used_tasks/data.task_count).toFixed(0) + '%' : '-';

  // token 分项
  h += '<div class="stat"><div class="label">Token 分项</div><div class="value">' + fmtTok(tok.input + tok.cache_read + tok.output) + '</div><div class="label" style="margin-top:4px">in ' + fmtTok(tok.input) + ' · cr ' + fmtTok(tok.cache_read) + ' · cw ' + fmtTok(tok.cache_write) + ' · out ' + fmtTok(tok.output) + '</div><div class="label">Cache 命中 ' + cacheHit + '%</div></div>';

  // 任务完成度
  h += '<div class="stat"><div class="label">任务完成度</div><div class="value">' + data.completed_sub + ' / ' + data.total_sub + '</div><div class="label" style="margin-top:4px">子任务完成数 / 总数</div></div>';

  // Skill 两口径
  h += '<div class="stat"><div class="label">Skill · 显式</div><div class="value">' + rExplicit + '</div><div class="label" style="margin-top:4px">' + data.skill_triggered_tasks + ' / ' + data.task_count + ' 任务</div></div>';
  h += '<div class="stat"><div class="label">Skill · 含脚本直用</div><div class="value">' + rUsed + '</div><div class="label" style="margin-top:4px">' + data.skill_used_tasks + ' / ' + data.task_count + ' 任务</div></div>';

  // 成本计价：airLab（有 actual_cost + 本次/稳定加价）或 JSONL（估算值）
  if (data.cost_analysis) {{
    const ca = data.cost_analysis;
    if (data.airlab) {{
      h += '<div class="stat"><div class="label">实际结算成本</div><div class="value">¥' + ca.actual_cost.toFixed(4) + '</div><div class="label" style="margin-top:4px">挂牌价 ¥' + ca.theoretical_cost.toFixed(4) + '</div></div>';
      h += '<div class="stat"><div class="label">平台加价（本次）</div><div class="value">' + (ca.markup ? ca.markup.toFixed(3) + 'x' : '-') + '</div><div class="label" style="margin-top:4px">由 (cost, usage) 反推</div></div>';
      h += '<div class="stat"><div class="label">平台加价（稳定值）</div><div class="value">' + (ca.stable_markup ? ca.stable_markup.toFixed(3) + 'x' : '-') + '</div><div class="label" style="margin-top:4px">同模型历史样本中位数</div></div>';
    }} else {{
      h += '<div class="stat"><div class="label">挂牌价成本</div><div class="value">¥' + ca.theoretical_cost.toFixed(4) + '</div><div class="label" style="margin-top:4px">' + esc(ca.model || '') + ' · 按官方公开单价</div></div>';
    }}
  }}

  h += '</div>';

  // 成本单价（airLab）副注
  if (data.airlab && data.cost_analysis && data.cost_analysis.unit_cost) {{
    const uc = data.cost_analysis.unit_cost;
    h += '<div class="muted" style="margin-top:8px;font-size:11px">实际结算单价（¥/百万 tokens）：输入 ¥' + uc.input.toFixed(2) + ' · 输出 ¥' + uc.output.toFixed(2) + ' · 缓存命中 ¥' + uc.cache_read.toFixed(4) + '</div>';
  }}
  h += '<div class="muted" style="margin-top:4px;font-size:11px">Skill「含脚本直用」额外识别了绕开 Skill 工具、直接 Bash/Read 执行 skills/&lt;名&gt;/ 目录下脚本的使用方式（agent 预加载后常不再显式调用 Skill 工具）。</div>';
  h += '</div>';

  // ===== 工具分布 + 模型使用（紧凑表格，两栏并排） =====
  h += '<div class="panel"><h2>工具与模型</h2><div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">';

  h += '<div>';
  h += '<div class="muted" style="font-size:11px;margin-bottom:6px">工具调用分布（共 ' + data.total_tool_calls + ' 次）</div>';
  const toolEntries = Object.entries(data.tool_dist).sort((a,b) => b[1]-a[1]);
  h += '<table><thead><tr><th>工具</th><th class="num">次数</th><th class="num">占比</th></tr></thead><tbody>';
  toolEntries.forEach(([name, cnt]) => {{
    const pct = data.total_tool_calls ? (100*cnt/data.total_tool_calls).toFixed(1) + '%' : '-';
    h += '<tr><td title="' + esc(name) + '" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(name) + '</td><td class="num">' + cnt + '</td><td class="num muted">' + pct + '</td></tr>';
  }});
  h += '</tbody></table></div>';

  h += '<div>';
  h += '<div class="muted" style="font-size:11px;margin-bottom:6px">模型使用</div>';
  const mEntries = Object.entries(data.model_usage);
  const totalModel = mEntries.reduce((s, e) => s + e[1], 0);
  h += '<table><thead><tr><th>模型</th><th class="num">轮数</th><th class="num">占比</th></tr></thead><tbody>';
  mEntries.forEach(([m, cnt]) => {{
    const pct = totalModel ? (100*cnt/totalModel).toFixed(1) + '%' : '-';
    h += '<tr><td title="' + esc(m) + '" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(m) + '</td><td class="num">' + cnt + '</td><td class="num muted">' + pct + '</td></tr>';
  }});
  h += '</tbody></table></div>';

  h += '</div></div>';

  // ===== 任务列表（按用户对话）· 密集行 =====
  h += '<div class="panel"><h2>任务列表（按用户对话）</h2>';
  const COMP_LABEL = {{'completed': '✓ 已完成', 'partial': '◐ 部分完成', 'interrupted': '✗ 疑似中断'}};
  const COMP_CLASS = {{'completed': 'badge-skill', 'partial': 'badge-noskill', 'interrupted': 'badge-blocked'}};
  data.tasks.forEach((tk, i) => {{
    h += '<div class="task-card">';
    h += '<div class="q">#' + (i+1) + ' ' + esc(tk.query) + '</div>';
    h += '<div class="meta">' + fmtTime(tk.start_ts) + ' → ' + fmtTime(tk.end_ts);
    h += ' · 活跃 ' + fmtDur(tk.active_s) + ' · 等待 ' + fmtDur(tk.wait_s);
    h += '</div>';
    h += '<div class="meta">';
    const comp = tk.completion || 'interrupted';
    if (tk.skill_loaded) h += '<span class="badge badge-skill">⚡ ' + esc(tk.skill_loaded) + '</span>';
    if (tk.skill_via_script) h += '<span class="badge badge-skill">📜 ' + esc(tk.skill_via_script) + '</span>';
    if (!tk.skill_loaded && !tk.skill_via_script) h += '<span class="badge badge-noskill">无 Skill</span>';
    h += '<span class="badge badge-noskill">工具 ' + tk.tool_calls + '</span>';
    h += '<span class="badge badge-noskill">in ' + fmtTok(tk.tokens.input) + '</span>';
    h += '<span class="badge badge-noskill">out ' + fmtTok(tk.tokens.output) + '</span>';
    h += '<span class="badge ' + (COMP_CLASS[comp]||'badge-noskill') + '">' + (COMP_LABEL[comp]||comp) + '</span>';
    if (tk.human_interventions > 0) h += '<span class="badge badge-blocked">🙋 ' + tk.human_interventions + '</span>';
    h += '</div>';
    if (tk.completion_reason) h += '<div class="muted" style="font-size:11px">' + esc(tk.completion_reason) + '</div>';

    // 嵌套子任务：默认折叠
    const subs = data.task_subitems.filter(s => s.belongs_to === tk.query);
    if (subs.length) {{
      const done = subs.filter(s => s.status === 'completed').length;
      h += '<details class="section"><summary>子任务 <span class="cnt">' + done + ' / ' + subs.length + ' 完成</span></summary><div class="detail-body">';
      h += '<table><thead><tr><th>子任务</th><th class="num">状态</th><th class="num">耗时</th></tr></thead><tbody>';
      subs.forEach(s => {{
        h += '<tr><td>' + esc(s.subject) + '</td><td class="num status-icon">' + statusCell(s) + '</td><td class="num">' + fmtDur(s.duration_s) + '</td></tr>';
      }});
      h += '</tbody></table></div></details>';
    }}
    h += '</div>';
  }});
  h += '</div>';

  // ===== 次要信息（默认折叠） =====

  // Skill 时间线
  h += '<details class="section"><summary>Skill 触发时间线 <span class="cnt">' + data.skill_events.length + ' 次</span></summary><div class="detail-body">';
  if (data.skill_events.length === 0) {{
    h += '<div class="empty">本会话未触发任何 Skill</div>';
  }} else {{
    h += '<table><thead><tr><th>时间</th><th>Skill</th></tr></thead><tbody>';
    data.skill_events.forEach(e => {{
      h += '<tr><td class="mono">' + fmtTime(e.ts) + '</td><td>⚡ ' + esc(e.skill_name) + '</td></tr>';
    }});
    h += '</tbody></table>';
  }}
  h += '</div></details>';

  // 人工介入
  h += '<details class="section"><summary>人工介入（AskUserQuestion） <span class="cnt">' + (data.total_human_interventions || 0) + ' 次</span></summary><div class="detail-body">';
  if (!data.human_interventions || data.human_interventions.length === 0) {{
    h += '<div class="empty">本会话 agent 未反向追问用户</div>';
  }} else {{
    data.human_interventions.forEach(q => {{
      h += '<div class="task-card" style="margin-bottom:8px">';
      h += '<div class="q">🙋 ' + esc(q.question) + '</div>';
      h += '<div class="meta">' + fmtTime(q.ts) + (q.header ? ' · ' + esc(q.header) : '') + '</div>';
      if (q.options && q.options.length) h += '<div class="muted" style="font-size:11px">选项：' + q.options.map(o => esc(o)).join(' / ') + '</div>';
      h += '</div>';
    }});
  }}
  h += '</div></details>';

  // 阻塞清单
  h += '<details class="section"><summary>阻塞清单 <span class="cnt">' + data.blocks.length + ' 处</span></summary><div class="detail-body">';
  if (data.blocks.length === 0) {{
    h += '<div class="empty">未检测到阻塞（全程无 blocked 状态）</div>';
  }} else {{
    h += '<table><thead><tr><th>子任务</th><th>状态</th><th>归属任务</th></tr></thead><tbody>';
    data.blocks.forEach(b => {{
      h += '<tr><td>' + esc(b.subject) + '</td><td class="badge-blocked">⛔ 阻塞</td><td class="muted">' + esc(b.belongs_to || '-') + '</td></tr>';
    }});
    h += '</tbody></table>';
  }}
  h += '</div></details>';

  // TaskList 追踪表（仅在有 TaskCreate 时渲染）
  if (data.task_subitems.length > 0) {{
    h += '<details class="section"><summary>TaskList 子任务执行追踪 <span class="cnt">' + data.task_subitems.length + ' 项</span></summary><div class="detail-body">';
    h += '<table><thead><tr><th>子任务</th><th class="num">状态</th><th class="num">耗时</th><th>归属任务</th></tr></thead><tbody>';
    data.task_subitems.forEach(s => {{
      h += '<tr><td>' + esc(s.subject) + '</td><td class="num status-icon">' + statusCell(s) + '</td><td class="num">' + fmtDur(s.duration_s) + '</td><td class="muted">' + esc(s.belongs_to || '-') + '</td></tr>';
    }});
    h += '</tbody></table>';
    h += '</div></details>';
  }}

  root.innerHTML = h;
}})();
</script>
</body>
</html>"""

    return html


def main():
    ap = argparse.ArgumentParser(description="单会话深度评测 HTML 报告（支持 Claude Code JSONL 与 airLab pod 文本日志）")
    ap.add_argument(
        "--jsonl",
        default=r"C:\Users\wangjing71\.claude\projects\D--wy-projects-work-4-log\625d0eda-7662-42c8-9091-49603b17e203.jsonl",
        help="输入日志路径（JSONL 或 airLab pod 文本日志，自动检测）",
    )
    ap.add_argument("--out", default=None, help="输出 HTML 路径，缺省 results/session_report.html")
    args = ap.parse_args()

    # 格式自动检测：JSONL vs airLab pod 文本日志
    path = args.jsonl
    is_airlab = False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(4096)
        try:
            json.loads(head.splitlines()[0])
        except Exception:
            is_airlab = True

    if is_airlab:
        data = scan_airlab_log(path)
    else:
        data = scan_single_session(path)

    # 终端摘要
    print("=== 单会话评测摘要 ===")
    print(f"session_id: {data['session_id']}" + ("  (airLab pod 日志)" if data.get("airlab") else ""))
    print(f"真实任务数: {data['task_count']} | Skill 触发任务: {data['skill_triggered_tasks']}")
    print(f"工具调用: {data['total_tool_calls']} | token: input {data['tokens']['input']:,} / cache_read {data['tokens']['cache_read']:,} / output {data['tokens']['output']:,}")
    if data.get("airlab"):
        markup = ""
        if data.get("cost_analysis") and data["cost_analysis"].get("markup"):
            ca = data["cost_analysis"]
            markup = f" | 加价(本次) {ca['markup']:.3f}x"
            if ca.get("stable_markup"):
                markup += f" (稳定值 {ca['stable_markup']:.3f}x)"
        print(f"成本: CNY {data.get('cost_usd', 0):.4f} | turns: {data.get('turns', '-')} | 总耗时: {_fmt_duration(data['duration_s'])}{markup}")
    else:
        print(f"子任务: {data['completed_sub']}/{data['total_sub']} 完成 | 阻塞: {len(data['blocks'])}")
    print()

    out = args.out or str(Path(__file__).parent / "results" / "session_report.html")
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(data), encoding="utf-8")
    print(f"报告已生成: {out_path}")


if __name__ == "__main__":
    main()
